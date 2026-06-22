"""
ECS Fargate entrypoint — downloads a binary instrument file from S3,
decodes it into structured records, embeds each record, and writes to PostgreSQL.
"""
import boto3
import json
import logging
import numpy as np
import os
import struct
import psycopg2
from dataclasses import dataclass

log = logging.getLogger(__name__)

s3      = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))


@dataclass
class ExperimentRecord:
    instrument_id:  str
    session_id:     str
    timestamp_utc:  float
    channel_count:  int
    sample_rate:    int
    duration_sec:   float
    payload:        np.ndarray   # shape: (channel_count, num_samples)
    metadata:       dict


# Format specs for each supported instrument type.
# header_fmt uses Python struct notation; header_bytes is the fixed header size.
INSTRUMENT_FORMATS = {
    "type_a": {"header_fmt": "16sf4i", "header_bytes": 36, "dtype": np.float32},
    "type_b": {"header_fmt": "16sf4i", "header_bytes": 36, "dtype": np.int16},
    "type_c": {"header_fmt": "16sd4i", "header_bytes": 44, "dtype": np.float64},
}


def decode(raw_bytes: bytes, instrument_id: str) -> ExperimentRecord:
    fmt = INSTRUMENT_FORMATS[instrument_id]
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


def to_text_chunk(record: ExperimentRecord) -> str:
    return (
        f"Session {record.session_id} | Instrument: {record.instrument_id} | "
        f"Channels: {record.channel_count} | Sample rate: {record.sample_rate}Hz | "
        f"Duration: {record.duration_sec:.1f}s | "
        f"Mean amplitude: {record.payload.mean():.4f} | "
        f"Peak amplitude: {float(np.abs(record.payload).max()):.4f}"
    )


def embed(text: str) -> list[float]:
    resp = bedrock.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text}),
    )
    return json.loads(resp["body"].read())["embedding"]


def write_to_db(conn, researcher_id: str, record: ExperimentRecord):
    text  = to_text_chunk(record)
    vec   = embed(text)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO experiment_chunks
                (researcher_id, instrument_id, session_id, timestamp_utc,
                 content, embedding, metadata)
            VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
            ON CONFLICT (instrument_id, session_id) DO UPDATE
                SET content    = EXCLUDED.content,
                    embedding  = EXCLUDED.embedding,
                    updated_at = now()
        """, (
            researcher_id,
            record.instrument_id,
            record.session_id,
            record.timestamp_utc,
            text,
            vec,
            json.dumps(record.metadata),
        ))
    conn.commit()
    log.info("Wrote session %s to DB", record.session_id)


def main():
    bucket        = os.environ["S3_BUCKET"]
    key           = os.environ["S3_KEY"]
    instrument_id = os.environ["INSTRUMENT_ID"]
    researcher_id = os.environ["RESEARCHER_ID"]
    db_url        = os.environ["DATABASE_URL"]

    log.info("Decoding s3://%s/%s", bucket, key)
    obj       = s3.get_object(Bucket=bucket, Key=key)
    raw_bytes = obj["Body"].read()

    record = decode(raw_bytes, instrument_id)
    log.info("Decoded: %s — %d channels, %.1fs", record.session_id, record.channel_count, record.duration_sec)

    conn = psycopg2.connect(db_url)
    write_to_db(conn, researcher_id, record)
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
