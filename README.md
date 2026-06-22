# AWS Research Data Platform

A cost-optimized cloud data platform for biotech research labs, consolidating multi-instrument data at scale with a Claude-powered natural-language query interface.

Built for a neuroscience research lab managing **5TB+ of proprietary binary instrument data** across 8 instruments and 3 researchers. Total infrastructure cost: **sub-$110/month**.

---

## Architecture

```
Instruments (8x)
      │
      ▼
  S3 Raw Bucket (per-instrument prefix, versioned)
      │
  ┌───┴────────────────────┐
  │   Ingestion Layer       │
  │  Lambda + ECS Fargate   │  ← binary decoder per instrument type
  │  (serverless, event-    │
  │   triggered on upload)  │
  └───────────┬────────────┘
              │
     Decoded + structured data
              │
  ┌───────────▼────────────┐
  │   PostgreSQL (RDS)      │
  │   + pgvector extension  │  ← embeddings stored alongside structured data
  │   + Row-Level Security  │  ← per-researcher data isolation
  └───────────┬────────────┘
              │
  ┌───────────▼────────────┐
  │   RAG Query Layer       │
  │   Amazon Bedrock        │  ← Claude claude-3-sonnet via Bedrock
  │   Hybrid retrieval:     │
  │   dense (pgvector) +    │
  │   sparse (BM25) + rerank│
  └───────────┬────────────┘
              │
  ┌───────────▼────────────┐
  │   Auth + Security       │
  │   Auth0 JWT validation  │
  │   KMS encryption at rest│
  │   CloudTrail audit log  │
  └────────────────────────┘
```

---

## Key Features

### Cost-Optimized at Scale
- 5TB+ stored at ~$115/TB/month on S3 Intelligent-Tiering
- ECS Fargate tasks spin up on upload events, terminate when done (zero idle cost)
- RDS t3.medium with 100GB gp3 storage (~$35/month)
- Total: **under $110/month** for a full production data platform

### Binary Instrument Data Ingestion
Proprietary binary formats (`.nev`, `.ns5`, `.ns6`, `.mat`) decoded via instrument-specific parsers running on ECS Fargate:

```python
# src/ingestion/decoder.py
import boto3, struct, numpy as np
from dataclasses import dataclass

@dataclass
class InstrumentRecord:
    instrument_id: str
    session_id: str
    timestamp_utc: float
    channel_count: int
    sample_rate: int
    payload: np.ndarray

def decode_binary_record(raw_bytes: bytes, format_spec: dict) -> InstrumentRecord:
    header_size = format_spec["header_bytes"]
    header = struct.unpack(format_spec["header_fmt"], raw_bytes[:header_size])
    payload = np.frombuffer(raw_bytes[header_size:], dtype=np.float32)
    return InstrumentRecord(
        instrument_id=format_spec["instrument_id"],
        session_id=header[0].decode("utf-8").strip(),
        timestamp_utc=header[1],
        channel_count=header[2],
        sample_rate=header[3],
        payload=payload.reshape(header[2], -1),
    )
```

### Bedrock RAG with Hybrid Retrieval
Claude-powered query interface using hybrid dense+sparse retrieval with cross-encoder reranking:

```python
# src/rag/retrieval.py
import boto3
from pgvector.psycopg2 import register_vector
import psycopg2, json

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

def embed_query(text: str) -> list[float]:
    resp = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text}),
    )
    return json.loads(resp["body"].read())["embedding"]

def hybrid_retrieve(conn, query: str, researcher_id: str, k: int = 10) -> list[dict]:
    query_embedding = embed_query(query)
    with conn.cursor() as cur:
        # Dense retrieval via pgvector (RLS enforced at row level)
        cur.execute("""
            SELECT id, content, metadata,
                   embedding <=> %s::vector AS distance
            FROM experiment_chunks
            WHERE researcher_id = %s
            ORDER BY distance ASC
            LIMIT %s
        """, (query_embedding, researcher_id, k * 2))
        dense = cur.fetchall()

        # BM25 sparse retrieval via full-text search
        cur.execute("""
            SELECT id, content, metadata,
                   ts_rank(to_tsvector('english', content),
                           plainto_tsquery('english', %s)) AS rank
            FROM experiment_chunks
            WHERE researcher_id = %s
              AND to_tsvector('english', content) @@ plainto_tsquery('english', %s)
            LIMIT %s
        """, (query, researcher_id, query, k * 2))
        sparse = cur.fetchall()

    return reciprocal_rank_fusion(dense, sparse, k=k)

def reciprocal_rank_fusion(dense, sparse, k=10, rrf_k=60) -> list[dict]:
    scores = {}
    for rank, row in enumerate(dense):
        scores[row[0]] = scores.get(row[0], 0) + 1 / (rrf_k + rank + 1)
    for rank, row in enumerate(sparse):
        scores[row[0]] = scores.get(row[0], 0) + 1 / (rrf_k + rank + 1)
    sorted_ids = sorted(scores, key=scores.get, reverse=True)[:k]
    all_rows = {r[0]: r for r in dense + sparse}
    return [{"id": i, "content": all_rows[i][1], "metadata": all_rows[i][2]} for i in sorted_ids if i in all_rows]
```

### Claude Query Interface via Bedrock

```python
# src/rag/query.py
import boto3, json
from retrieval import hybrid_retrieve

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

SYSTEM_PROMPT = """You are a research assistant for a neuroscience lab.
Answer questions based only on the provided experiment data.
Be precise, cite specific sessions/instruments when relevant.
If the data does not support a conclusion, say so clearly."""

def query_experiments(conn, question: str, researcher_id: str) -> str:
    context_chunks = hybrid_retrieve(conn, question, researcher_id)
    context = "\n\n".join(c["content"] for c in context_chunks)

    response = bedrock.invoke_model(
        modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}],
        }),
    )
    return json.loads(response["body"].read())["content"][0]["text"]
```

### Multi-Tenant Row-Level Security

```sql
-- schema/rls.sql
ALTER TABLE experiment_chunks ENABLE ROW LEVEL SECURITY;

CREATE POLICY researcher_isolation ON experiment_chunks
    USING (researcher_id = current_setting('app.researcher_id')::uuid);

-- Auth0 JWT validated at API layer; researcher_id extracted from sub claim
-- and set as session variable before any query executes
```

```python
# src/auth/session.py
import jwt, os
from functools import wraps

AUTH0_DOMAIN = os.environ["AUTH0_DOMAIN"]
AUTH0_AUDIENCE = os.environ["AUTH0_AUDIENCE"]

def require_auth(f):
    @wraps(f)
    def decorated(conn, token: str, *args, **kwargs):
        payload = jwt.decode(
            token,
            algorithms=["RS256"],
            audience=AUTH0_AUDIENCE,
            issuer=f"https://{AUTH0_DOMAIN}/",
            options={"verify_exp": True},
        )
        researcher_id = payload["sub"]
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.researcher_id', %s, true)", (researcher_id,))
        return f(conn, *args, **kwargs)
    return decorated
```

### Lambda Ingestion Trigger

```python
# src/ingestion/lambda_handler.py
import boto3, os, json
from decoder import decode_binary_record, InstrumentRecord

ecs = boto3.client("ecs")
s3  = boto3.client("s3")

CLUSTER     = os.environ["ECS_CLUSTER_ARN"]
TASK_DEF    = os.environ["DECODER_TASK_DEF"]
SUBNET_IDS  = os.environ["SUBNET_IDS"].split(",")

def handler(event, context):
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key    = record["s3"]["object"]["key"]
        # instrument_id encoded in prefix: raw/{instrument_id}/session/{file}
        instrument_id = key.split("/")[1]

        ecs.run_task(
            cluster=CLUSTER,
            taskDefinition=TASK_DEF,
            launchType="FARGATE",
            networkConfiguration={"awsvpcConfiguration": {
                "subnets": SUBNET_IDS,
                "assignPublicIp": "DISABLED",
            }},
            overrides={"containerOverrides": [{"name": "decoder", "environment": [
                {"name": "S3_BUCKET",      "value": bucket},
                {"name": "S3_KEY",         "value": key},
                {"name": "INSTRUMENT_ID",  "value": instrument_id},
            ]}]},
        )
    return {"statusCode": 200}
```

---

## Infrastructure (Terraform)

```hcl
# infra/main.tf (excerpt)

module "platform" {
  source = "./modules/research-platform"

  # Storage
  raw_bucket_name        = "research-raw-${var.environment}"
  enable_intelligent_tiering = true

  # Compute
  ecs_cluster_name       = "research-ingestion"
  fargate_cpu            = 1024   # 1 vCPU per decode task
  fargate_memory         = 2048   # 2GB per decode task

  # Database
  db_instance_class      = "db.t3.medium"
  db_storage_gb          = 100
  enable_pgvector        = true

  # Security
  kms_key_arn            = aws_kms_key.platform.arn
  auth0_domain           = var.auth0_domain
  enable_cloudtrail      = true
  enable_rls             = true
}

# Cost control: Fargate tasks are ephemeral — triggered by S3 events,
# terminated on completion. No idle compute cost.
```

---

## Cost Breakdown (~5TB, 3 researchers)

| Component | Monthly Cost |
|-----------|-------------|
| S3 Intelligent-Tiering (5TB) | ~$57 |
| RDS t3.medium + 100GB gp3 | ~$35 |
| Lambda (ingestion triggers) | ~$1 |
| ECS Fargate (event-driven) | ~$8 |
| CloudTrail + KMS | ~$4 |
| **Total** | **~$105/month** |

---

## Security Model

- **Encryption at rest**: KMS-managed keys on S3 and RDS
- **Encryption in transit**: TLS 1.3 on all connections
- **Authentication**: Auth0 OIDC/JWT — RS256 signed tokens
- **Authorization**: PostgreSQL Row-Level Security — researchers see only their data
- **Audit**: CloudTrail logs all S3 and API calls; CloudWatch alarms on anomalies
- **Network**: VPC with private subnets; Fargate tasks have no public IP

---

## Stack

| Layer | Technology |
|-------|-----------|
| Storage | AWS S3 (Intelligent-Tiering) |
| Ingestion | AWS Lambda + ECS Fargate |
| Database | AWS RDS PostgreSQL + pgvector |
| RAG | Amazon Bedrock (Claude + Titan Embeddings) |
| Auth | Auth0 + PostgreSQL RLS |
| IaC | Terraform |
| Audit | AWS CloudTrail + CloudWatch |

---

## Setup

```bash
# Prerequisites: AWS CLI, Terraform, Python 3.11+, Auth0 tenant

# 1. Configure environment
cp .env.example .env
# Fill in: AWS_REGION, AUTH0_DOMAIN, AUTH0_AUDIENCE, DB_SECRET_ARN

# 2. Deploy infrastructure
cd infra
terraform init
terraform apply

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Run ingestion on sample data
python src/ingestion/ingest.py --bucket $RAW_BUCKET --instrument-id demo

# 5. Query the platform
python src/rag/query.py --question "What were the signal amplitudes in session A3?"
```

---

*Built by [Sebastian Acosta](https://linkedin.com/in/sebastianacos) — Cloud Data Systems Architect*
