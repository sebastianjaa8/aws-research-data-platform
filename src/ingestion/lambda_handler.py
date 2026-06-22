"""Dispatch an ECS Fargate decode task for each S3 PutObject record."""
import boto3
import os
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
                    # RESEARCHER_ID must be resolved from instrument_id before dispatch.
                    # Implementation: DynamoDB lookup keyed on instrument_id, or
                    # a Lambda environment variable mapping (for fixed-assignment labs).
                ]}]},
            )
        except Exception:
            failures.append({"itemIdentifier": key})

    if failures:
        return {"batchItemFailures": failures}
    return {"statusCode": 200, "body": f"Dispatched {len(event['Records'])} decode tasks"}
