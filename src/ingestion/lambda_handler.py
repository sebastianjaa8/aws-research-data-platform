"""
S3 event trigger — spawns ECS Fargate decoder task per uploaded file.
Instrument ID is encoded in the S3 key prefix: raw/{instrument_id}/...
"""
import boto3
import os

ecs = boto3.client("ecs")

CLUSTER    = os.environ["ECS_CLUSTER_ARN"]
TASK_DEF   = os.environ["DECODER_TASK_DEF"]
SUBNETS    = os.environ["SUBNET_IDS"].split(",")
SG_IDS     = os.environ["SECURITY_GROUP_IDS"].split(",")


def handler(event, context):
    for record in event["Records"]:
        bucket        = record["s3"]["bucket"]["name"]
        key           = record["s3"]["object"]["key"]
        instrument_id = key.split("/")[1]   # raw/{instrument_id}/session/{file}

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
            ]}]},
        )

    return {"statusCode": 200, "body": f"Dispatched {len(event['Records'])} decode tasks"}
