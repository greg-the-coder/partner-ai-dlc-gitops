#!/usr/bin/env bash
##############################################################################
# microvm_run.sh — PROTOTYPE
#
# Find-or-create the AWS Lambda MicroVM backing this Coder workspace, then wait
# for it to reach RUNNING. Called by the create-time local-exec provisioner in
# main.tf. All inputs arrive via environment variables (never argv) so the Coder
# agent token is not exposed in process listings.
#
# NOTE (validated against the live API): run-microvm has NO --tags and NO
# --resources. Size is baked into the single-size image at build time. To map a
# workspace to its MicroVM across start/stop we keep a tiny index object in S3:
#   s3://$MICROVM_STATE_BUCKET/coder-microvm-index/<workspace-id> -> <microvmId>
# and use --client-token for run idempotency.
#
# The Coder agent token + access URL + init script are handed to the guest via
# the /run hook payload (the platform wraps it as {"microvmId","runHookPayload"};
# the in-guest hook_server.py unwraps runHookPayload). Payload goes through a
# 0600 temp file via fileb:// so secrets never hit argv.
##############################################################################
set -euo pipefail

log() { printf '[microvm_run] %s\n' "$*" >&2; }
command -v aws >/dev/null 2>&1 || { log "ERROR: aws CLI not found on the provisioner image."; exit 1; }

: "${CODER_WS_ID:?}"; : "${MICROVM_IMAGE_ID:?}"; : "${MICROVM_EXEC_ROLE:?}"
: "${AWS_REGION:?}"; : "${CODER_AGENT_TOKEN:?}"; : "${CODER_AGENT_URL:?}"
: "${MICROVM_STATE_BUCKET:?set MICROVM_STATE_BUCKET (S3) for the workspace->microvm index}"
MICROVM_IMAGE_VERSION="${MICROVM_IMAGE_VERSION:-}"   # empty => resolve ACTIVE version
MICROVM_MAX_DURATION="${MICROVM_MAX_DURATION:-28800}"
INDEX_KEY="coder-microvm-index/${CODER_WS_ID}"
INDEX_URI="s3://${MICROVM_STATE_BUCKET}/${INDEX_KEY}"

microvm_state() { aws lambda-microvms get-microvm --region "$AWS_REGION" --microvm-identifier "$1" --query 'state' --output text 2>/dev/null || echo "MISSING"; }

# --- 1. Reuse an existing MicroVM for this workspace, if the index points at a
#        still-live one ---------------------------------------------------------
existing="$(aws s3 cp "$INDEX_URI" - --region "$AWS_REGION" 2>/dev/null || true)"
if [ -n "${existing:-}" ]; then
  st="$(microvm_state "$existing")"
  case "$st" in
    RUNNING|SUSPENDED|PENDING) log "Reusing MicroVM $existing (state=$st)"; echo "$existing"; exit 0 ;;
    *) log "Indexed MicroVM $existing is $st; launching a fresh one." ;;
  esac
fi

# --- 2. Build the /run hook payload (secrets -> 0600 temp file) -------------
payload_file="$(mktemp)"; chmod 600 "$payload_file"
trap 'rm -f "$payload_file"' EXIT
cat > "$payload_file" <<JSON
{
  "coder_agent_token": "${CODER_AGENT_TOKEN}",
  "coder_agent_url": "${CODER_AGENT_URL}",
  "coder_agent_init_b64": "${CODER_AGENT_INIT_B64:-}",
  "efs_dns": "${EFS_DNS:-}",
  "efs_access_point_id": "${EFS_ACCESS_POINT_ID:-}",
  "home_dir": "/home/coder"
}
JSON

# --- 3. run-microvm ----------------------------------------------------------
# No --idle-policy: MicroVM idle is measured by INBOUND proxy traffic, but the
# Coder agent connects OUTBOUND, so it would be misread as idle. MVP maps
# workspace stop -> terminate. (Fast-follow: harness-driven suspend/resume.)
args=(
  --region "$AWS_REGION"
  --image-identifier "$MICROVM_IMAGE_ID"
  --execution-role-arn "$MICROVM_EXEC_ROLE"
  --maximum-duration-in-seconds "$MICROVM_MAX_DURATION"
  --run-hook-payload "fileb://${payload_file}"
  --client-token "coder-${CODER_WS_ID}"
  --logging "{\"cloudWatch\":{\"logGroup\":\"/aws/lambda-microvms/coder-microvm-agent\"}}"
)
[ -n "$MICROVM_IMAGE_VERSION" ] && args+=(--image-version "$MICROVM_IMAGE_VERSION")
[ -n "${MICROVM_EGRESS_CONN:-}" ] && args+=(--egress-network-connectors "[\"${MICROVM_EGRESS_CONN}\"]")

log "Launching MicroVM for workspace ${CODER_WS_ID}"
run_json="$(aws lambda-microvms run-microvm "${args[@]}")"
microvm_id="$(printf '%s' "$run_json" | jq -r '.microvmId')"
[ -n "$microvm_id" ] && [ "$microvm_id" != "null" ] || { log "ERROR: no microvmId in: $run_json"; exit 1; }

# Record the mapping so stop/delete can find it (no tags on run-microvm).
printf '%s' "$microvm_id" | aws s3 cp - "$INDEX_URI" --region "$AWS_REGION" >/dev/null
log "MicroVM ${microvm_id} launched; indexed at ${INDEX_URI}"

# --- 4. Wait for RUNNING (readiness is best determined by connecting, but the
#        state check is enough to unblock the Coder build) --------------------
for _ in $(seq 1 60); do
  case "$(microvm_state "$microvm_id")" in
    RUNNING) log "MicroVM ${microvm_id} is RUNNING."; echo "$microvm_id"; exit 0 ;;
    TERMINATING|TERMINATED) log "ERROR: entered terminal state before RUNNING (check /run hook)."; exit 1 ;;
    *) sleep 5 ;;
  esac
done
log "ERROR: timed out waiting for ${microvm_id} to reach RUNNING."; exit 1
