> **Template / Showcase Repository**
>
> This repository is a clean architectural template derived from a production system built for a biomedical research client. All of the following have been removed or replaced with synthetic stand-ins:
>
> - Client identity, institution name, and researcher details
> - Proprietary binary instrument format specifications (`.nev`, `.ns5`, `.ns6`, `.mat` header layouts, calibration constants, channel-mapping tables)
> - Actual experiment data, session IDs, and signal recordings
> - Infrastructure identifiers (account IDs, VPC IDs, S3 bucket names, RDS endpoints, Auth0 tenant)
> - Client-specific IAM policies and KMS key configurations
>
> The architecture, design decisions, cost model, security model, and all code structure shown here accurately represent the real deployed system. The sample data used in tests is fully synthetic. This repository exists to demonstrate the engineering approach, not to ship a turnkey clone — every real deployment requires instrument-specific decoder implementations and environment-specific Terraform variable files that are intentionally absent.

# AWS Research Data Platform

A cost-optimized cloud data platform for biotech research labs, consolidating multi-instrument data at scale with a Claude-powered natural-language query interface.

Built for a neuroscience research lab managing **5TB+ of proprietary binary instrument data** across 8 instruments and 3 researchers. Total infrastructure cost: **sub-$110/month**.

---

## Why This Was Built

### The Problem

A neuroscience research lab had accumulated more than 5TB of experimental recordings across 8 instruments over several years of active study. Every instrument wrote data in its own proprietary binary format. There was no shared schema, no common query surface, and no way for a researcher to ask a cross-instrument question like "which sessions showed elevated signal amplitude in channel 12 across the last six months" without manually loading individual files in proprietary desktop software, instrument by instrument.

Three researchers were spending meaningful time on data retrieval that should have been automated. Each researcher also needed strict data isolation — no researcher should be able to query another's unpublished sessions, and the lab needed a defensible audit trail for data access to satisfy IRB record-keeping requirements.

### The Constraints

This was a research lab, not an enterprise. The budget ceiling was set at under $150/month all-in. That constraint ruled out managed vector database services, dedicated ML inference infrastructure, and anything that charges per-query at scale. The team had no dedicated DevOps support, so operational complexity had to be low: if Terraform could not deploy it and a single Python script could not run it, it was the wrong tool.

The future requirement — natural-language querying of experiment data — was explicitly called out at the start of the engagement, which meant the storage and retrieval layer had to be designed with embedding search in mind from day one, not retrofitted later.

### The Solution Summary

A serverless ingestion pipeline converts proprietary binary files to structured records as they arrive. Structured records and dense vector embeddings are co-located in a single RDS PostgreSQL instance using the pgvector extension, enabling hybrid retrieval without a separate vector database. Researchers authenticate via Auth0; PostgreSQL Row-Level Security enforces data isolation at the database level so the application layer cannot accidentally leak cross-researcher data. Amazon Bedrock provides Claude-powered natural-language querying without requiring a direct OpenAI dependency or a separately provisioned inference server. Total infrastructure cost at 5TB and 3 researchers: approximately $105/month.

---

## Architecture

Four layers. Each layer has one job.

```
Instruments (8x)
      │  proprietary binary files (.nev, .ns5, .ns6, .mat)
      ▼
  S3 Raw Bucket
  raw/{instrument_id}/session/{session_id}/{file}
  (versioned, KMS-encrypted, Intelligent-Tiering)
      │
      │  S3 ObjectCreated event
      ▼
  ┌──────────────────────────────────┐
  │  Ingestion Layer                  │
  │  Lambda (dispatch only, <200ms)   │
  │      │                            │
  │      ▼                            │
  │  ECS Fargate task (ephemeral)     │
  │  - instrument-specific decoder    │
  │  - struct.unpack binary header    │
  │  - numpy float32 payload parse    │
  │  - Titan Embeddings v2 (Bedrock)  │
  │  - write chunks + vectors -> RDS  │
  │  (spins up on upload, terminates) │
  └──────────────┬───────────────────┘
                 │  structured records + 1536-dim embeddings
                 ▼
  ┌──────────────────────────────────┐
  │  PostgreSQL (RDS t3.medium)       │
  │  + pgvector (IVFFlat index)       │  <- embeddings alongside structured data
  │  + tsvector full-text index       │  <- sparse full-text retrieval
  │  + Row-Level Security policy      │  <- researcher_id isolation, DB-enforced
  └──────────────┬───────────────────┘
                 │
  ┌──────────────▼───────────────────┐
  │  Query Layer (per request)        │
  │  Auth0 JWT -> researcher_id       │
  │  SET LOCAL app.researcher_id      │  <- activates RLS policy
  │  Hybrid retrieval:                │
  │    dense  (pgvector cosine, k=16) │
  │    sparse (ts_rank full-text, k=16│
  │    fusion (Reciprocal Rank, k=8)  │
  │  Claude 3.5 Sonnet via Bedrock    │  <- generation on fused context
  └──────────────────────────────────┘

  Cross-cutting: KMS encryption at rest, TLS 1.3 in transit,
  CloudTrail audit on all S3 + API events, VPC private subnets
```

---

## Data Flow: From Instrument Upload to Research Answer

The diagram above shows the topology. Here is what actually happens to a file from the moment a researcher's instrument writes it to the moment a natural-language question returns a cited answer.

**Step 1 — Upload.** A researcher or an automated instrument sync script uploads a raw binary file to S3 under a prefix-encoded path: `raw/{instrument_id}/session/{session_id}/{filename}`. The instrument ID in the prefix is load-bearing — it determines which decoder the ingestion layer selects in Step 2.

**Step 2 — Trigger.** An S3 event notification fires a Lambda function on every `ObjectCreated` event in the raw bucket. The Lambda function reads the instrument ID from the object key (URL-decoded via `unquote_plus`), then calls `ecs.run_task` to launch a Fargate task with the bucket name, object key, and instrument ID injected as environment variables. The Lambda itself does no decoding — it is purely a dispatch layer. This keeps Lambda cold-start time under 200ms and avoids hitting Lambda's memory and timeout limits on large binary files.

**Step 3 — Decode.** The Fargate task downloads the binary file from S3, selects the correct format specification for the instrument ID, and calls the instrument-specific decoder. The decoder reads the binary header (session ID, timestamp, channel count, sample rate) and the payload (raw float32 signal array), then produces a structured `ExperimentRecord`. Each record is converted to a text chunk with metadata preserved alongside for full-text search and JSONB column storage.

**Step 4 — Embed and store.** Each chunk's text representation is passed to Amazon Titan Embeddings v2 via Bedrock to produce a 1536-dimensional dense vector. The structured fields, text chunk, and embedding vector are written to the `experiment_chunks` table in RDS PostgreSQL in a single transaction. The `researcher_id` foreign key is set from the session's ownership record. pgvector stores the embedding as a native column type, enabling cosine-distance index scans via an IVFFlat index.

**Step 5 — Query authentication.** When a researcher submits a natural-language question, the API layer validates the Auth0 JWT (RS256, checked against the Auth0 JWKS endpoint with TTL-aware caching that refreshes hourly to handle key rotation). The `sub` claim from the validated token is the researcher's identifier. Before executing any query, the application calls `SET LOCAL app.researcher_id = '<value>'` on the database connection, activating the Row-Level Security policy for that transaction. No application-layer filtering is needed — the database engine enforces isolation.

**Step 6 — Hybrid retrieval.** The query text is embedded via Titan Embeddings. A dense vector search (cosine distance via pgvector, IVFFlat index with `ivfflat.probes = 10`) retrieves the top 16 nearest chunks. A parallel PostgreSQL full-text search (`tsvector` / `plainto_tsquery` with `ts_rank` scoring) retrieves the top 16 lexically matching chunks. The two ranked lists are merged using Reciprocal Rank Fusion, which rewards chunks that rank highly in both lists without requiring a weighting scalar between the two scores. The top 8 fused results are passed to the generation step.

**Step 7 — Generation.** The fused context chunks are assembled into a prompt and sent to Claude 3.5 Sonnet via Amazon Bedrock. The system prompt instructs the model to answer only from the provided context, cite specific sessions and instruments, and explicitly say when the data does not support a conclusion. The model's response, along with source citations, is returned to the researcher. Every query is logged to the `query_log` table for IRB audit compliance.

---

## Key Design Decisions

Every major technology choice in this system was made against a specific alternative. The table below records the decision, the alternative considered, and the reason the chosen path won under this project's constraints.

### Storage and Retrieval Architecture

| Decision | Chosen | Alternative Considered | Reason |
|---|---|---|---|
| Vector store | pgvector on RDS PostgreSQL | Pinecone, Weaviate, OpenSearch | Co-locating embeddings with structured metadata in one database eliminates a second managed service, a second authentication surface, and $70-$200/month in dedicated vector DB fees. At this dataset size, IVFFlat recall is within acceptable bounds and query latency is under 80ms on a t3.medium. A dedicated vector DB adds operational complexity not justified until the dataset exceeds ~100M vectors. |
| Retrieval strategy | Hybrid dense + sparse (RRF fusion) | Pure dense (embedding-only) | Neuroscience experiment metadata contains precise terms — channel numbers, session IDs, instrument names, specific numeric thresholds — that dense retrieval systematically misses when the query uses exact strings. Sparse full-text search (ts_rank) catches exact-match cases; dense retrieval catches semantic paraphrase. RRF is parameter-free and robust; it does not require tuning a weighting scalar between the two lists. |
| Embedding model | Amazon Titan Embeddings v2 (Bedrock) | OpenAI `text-embedding-3-large` | Titan Embeddings is invoked via the same Bedrock client used for Claude generation, eliminating a second API dependency, a second secret, and cross-cloud egress on embedding calls. Staying AWS-native also simplifies IAM permission scoping and keeps proprietary instrument data within the AWS network boundary. |

### Compute Architecture

| Decision | Chosen | Alternative Considered | Reason |
|---|---|---|---|
| Ingestion compute | ECS Fargate (event-driven, ephemeral) | EC2 always-on worker, AWS Batch | Binary file decoding is bursty: upload events cluster around lab sessions, then go quiet for hours or days. An always-on EC2 instance would cost ~$30-$60/month in idle compute. Fargate tasks spin up on demand, run to completion, and terminate — the only cost is the decode time itself (~$0.04/session at 1 vCPU / 2GB). AWS Batch was considered but adds queue scheduling latency unnecessary at this throughput. |
| Ingestion trigger | Lambda dispatch + ECS Fargate decode | Lambda-only decode | Lambda's 15-minute timeout and 10GB memory ceiling are adequate for most files, but the largest recordings exceed 2GB uncompressed. Running the decoder inside Lambda would require streaming partial decodes with complex checkpoint logic. Lambda-as-dispatcher keeps the trigger function under 200ms and unconstrained on file size; the Fargate task gets a full memory ceiling appropriate to the file profile. |
| LLM inference | Amazon Bedrock | Self-hosted via SageMaker, OpenAI API | Bedrock requires no inference infrastructure management. SageMaker endpoint hosting of a comparable model would cost $150-$400/month for an always-on endpoint — more than the entire rest of the platform. OpenAI API introduces cross-cloud data transfer (instrument data embeddings leaving AWS) and a harder compliance story for IRB data handling. |

### Security Architecture

| Decision | Chosen | Alternative Considered | Reason |
|---|---|---|---|
| Data isolation | PostgreSQL Row-Level Security (database-enforced) | Application-layer `WHERE researcher_id = ?` filter | Application-layer filtering relies on every query path in the codebase correctly appending the filter. A single missing `WHERE` clause leaks cross-researcher data. RLS enforces isolation at the database engine level — even a bare `SELECT * FROM experiment_chunks` returns only the rows the current session is authorized to see. A bug in the application layer cannot produce a data leak. |
| Authentication | Auth0 OIDC + JWT (RS256) | AWS Cognito, self-managed sessions | Auth0 provides JWKS rotation, MFA, and OIDC compliance without any server-side session storage. The `sub` claim from the validated JWT maps directly to `researcher_id` in the database, keeping the auth-to-authorization chain auditable and stateless. |
| Audit | AWS CloudTrail + query_log table | Application-level logging only | CloudTrail provides tamper-resistant, AWS-managed logs of every S3 API call and RDS IAM authentication event. Application logs can be deleted or modified by a compromised application process; CloudTrail logs cannot be altered without leaving a separate trail. The `query_log` table records every RAG query for IRB record-keeping. |

---

## What Was Built

### Instrument-Agnostic Binary Ingestion

Each instrument writes data in a proprietary binary format with its own header layout, channel encoding, and payload structure. The ingestion layer uses a format-specification registry: each instrument ID maps to a `format_spec` dict describing header byte count, struct format string, and payload dtype. Adding a new instrument requires adding one registry entry and, if the format is novel, a decoder function — no changes to the Lambda trigger, Fargate task definition, or database schema.

```python
# src/ingestion/decoder.py
import boto3, json, logging, numpy as np, os, struct, psycopg2
from botocore.config import Config
from dataclasses import dataclass

log = logging.getLogger(__name__)

INSTRUMENT_FORMATS = {
    "type_a": {"header_fmt": "16sf4i", "header_bytes": 36, "dtype": np.float32},
    "type_b": {"header_fmt": "16sf4i", "header_bytes": 36, "dtype": np.int16},
    "type_c": {"header_fmt": "16sd4i", "header_bytes": 44, "dtype": np.float64},
}

@dataclass
class ExperimentRecord:
    instrument_id: str
    session_id: str
    timestamp_utc: float
    channel_count: int
    sample_rate: int
    duration_sec: float
    payload: np.ndarray
    metadata: dict

def decode(raw_bytes: bytes, instrument_id: str) -> ExperimentRecord:
    if instrument_id not in INSTRUMENT_FORMATS:
        raise ValueError(
            f"Unknown instrument_id {instrument_id!r}. Valid: {list(INSTRUMENT_FORMATS)}"
        )
    fmt = INSTRUMENT_FORMATS[instrument_id]
    if len(raw_bytes) < fmt["header_bytes"]:
        raise ValueError(
            f"File too short for instrument {instrument_id!r}: "
            f"need {fmt['header_bytes']} bytes, got {len(raw_bytes)}"
        )
    header = struct.unpack(fmt["header_fmt"], raw_bytes[:fmt["header_bytes"]])
    session_id, timestamp, channels, sample_rate, n_samples, _ = header
    payload = (
        np.frombuffer(raw_bytes[fmt["header_bytes"]:], dtype=fmt["dtype"])
        .reshape(channels, n_samples)
    )
    return ExperimentRecord(
        instrument_id=instrument_id,
        session_id=session_id.decode().strip("\x00"),
        timestamp_utc=timestamp,
        channel_count=channels,
        sample_rate=sample_rate,
        duration_sec=n_samples / sample_rate,
        payload=payload,
        metadata={"source_format": instrument_id, "n_samples": n_samples},
    )

def main():
    bucket        = os.environ["S3_BUCKET"]
    key           = os.environ["S3_KEY"]
    instrument_id = os.environ["INSTRUMENT_ID"]
    researcher_id = os.environ["RESEARCHER_ID"]
    db_url        = os.environ["DATABASE_URL"]

    s3 = boto3.client("s3")
    bedrock = boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(retries={"max_attempts": 3, "mode": "adaptive"}),
    )

    obj = s3.get_object(Bucket=bucket, Key=key)
    raw_bytes = obj["Body"].read()
    record = decode(raw_bytes, instrument_id)
    log.info("Decoded: %s — %d channels, %.1fs", record.session_id, record.channel_count, record.duration_sec)

    conn = psycopg2.connect(db_url)
    try:
        write_to_db(conn, researcher_id, record, bedrock)
    finally:
        conn.close()
```

### Lambda Ingestion Trigger

```python
# src/ingestion/lambda_handler.py
"""Dispatch an ECS Fargate decode task for each S3 PutObject record."""
import boto3, os
from urllib.parse import unquote_plus

ecs = boto3.client("ecs")

CLUSTER  = os.environ["ECS_CLUSTER_ARN"]
TASK_DEF = os.environ["DECODER_TASK_DEF"]
SUBNETS  = os.environ["SUBNET_IDS"].split(",")
SG_IDS   = os.environ["SECURITY_GROUP_IDS"].split(",")

def handler(event, context):
    failures = []
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key    = unquote_plus(record["s3"]["object"]["key"])  # decode URL-encoded keys

        parts = key.split("/")
        if len(parts) < 3 or parts[0] != "raw":
            raise ValueError(f"Unexpected S3 key format: {key!r}")
        instrument_id = parts[1]

        try:
            ecs.run_task(
                cluster=CLUSTER,
                taskDefinition=TASK_DEF,
                launchType="FARGATE",
                networkConfiguration={"awsvpcConfiguration": {
                    "subnets":        SUBNETS,
                    "securityGroups": SG_IDS,
                    "assignPublicIp": "DISABLED",
                }},
                overrides={"containerOverrides": [{"name": "decoder", "environment": [
                    {"name": "S3_BUCKET",     "value": bucket},
                    {"name": "S3_KEY",        "value": key},
                    {"name": "INSTRUMENT_ID", "value": instrument_id},
                    # RESEARCHER_ID resolved from instrument_id -> researcher mapping
                    # stored in a DynamoDB table or Lambda environment variable
                ]}]},
            )
        except Exception:
            failures.append({"itemIdentifier": key})

    if failures:
        return {"batchItemFailures": failures}
    return {"statusCode": 200, "body": f"Dispatched {len(event['Records'])} decode tasks"}
```

### Hybrid Retrieval with Reciprocal Rank Fusion

Neuroscience experiment data contains two distinct query modalities. Researchers ask semantic questions ("sessions where the signal showed irregular bursting patterns") and also exact-match questions ("all recordings from instrument NS-06 in session A3-2024"). Pure dense retrieval fails on exact-match queries when the embedding space does not preserve precise identifiers. Pure sparse retrieval fails on semantic queries. Hybrid retrieval with RRF handles both without a scalar weighting parameter to tune.

```python
# src/rag/retrieval.py
import boto3, json, os, psycopg2

bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

def embed_query(text: str) -> list[float]:
    resp = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text}),
    )
    return json.loads(resp["body"].read())["embedding"]

def dense_search(cur, query_vec: list[float], k: int) -> list[tuple]:
    """pgvector cosine similarity search. RLS filters rows by researcher automatically."""
    cur.execute("SET LOCAL ivfflat.probes = 10")   # recall tuning: ~sqrt(lists=100)
    cur.execute("""
        SELECT id, content, metadata,
               1 - (embedding <=> %s::vector) AS score
        FROM experiment_chunks
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """, (query_vec, query_vec, k))
    return cur.fetchall()

def sparse_search(cur, query: str, k: int) -> list[tuple]:
    """PostgreSQL full-text search (ts_rank). RLS filters rows by researcher automatically."""
    cur.execute("""
        SELECT id, content, metadata,
               ts_rank(to_tsvector('english', content),
                       plainto_tsquery('english', %s)) AS score
        FROM experiment_chunks
        WHERE to_tsvector('english', content) @@ plainto_tsquery('english', %s)
        ORDER BY score DESC
        LIMIT %s
    """, (query, query, k))
    return cur.fetchall()

def reciprocal_rank_fusion(dense_rows, sparse_rows, k=8, rrf_k=60) -> list[dict]:
    """
    Standard RRF formula: score(d) = sum(1 / (rrf_k + rank(d)))
    Merges dense and sparse rankings without needing score normalization.
    """
    scores: dict = {}
    rows:   dict = {}
    for rank, row in enumerate(dense_rows):
        scores[row[0]] = scores.get(row[0], 0.0) + 1.0 / (rrf_k + rank + 1)
        rows[row[0]] = row
    for rank, row in enumerate(sparse_rows):
        scores[row[0]] = scores.get(row[0], 0.0) + 1.0 / (rrf_k + rank + 1)
        rows[row[0]] = row
    top_k = sorted(scores, key=scores.__getitem__, reverse=True)[:k]
    return [{"id": i, "content": rows[i][1], "metadata": rows[i][2], "score": scores[i]} for i in top_k]

def retrieve(conn, query: str, k: int = 8) -> list[dict]:
    """
    Main entry point. Session variable app.researcher_id must be set before calling
    (set by set_researcher_context) so RLS policies apply to both searches.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.researcher_id', true)")
        if not cur.fetchone()[0]:
            raise RuntimeError("researcher context not set — call set_researcher_context before retrieve")
    query_vec = embed_query(query)
    with conn.cursor() as cur:
        dense  = dense_search(cur, query_vec, k * 2)
        sparse = sparse_search(cur, query,    k * 2)
    return reciprocal_rank_fusion(dense, sparse, k=k)
```

### Claude Query Interface via Bedrock

The generation layer uses Claude 3.5 Sonnet via Amazon Bedrock. The system prompt constrains the model to the retrieved context — it is explicitly instructed to say when the data does not support a conclusion, rather than synthesize an answer from training data. Session and instrument citations are requested so researchers can trace answers back to source files. Every query is recorded to the `query_log` table for IRB audit compliance.

```python
# src/rag/query.py
import boto3, json, os, psycopg2
from src.auth.session import set_researcher_context
from src.rag.retrieval import retrieve

bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

MODEL_ID = "anthropic.claude-3-5-sonnet-20241022-v2:0"

SYSTEM_PROMPT = """You are a research assistant for a neuroscience lab.
Answer questions using only the experiment data provided below.
Be precise. Cite specific sessions and instruments when relevant.
If the data does not contain enough information to answer confidently, say so."""

def answer(conn, question: str, token: str) -> dict:
    """
    Full pipeline:
      1. Validate JWT and set researcher context (enforces RLS)
      2. Retrieve relevant experiment chunks via hybrid search
      3. Synthesize answer with Claude via Bedrock
      4. Log query to audit table
    """
    researcher_id = set_researcher_context(conn, token)
    chunks        = retrieve(conn, question)

    if not chunks:
        return {"answer": "No relevant experiment data found for this query.", "sources": []}

    context = "\n\n".join(f"[{i+1}] {c['content']}" for i, c in enumerate(chunks))
    prompt  = f"Experiment data:\n{context}\n\nQuestion: {question}"

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }),
    )
    answer_text = json.loads(response["body"].read())["content"][0]["text"]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO query_log (researcher_id, question, chunks_used) VALUES (%s, %s, %s)",
            (researcher_id, question, len(chunks)),
        )
    conn.commit()

    return {
        "answer":        answer_text,
        "researcher_id": researcher_id,
        "sources":       [{"id": c["id"], "content": c["content"][:120]} for c in chunks],
    }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Query the research data platform")
    parser.add_argument("--question", required=True)
    parser.add_argument("--token",    required=True, help="Auth0 JWT")
    args = parser.parse_args()

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        result = answer(conn, args.question, args.token)
    finally:
        conn.close()
    print(f"\nAnswer:\n{result['answer']}")
    print(f"\nSources used: {len(result['sources'])}")
```

### Database-Enforced Multi-Tenant Isolation

Row-Level Security is the correct control here, not application-layer filtering. Every query path in an application can have bugs; adding a `WHERE researcher_id = ?` filter to the application is a soft control. PostgreSQL RLS is a hard control: the database engine enforces it regardless of what the application sends.

The auth-to-isolation chain: Auth0 JWT validated at the API layer, `sub` claim extracted as researcher identifier, `SET LOCAL app.researcher_id` called on the connection, RLS policy evaluated by the database engine on every row scan. The policy includes an explicit NULL guard — if the session variable is unset, the policy fails closed (zero rows, no error leak).

```sql
-- schema/init.sql (security-relevant excerpt)
ALTER TABLE experiment_chunks ENABLE ROW LEVEL SECURITY;

CREATE POLICY researcher_owns_chunks ON experiment_chunks
    USING (
        researcher_id = (
            SELECT id FROM researchers
            WHERE auth0_sub = current_setting('app.researcher_id', true)
        )
        AND current_setting('app.researcher_id', true) IS NOT NULL
    )
    WITH CHECK (
        researcher_id = (
            SELECT id FROM researchers
            WHERE auth0_sub = current_setting('app.researcher_id', true)
        )
        AND current_setting('app.researcher_id', true) IS NOT NULL
    );

-- Application role: no BYPASSRLS privilege
-- Password provisioned out-of-band via Secrets Manager, never in DDL
CREATE ROLE platform_app LOGIN;
GRANT SELECT, INSERT, UPDATE ON experiment_chunks TO platform_app;
GRANT SELECT, INSERT ON query_log TO platform_app;
GRANT SELECT ON researchers TO platform_app;
GRANT USAGE ON SCHEMA public TO platform_app;
```

```python
# src/auth/session.py
import os, threading
from cachetools import TTLCache
import jwt
import psycopg2
import requests

AUTH0_DOMAIN   = os.environ["AUTH0_DOMAIN"]
AUTH0_AUDIENCE = os.environ["AUTH0_AUDIENCE"]
JWKS_URL       = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"

_jwks_cache = TTLCache(maxsize=1, ttl=3600)  # refresh hourly for key rotation
_jwks_lock  = threading.Lock()

def _get_jwks() -> dict:
    """TTL-cached fetch of Auth0 public keys (refreshed hourly to handle key rotation)."""
    with _jwks_lock:
        if "jwks" not in _jwks_cache:
            _jwks_cache["jwks"] = requests.get(JWKS_URL, timeout=5).json()
        return _jwks_cache["jwks"]

def validate_token(token: str) -> dict:
    """Validates an Auth0 JWT. Raises jwt.InvalidTokenError on any validation failure."""
    header = jwt.get_unverified_header(token)
    jwks   = _get_jwks()
    key    = next((k for k in jwks["keys"] if k["kid"] == header["kid"]), None)
    if key is None:
        raise jwt.InvalidTokenError(f"Unknown kid: {header['kid']}")
    pub_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
    return jwt.decode(
        token, pub_key,
        algorithms=["RS256"],
        audience=AUTH0_AUDIENCE,
        issuer=f"https://{AUTH0_DOMAIN}/",
        options={"verify_exp": True, "require": ["sub", "exp"]},
    )

def set_researcher_context(conn: psycopg2.extensions.connection, token: str) -> str:
    """
    Validates token, then sets app.researcher_id as a PostgreSQL session variable.
    PostgreSQL RLS policies read this variable to enforce per-researcher isolation.
    NOTE: caller must not commit between this call and the subsequent query — the
    SET LOCAL is transaction-scoped and resets on commit/rollback.
    Returns the researcher_id (Auth0 sub claim).
    """
    payload       = validate_token(token)
    researcher_id = payload["sub"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.researcher_id', %s, true)",
            (researcher_id,),
        )
    return researcher_id
```

---

## Infrastructure (Terraform)

```hcl
# infra/main.tf (excerpt)

module "platform" {
  source = "./modules/research-platform"

  # Storage
  raw_bucket_name            = "research-raw-${var.environment}"
  enable_intelligent_tiering = true

  # Compute
  ecs_cluster_name  = "research-ingestion"
  fargate_cpu       = 1024   # 1 vCPU per decode task
  fargate_memory    = 2048   # 2GB per decode task

  # Database
  db_instance_class = "db.t3.medium"
  db_storage_gb     = 100
  enable_pgvector   = true

  # Security
  kms_key_arn        = aws_kms_key.platform.arn
  auth0_domain       = var.auth0_domain
  enable_cloudtrail  = true
  enable_rls         = true
}

# Cost control: Fargate tasks are ephemeral — triggered by S3 events,
# terminated on completion. No idle compute cost.
```

---

## Cost Breakdown (~5TB, 3 researchers)

| Component | Monthly Cost |
|-----------|-------------|
| S3 Intelligent-Tiering (5TB, mixed-tier distribution) | ~$57 |
| RDS t3.medium + 100GB gp3 | ~$35 |
| Lambda (ingestion triggers) | ~$1 |
| ECS Fargate (event-driven, ~800 vCPU-min/month) | ~$8 |
| CloudTrail + KMS | ~$4 |
| **Total** | **~$105/month** |

S3 Standard at this volume would cost ~$115/month for storage alone. Intelligent-Tiering automatically migrates older sessions to cheaper access tiers while preserving millisecond retrieval for any session that becomes active again. A dedicated vector database (Pinecone Standard, Weaviate Cloud) would add $70-$140/month on top of these figures — the pgvector co-location approach eliminates that line item entirely.

---

## Security Model

- **Encryption at rest**: KMS-managed keys on S3 and RDS
- **Encryption in transit**: TLS 1.3 on all connections
- **Authentication**: Auth0 OIDC/JWT — RS256 signed tokens, JWKS-verified with hourly key-rotation refresh
- **Authorization**: PostgreSQL Row-Level Security — researchers see only their data, enforced at the database engine level with explicit NULL guard (fail-closed when session variable is unset)
- **Audit**: CloudTrail logs all S3 and API calls; every RAG query is recorded to `query_log`; CloudWatch alarms on anomalies
- **Network**: VPC with private subnets; Fargate tasks have no public IP
- **Secrets**: Database credentials provisioned via AWS Secrets Manager — no passwords in DDL or committed files

---

## Stack

| Layer | Technology |
|-------|-----------| 
| Storage | AWS S3 (Intelligent-Tiering, versioned) |
| Ingestion | AWS Lambda + ECS Fargate |
| Database | AWS RDS PostgreSQL 16 + pgvector |
| RAG | Amazon Bedrock (Claude 3.5 Sonnet + Titan Embeddings v2) |
| Auth | Auth0 OIDC + PostgreSQL RLS |
| IaC | Terraform |
| Audit | AWS CloudTrail + CloudWatch + query_log table |

---

## Setup

```bash
# Prerequisites: AWS CLI, Terraform, Python 3.11+, Auth0 tenant

# 1. Configure environment
cp .env.example .env
# Fill in: AWS_REGION, AUTH0_DOMAIN, AUTH0_AUDIENCE, DB_SECRET_ARN
# Production: use DB_SECRET_ARN — do not set DATABASE_URL directly

# 2. Deploy infrastructure
cd infra
terraform init
terraform apply

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Run ingestion on sample data
# Set S3_BUCKET, S3_KEY, INSTRUMENT_ID, RESEARCHER_ID, DATABASE_URL env vars
python src/ingestion/decoder.py

# 5. Query the platform
python src/rag/query.py --question "What were the signal amplitudes in session A3?" --token <jwt>
```

---

*Built by [Sebastian Acosta](https://linkedin.com/in/sebastianacosta) — Cloud Data Systems Architect*
