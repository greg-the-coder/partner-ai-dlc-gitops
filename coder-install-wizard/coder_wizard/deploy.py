"""
Deploy orchestrator — launches both CloudFormation stacks in the correct order,
streams real-time events back to the caller, and waits for completion.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# CloudFormation rejects an inline --template-body larger than this many bytes.
# Larger templates must be uploaded to S3 and referenced with --template-url
# (the S3 object limit is ~1 MB).
TEMPLATE_BODY_MAX_BYTES = 51200


@dataclass
class StackEvent:
    timestamp: str
    logical_id: str
    resource_type: str
    status: str
    reason: str = ""


@dataclass
class DeployResult:
    stack_name: str
    success: bool
    outputs: dict = field(default_factory=dict)
    error: str = ""
    stack_id: str = ""
    pending: bool = False  # True when the stack was submitted but not waited on (hand-off)


def _aws(args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        ["aws"] + args + ["--output", "json"],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _aws_json(args: list[str]) -> tuple[int, dict | list | None]:
    code, out, _ = _aws(args)
    if code != 0 or not out:
        return code, None
    try:
        return 0, json.loads(out)
    except json.JSONDecodeError:
        return 0, None


def _account_id() -> str:
    # NOTE: do not route through _aws() — it appends `--output json`, which would
    # override `--output text` and return the account id JSON-quoted (e.g. "123..."),
    # producing an invalid S3 bucket name.
    res = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        capture_output=True, text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else ""


def _ensure_template_bucket(region: str) -> tuple[Optional[str], str]:
    """Ensure a private S3 bucket exists for staging large CloudFormation templates.

    Returns (bucket_name, "") on success, or (None, error_message) on failure.
    """
    account = _account_id()
    if not account:
        return None, "could not determine AWS account id (aws sts get-caller-identity failed)"
    bucket = f"coder-wizard-templates-{account}-{region}"

    # head-bucket succeeds if it already exists and we own it.
    head = subprocess.run(
        ["aws", "s3api", "head-bucket", "--bucket", bucket, "--region", region],
        capture_output=True, text=True,
    )
    if head.returncode == 0:
        return bucket, ""

    create = ["aws", "s3api", "create-bucket", "--bucket", bucket, "--region", region]
    if region != "us-east-1":
        create += ["--create-bucket-configuration", f"LocationConstraint={region}"]
    res = subprocess.run(create, capture_output=True, text=True)
    if res.returncode != 0 and "BucketAlreadyOwnedByYou" not in res.stderr:
        return None, f"create-bucket '{bucket}' failed: {(res.stderr or res.stdout).strip()}"

    # Block public access; templates are fetched by CloudFormation using the caller's creds.
    subprocess.run(
        ["aws", "s3api", "put-public-access-block", "--bucket", bucket, "--region", region,
         "--public-access-block-configuration",
         "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"],
        capture_output=True, text=True,
    )

    # Default encryption: SSE-S3 (AES256), or SSE-KMS if a CMK is supplied via
    # CODER_WIZARD_TEMPLATE_KMS_KEY_ARN. Best-effort: skip silently if the caller
    # lacks s3:PutEncryptionConfiguration.
    kms_key = os.environ.get("CODER_WIZARD_TEMPLATE_KMS_KEY_ARN", "").strip()
    if kms_key:
        enc = {"Rules": [{
            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": kms_key},
            "BucketKeyEnabled": True,
        }]}
    else:
        enc = {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}
    subprocess.run(
        ["aws", "s3api", "put-bucket-encryption", "--bucket", bucket, "--region", region,
         "--server-side-encryption-configuration", json.dumps(enc)],
        capture_output=True, text=True,
    )

    # Staged templates are transient (needed only at stack create/update submission):
    # expire after 7 days and abort stale multipart uploads after 1 day. Best-effort.
    lifecycle = {"Rules": [{
        "ID": "expire-staged-templates",
        "Filter": {"Prefix": ""},
        "Status": "Enabled",
        "Expiration": {"Days": 7},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
    }]}
    subprocess.run(
        ["aws", "s3api", "put-bucket-lifecycle-configuration", "--bucket", bucket, "--region", region,
         "--lifecycle-configuration", json.dumps(lifecycle)],
        capture_output=True, text=True,
    )
    return bucket, ""


def _upload_template_to_s3(template_path: str, region: str, stack_name: str) -> tuple[Optional[str], str]:
    """Upload a template to S3 and return (https_url, "") or (None, error_message)."""
    bucket, err = _ensure_template_bucket(region)
    if not bucket:
        return None, err
    key = f"{stack_name}/{int(time.time())}-{os.path.basename(template_path)}"
    cp = subprocess.run(
        ["aws", "s3", "cp", template_path, f"s3://{bucket}/{key}", "--region", region],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        return None, f"upload to s3://{bucket}/{key} failed: {(cp.stderr or cp.stdout).strip()}"
    host = "s3.amazonaws.com" if region == "us-east-1" else f"s3.{region}.amazonaws.com"
    return f"https://{bucket}.{host}/{key}", ""


def _template_args(template_path: str, region: str, stack_name: str) -> tuple[list[str], Optional[str]]:
    """Return the CloudFormation CLI template argument.

    Uses --template-body for small templates; for templates over the inline
    51,200-byte API limit, stages the file in S3 and uses --template-url.
    Returns (args, error_message).
    """
    try:
        size = os.path.getsize(template_path)
    except OSError as e:
        return [], f"template not found: {e}"

    if size <= TEMPLATE_BODY_MAX_BYTES:
        return ["--template-body", f"file://{template_path}"], None

    url, err = _upload_template_to_s3(template_path, region, stack_name)
    if not url:
        return [], (
            f"template is {size} bytes (> {TEMPLATE_BODY_MAX_BYTES} inline limit) and could not "
            f"be staged to S3: {err}"
        )
    return ["--template-url", url], None


def _cfn_stack_status(stack_name: str, region: str) -> Optional[str]:
    code, data = _aws_json([
        "cloudformation", "describe-stacks",
        "--stack-name", stack_name,
        "--region", region,
    ])
    if code != 0 or data is None:
        return None
    stacks = data.get("Stacks", [])
    return stacks[0].get("StackStatus") if stacks else None


def _cfn_stack_outputs(stack_name: str, region: str) -> dict:
    code, data = _aws_json([
        "cloudformation", "describe-stacks",
        "--stack-name", stack_name,
        "--region", region,
    ])
    if code != 0 or data is None:
        return {}
    stacks = data.get("Stacks", [])
    if not stacks:
        return {}
    return {
        o["OutputKey"]: o["OutputValue"]
        for o in stacks[0].get("Outputs", [])
    }


def _cfn_recent_events(stack_name: str, region: str, since_token: Optional[str]) -> tuple[list[StackEvent], Optional[str]]:
    """Return new events since the last seen timestamp."""
    args = [
        "cloudformation", "describe-stack-events",
        "--stack-name", stack_name,
        "--region", region,
    ]
    code, data = _aws_json(args)
    if code != 0 or data is None:
        return [], since_token

    events_raw = data.get("StackEvents", [])

    seen_events: list[StackEvent] = []
    for e in events_raw:
        ts = e.get("Timestamp", "")
        if since_token and ts <= since_token:
            break
        seen_events.append(StackEvent(
            timestamp=ts,
            logical_id=e.get("LogicalResourceId", ""),
            resource_type=e.get("ResourceType", ""),
            status=e.get("ResourceStatus", ""),
            reason=e.get("ResourceStatusReason", ""),
        ))

    latest_ts = events_raw[0].get("Timestamp") if events_raw else since_token
    return list(reversed(seen_events)), latest_ts


TERMINAL_STATUSES = {
    "CREATE_COMPLETE", "CREATE_FAILED", "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED", "UPDATE_COMPLETE", "UPDATE_FAILED",
    "DELETE_COMPLETE", "DELETE_FAILED",
}

FAILURE_STATUSES = {
    "CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED",
    "UPDATE_FAILED", "DELETE_FAILED",
}

# A stack in one of these states exists and is healthy/usable.
SUCCESS_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}

# A stack in one of these states exists but is unusable and must be deleted
# before it can be recreated (e.g. a failed create that rolled back, or an
# unexecuted change-set stub).
RECREATE_STATUSES = {
    "ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "CREATE_FAILED",
    "DELETE_FAILED", "UPDATE_ROLLBACK_FAILED", "REVIEW_IN_PROGRESS",
}


def get_stack_status(stack_name: str, region: str) -> Optional[str]:
    """Return the CloudFormation stack status, or None if the stack does not exist."""
    return _cfn_stack_status(stack_name, region)


def get_stack_outputs(stack_name: str, region: str) -> dict:
    """Return the CloudFormation stack outputs as a dict (empty if none/not found)."""
    return _cfn_stack_outputs(stack_name, region)


def get_stack_id(stack_name: str, region: str) -> str:
    """Return the stack's physical ID (ARN), or "" if not found."""
    code, data = _aws_json([
        "cloudformation", "describe-stacks",
        "--stack-name", stack_name, "--region", region,
    ])
    if code == 0 and data:
        stacks = data.get("Stacks", [])
        if stacks:
            return stacks[0].get("StackId", "")
    return ""


def is_in_progress(status: Optional[str]) -> bool:
    return bool(status) and status.endswith("_IN_PROGRESS") and status != "REVIEW_IN_PROGRESS"


def delete_stack(
    stack_name: str,
    region: str,
    on_event: Callable[[StackEvent], None] | None = None,
    poll_interval: int = 15,
) -> DeployResult:
    """Delete a CloudFormation stack and wait until it is gone (or DELETE_FAILED)."""
    code, _, err = _aws([
        "cloudformation", "delete-stack",
        "--stack-name", stack_name,
        "--region", region,
    ])
    if code != 0:
        return DeployResult(stack_name=stack_name, success=False, error=err)

    last_ts: Optional[str] = None
    while True:
        status = _cfn_stack_status(stack_name, region)

        if on_event:
            new_events, last_ts = _cfn_recent_events(stack_name, region, last_ts)
            for evt in new_events:
                on_event(evt)

        # describe-stacks by name returns nothing once the stack is fully deleted.
        if status is None or status == "DELETE_COMPLETE":
            return DeployResult(stack_name=stack_name, success=True)
        if status == "DELETE_FAILED":
            return DeployResult(
                stack_name=stack_name, success=False,
                error="Stack ended in status: DELETE_FAILED",
            )
        time.sleep(poll_interval)


def deploy_stack(
    stack_name: str,
    template_path: str,
    parameters: dict,
    region: str,
    on_event: Callable[[StackEvent], None] | None = None,
    poll_interval: int = 15,
    on_failure: Optional[str] = None,
    wait: bool = True,
    on_submit: Callable[[str], None] | None = None,
) -> DeployResult:
    """
    Submit a CloudFormation stack. When wait=True, stream events until it reaches a
    terminal state; when wait=False, return as soon as it is submitted (hand-off).
    on_submit(stack_id) is invoked right after submission (e.g. to print console links).
    """
    param_list = [
        f"ParameterKey={k},ParameterValue={v}"
        for k, v in parameters.items()
    ]

    template_args, tmpl_err = _template_args(template_path, region, stack_name)
    if tmpl_err:
        return DeployResult(stack_name=stack_name, success=False, error=tmpl_err)

    create_args = [
        "cloudformation", "create-stack",
        "--stack-name", stack_name,
    ] + template_args + [
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_NAMED_IAM",
        "--region", region,
        "--parameters",
    ] + param_list

    # ROLLBACK (default) | DO_NOTHING | DELETE. DO_NOTHING keeps resources on
    # failure so stateful data (Aurora/EFS) and the EKS cluster aren't destroyed
    # by a rollback that would also fail on the shared VPC.
    if on_failure:
        create_args += ["--on-failure", on_failure]

    code, out, err = _aws(create_args)
    if code != 0:
        return DeployResult(stack_name=stack_name, success=False, error=err)

    stack_id = ""
    if out:
        try:
            stack_id = json.loads(out).get("StackId", "")
        except json.JSONDecodeError:
            pass

    if on_submit:
        on_submit(stack_id)

    if not wait:
        return DeployResult(stack_name=stack_name, success=True, stack_id=stack_id, pending=True)

    return wait_for_stack(stack_name, region, on_event=on_event,
                          poll_interval=poll_interval, stack_id=stack_id)


def wait_for_stack(
    stack_name: str,
    region: str,
    on_event: Callable[[StackEvent], None] | None = None,
    poll_interval: int = 15,
    stack_id: str = "",
) -> DeployResult:
    """Stream a stack's events until it reaches a terminal state.

    Tolerates transient describe failures (status None -> keep waiting), matching the
    original create-and-wait behavior. Callers that watch a possibly-nonexistent stack
    should check get_stack_status() first.
    """
    last_ts: Optional[str] = None
    while True:
        status = _cfn_stack_status(stack_name, region)

        if on_event:
            new_events, last_ts = _cfn_recent_events(stack_name, region, last_ts)
            for evt in new_events:
                on_event(evt)

        if status in TERMINAL_STATUSES:
            success = status not in FAILURE_STATUSES
            outputs = _cfn_stack_outputs(stack_name, region) if success else {}
            error = "" if success else f"Stack ended in status: {status}"
            return DeployResult(
                stack_name=stack_name,
                success=success,
                outputs=outputs,
                error=error,
                stack_id=stack_id,
            )

        time.sleep(poll_interval)


def wait_for_codebuild(project_name: str, region: str, timeout: int = 3600) -> bool:
    """
    Poll a CodeBuild project until its latest build succeeds or fails.
    Returns True on success, False otherwise.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, data = _aws_json([
            "codebuild", "list-builds-for-project",
            "--project-name", project_name,
            "--region", region,
        ])
        if code != 0 or not data:
            time.sleep(30)
            continue

        build_ids = data.get("ids", [])
        if not build_ids:
            time.sleep(30)
            continue

        code2, build_data = _aws_json([
            "codebuild", "batch-get-builds",
            "--ids", build_ids[0],
            "--region", region,
        ])
        if code2 != 0 or not build_data:
            time.sleep(30)
            continue

        builds = build_data.get("builds", [])
        if not builds:
            time.sleep(30)
            continue

        build = builds[0]
        build_status = build.get("buildStatus", "")

        if build_status == "SUCCEEDED":
            return True
        if build_status in {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}:
            return False

        time.sleep(30)

    return False  # timed out
