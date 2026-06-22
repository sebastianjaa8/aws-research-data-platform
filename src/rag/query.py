"""
Natural-language query interface for the research data platform.
Authenticates the researcher, retrieves relevant experiment chunks,
and passes them to Claude via Amazon Bedrock for synthesis.
"""
import boto3
import json
import os
import psycopg2

from src.auth.session import set_researcher_context
from src.rag.retrieval import retrieve

bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

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
    """
    researcher_id = set_researcher_context(conn, token)
    chunks        = retrieve(conn, question)

    if not chunks:
        return {"answer": "No relevant experiment data found for this query.", "sources": []}

    context = "\n\n".join(
        f"[{i+1}] {c['content']}" for i, c in enumerate(chunks)
    )
    prompt = f"Experiment data:\n{context}\n\nQuestion: {question}"

    response = bedrock.invoke_model(
        modelId="anthropic.claude-3-sonnet-20240229-v1:0",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }),
    )
    answer_text = json.loads(response["body"].read())["content"][0]["text"]

    return {
        "answer":       answer_text,
        "researcher_id": researcher_id,
        "sources":      [{"id": c["id"], "content": c["content"][:120]} for c in chunks],
    }


# --- CLI for local testing ---
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Query the research data platform")
    parser.add_argument("--question", required=True)
    parser.add_argument("--token",    required=True, help="Auth0 JWT")
    args = parser.parse_args()

    conn   = psycopg2.connect(os.environ["DATABASE_URL"])
    result = answer(conn, args.question, args.token)
    conn.close()

    print(f"\nAnswer:\n{result['answer']}")
    print(f"\nSources used: {len(result['sources'])}")
