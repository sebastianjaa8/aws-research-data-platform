"""
Hybrid retrieval: dense (pgvector cosine) + sparse (PostgreSQL full-text search).
Results fused via Reciprocal Rank Fusion, then returned for Claude generation.
Row-Level Security on the DB enforces per-researcher data isolation automatically.
"""
import boto3
import json
import os
import psycopg2

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


def reciprocal_rank_fusion(
    dense_rows: list[tuple],
    sparse_rows: list[tuple],
    k: int = 8,
    rrf_k: int = 60,
) -> list[dict]:
    """
    Standard RRF formula: score(d) = sum(1 / (rrf_k + rank(d)))
    Merges dense and sparse rankings without needing score normalization.
    """
    scores: dict[str, float] = {}
    rows:   dict[str, tuple] = {}

    for rank, row in enumerate(dense_rows):
        scores[row[0]] = scores.get(row[0], 0.0) + 1.0 / (rrf_k + rank + 1)
        rows[row[0]] = row

    for rank, row in enumerate(sparse_rows):
        scores[row[0]] = scores.get(row[0], 0.0) + 1.0 / (rrf_k + rank + 1)
        rows[row[0]] = row

    top_k = sorted(scores, key=scores.__getitem__, reverse=True)[:k]
    return [
        {"id": i, "content": rows[i][1], "metadata": rows[i][2], "score": scores[i]}
        for i in top_k
    ]


def retrieve(conn, query: str, k: int = 8) -> list[dict]:
    """
    Main entry point. Session variable app.researcher_id must be set before calling
    (enforced by set_researcher_context) so RLS policies apply to both searches.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.researcher_id', true)")
        if not cur.fetchone()[0]:
            raise RuntimeError("researcher context not set — call set_researcher_context before retrieve")
    query_vec = embed_query(query)
    with conn.cursor() as cur:
        dense  = dense_search(cur,  query_vec, k * 2)
        sparse = sparse_search(cur, query,     k * 2)
    return reciprocal_rank_fusion(dense, sparse, k=k)
