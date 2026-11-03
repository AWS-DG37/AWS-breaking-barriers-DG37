"""Launch a Fargate worker. Use --wait to fetch this exact run's results."""

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid4

import boto3
from botocore.exceptions import ClientError

from mel.bundle import local_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", default="farmerchat-mel")
    parser.add_argument("--region", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/agent.yaml"))
    parser.add_argument("--run-id", help="Reuse the same UUID and identical input/config to resume")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--wait", action="store_true", help="Wait for Fargate and download its results"
    )
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument(
        "--full",
        action="store_true",
        help="Process the entire input; default is only the first 3 nonblank rows",
    )
    args = parser.parse_args()
    run_id = str(UUID(args.run_id)) if args.run_id else str(uuid4())
    session = boto3.Session(region_name=args.region)
    stack = session.client("cloudformation").describe_stacks(StackName=args.stack)["Stacks"][0]
    outputs = {row["OutputKey"]: row["OutputValue"] for row in stack["Outputs"]}
    bucket = outputs["DataBucketName"]
    s3 = session.client("s3")
    snapshot = local_bundle(args.config)
    s3.upload_file(str(args.input), bucket, f"inputs/{run_id}/insights.jsonl")
    s3.put_object(
        Bucket=bucket,
        Key=f"config/{run_id}/bundle.json",
        Body=json.dumps(snapshot).encode(),
        ContentType="application/json",
    )
    command = [
        "run",
        "--run-id",
        run_id,
        "--input",
        f"s3://{bucket}/inputs/{run_id}/insights.jsonl",
        "--output",
        f"s3://{bucket}/outputs/{run_id}/results.jsonl",
        "--bundle-uri",
        f"s3://{bucket}/config/{run_id}/bundle.json",
    ]
    if args.retry_failed:
        command.append("--retry-failed")
    if args.full:
        command.append("--full")
    ecs = session.client("ecs")
    result = ecs.run_task(
        cluster=outputs["ClusterArn"],
        taskDefinition=outputs["WorkerTaskDefinitionArn"],
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [outputs["WorkerSubnetId"]],
                "securityGroups": [outputs["WorkerSecurityGroupId"]],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides={"containerOverrides": [{"name": "worker", "command": command}]},
        startedBy=f"mel-{run_id[:32]}",
    )
    if result.get("failures") or not result.get("tasks"):
        raise SystemExit(json.dumps(result.get("failures", [])))
    task = result["tasks"][0]
    run_directory = args.output_dir / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    manifest_path = run_directory / "launch.json"
    metadata = {
        "run_id": run_id,
        "task_arn": task["taskArn"],
        "task_definition": outputs["WorkerTaskDefinitionArn"],
        "config_version": snapshot["config"]["version"],
        "selection": "full" if args.full else "first_3",
        "log_group": outputs["WorkerLogGroup"],
        "results": f"s3://{bucket}/outputs/{run_id}/results.jsonl",
        "launch_manifest": str(manifest_path),
    }
    manifest_path.write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata, indent=2), flush=True)
    if not args.wait:
        return

    # Wait for the submitted task ARN, never a previously completed task.
    ecs.get_waiter("tasks_stopped").wait(
        cluster=outputs["ClusterArn"],
        tasks=[task["taskArn"]],
        WaiterConfig={"Delay": 5, "MaxAttempts": 720},
    )
    stopped = ecs.describe_tasks(cluster=outputs["ClusterArn"], tasks=[task["taskArn"]])
    if stopped.get("failures") or not stopped.get("tasks"):
        raise SystemExit("Could not inspect the submitted task: " + json.dumps(stopped))
    completed = stopped["tasks"][0]
    worker = next((c for c in completed.get("containers", []) if c["name"] == "worker"), {})
    metadata.update(
        {
            "status": completed.get("lastStatus"),
            "exit_code": worker.get("exitCode"),
            "stopped_reason": completed.get("stoppedReason"),
            "container_reason": worker.get("reason"),
            "image": worker.get("image"),
            "image_digest": worker.get("imageDigest"),
        }
    )
    manifest_path.write_text(json.dumps(metadata, indent=2))
    if worker.get("exitCode") is None:
        raise SystemExit("Worker did not finish normally: " + json.dumps(metadata))

    # A task-specific filename prevents a failed download from exposing an old local export.
    results_path = run_directory / f"results-{task['taskArn'].rsplit('/', 1)[-1]}.jsonl"
    try:
        s3.download_file(bucket, f"outputs/{run_id}/results.jsonl", str(results_path))
    except ClientError as error:
        raise SystemExit(
            "This run has no downloadable export; inspect its task/logs. "
            f"AWS error: {error.response['Error']['Code']}; manifest: {manifest_path}"
        ) from None
    metadata["local_results"] = str(results_path)
    manifest_path.write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata, indent=2), flush=True)
    print(results_path.read_text(), end="")
    if worker["exitCode"] != 0:
        raise SystemExit(worker["exitCode"])


if __name__ == "__main__":
    main()
