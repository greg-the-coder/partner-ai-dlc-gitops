"""
coder-microvm-controller — PROTOTYPE

A small AWS Lambda that runs / terminates Lambda MicroVMs on behalf of the Coder
template, so the Coder provisioner needs NO aws CLI (and no boto3) — it only
invokes this function via the Terraform `aws_lambda_invocation` resource.

Designed for `aws_lambda_invocation` with `lifecycle_scope = "CRUD"`, which
injects an `event["tf"]["action"]` of create | update | delete. On create/update
we find-or-run the workspace's MicroVM; on delete we terminate it. The
workspace -> microvmId mapping is kept in S3 (run-microvm has no tags), so
terminate can find the VM without any state passed from Terraform.

Input event fields (from the template):
  workspace_id, state_bucket, image_identifier, image_version (optional),
  execution_role_arn, egress_connector_arn (optional), max_duration,
  coder_agent_token, coder_agent_url, coder_agent_init_b64 (optional),
  efs_dns (optional), efs_access_point_id (optional)

Returns (create/update): {"microvm_id": "...", "endpoint": "..."}
Returns (delete):        {"terminated": "<id or ''>"}
"""
import json
import boto3

s3 = boto3.client("s3")
mv = boto3.client("lambda-microvms")

INDEX_PREFIX = "coder-microvm-index/"
LIVE = ("RUNNING", "SUSPENDED", "PENDING", "SUSPENDING", "RESUMING")


def _key(ws):
    return INDEX_PREFIX + ws


def _get_indexed(bucket, ws):
    try:
        obj = s3.get_object(Bucket=bucket, Key=_key(ws))
        return obj["Body"].read().decode().strip()
    except Exception:
        return None


def _state(mid):
    try:
        return mv.get_microvm(microvmIdentifier=mid).get("state", "MISSING")
    except Exception:
        return "MISSING"


def _endpoint(mid):
    try:
        return mv.get_microvm(microvmIdentifier=mid).get("endpoint", "")
    except Exception:
        return ""


def _run(event):
    ws = event["workspace_id"]
    bucket = event["state_bucket"]

    existing = _get_indexed(bucket, ws)
    if existing and _state(existing) in LIVE:
        print("reusing existing microvm %s for workspace %s" % (existing, ws))
        return {"microvm_id": existing, "endpoint": _endpoint(existing)}

    # runHookPayload is capped at 4096 bytes. The base64 Coder agent init script
    # alone is several KB, so only inline it if the whole payload still fits;
    # otherwise omit it and let the hook launch the baked `coder agent` binary
    # with just the token + URL.
    base = {
        "coder_agent_token": event["coder_agent_token"],
        "coder_agent_url": event["coder_agent_url"],
        "efs_dns": event.get("efs_dns", ""),
        "efs_access_point_id": event.get("efs_access_point_id", ""),
        "home_dir": "/home/coder",
    }
    init = event.get("coder_agent_init_b64", "")
    payload = json.dumps(base)
    if init:
        candidate = json.dumps(dict(base, coder_agent_init_b64=init))
        if len(candidate.encode("utf-8")) <= 4096:
            payload = candidate
        else:
            print("init script too large for runHookPayload (%d B); "
                  "falling back to baked `coder agent`." % len(candidate))
    kwargs = {
        "imageIdentifier": event["image_identifier"],
        "executionRoleArn": event["execution_role_arn"],
        "maximumDurationInSeconds": int(event.get("max_duration", 28800)),
        "runHookPayload": payload,
        # Idempotency guard for retries within the dedup window.
        "clientToken": ("coder-" + ws)[:64],
    }
    if event.get("image_version"):
        kwargs["imageVersion"] = event["image_version"]
    if event.get("egress_connector_arn"):
        kwargs["egressNetworkConnectors"] = [event["egress_connector_arn"]]

    resp = mv.run_microvm(**kwargs)
    mid = resp["microvmId"]
    print("ran microvm %s for workspace %s" % (mid, ws))
    s3.put_object(Bucket=bucket, Key=_key(ws), Body=mid.encode())
    return {"microvm_id": mid, "endpoint": resp.get("endpoint", _endpoint(mid))}


def _delete(event):
    ws = event["workspace_id"]
    bucket = event["state_bucket"]
    mid = _get_indexed(bucket, ws)
    if mid:
        try:
            mv.terminate_microvm(microvmIdentifier=mid)
            print("terminated microvm %s" % mid)
        except Exception as exc:  # noqa: BLE001 — best-effort; 8h cap is backstop
            print("WARNING: terminate failed for %s: %r" % (mid, exc))
        try:
            s3.delete_object(Bucket=bucket, Key=_key(ws))
        except Exception:
            pass
    return {"terminated": mid or ""}


def handler(event, context):
    action = (event.get("tf") or {}).get("action") or event.get("action") or "create"
    print("action=%s workspace=%s" % (action, event.get("workspace_id")))
    if action in ("create", "update"):
        return _run(event)
    if action == "delete":
        return _delete(event)
    return {"error": "unknown action: %s" % action}
