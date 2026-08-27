#!/usr/bin/env bash
##############################################################################
# microvm_run.sh — PROTOTYPE
#
# Find-or-create the AWS Lambda MicroVM backing this Coder workspace, then wait
# for it to reach RUNNING. Called by the create-time local-exec provisioner in
# main.tf. All inputs arrive via environment variables (never argv) so the Coder
# agent token is not exposed in process listings.
#
# Idempotency: MicroVMs are tagged com.coder.workspace.id=$CODER_WS_ID. On a
# workspace restart we reuse an existing non-terminated MicroVM for the same
# workspace id instead of launching a duplicate.
#
# The Coder agent token + access URL + init script are handed to the guest via
# the /run hook payload (written to a 0600 temp file and passed with fileb://,
# so secrets never hit argv). The in-guest hook server (images/coder-microvm-
# agent/hooks/hook_server.py) mounts EFS and launches `coder agent`.
#
# TODO (live-account validation): confirm exact flag names for the brand-new
# `aws lambda-microvms` CLI — in particular --tags on run-microvm and the
# --resources / --run-hook-payload shapes. Adjust once smoke-tested.
##############################################################################
set -euo pipefail

log() { printf '[microvm_run] %s\n' "$*" >&2; }

if ! command -v aws >/dev/null 2>&1; then
  log "ERROR: aws CLI not found on the provisioner. Install it in the Coder provisioner image."
  exit 1
fi

: "${CODER_WS_ID:?}"; : "${MICROVM_IMAGE_ID:?}"; : "${MICROVM_EXEC_ROLE:?}"
: "${AWS_REGION:?}"; : "${CODER_AGENT_TOKEN:?}"; : "${CODER_AGENT_URL:?}"
MICROVM_IMAGE_VERSION="${MICROVM_IMAGE_VERSION:-1.0}"
MICROVM_MAX_DURATION="${MICROVM_MAX_DURATION:-28800}"
MICROVM_CPU="${MICROVM_CPU:-4}"
MICROVM_MEM_GB="${MICROVM_MEM_GB:-8}"

# --- 1. Reuse an existing MicroVM for this workspace, if any ----------------
# (list-microvms + client-side filter on the workspace tag; the exact query API
# may differ — adjust after smoke test.)
existing="$(aws lambda-microvms list-microvms --region "$AWS_REGION" \
  --query "microvms[?tags.\"com.coder.workspace.id\"=='${CODER_WS_ID}' && state!='TERMINATED'].microvmId | [0]" \
  --output text 2>/dev/null || echo "None")"

if [ -n "$existing" ] && [ "$existing" != "None" ]; then
  log "Reusing existing MicroVM $existing for workspace $CODER_WS_ID"
  echo "$existing"
  exit 0
fi

# --- 2. Build the /run hook payload (secrets -> 0600 temp file) -------------
payload_file="$(mktemp)"
chmod 600 "$payload_file"
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

# --- 3. Assemble run-microvm args -------------------------------------------
# NOTE: no --idle-policy on purpose. MicroVM idle is measured by INBOUND proxy
# traffic only; the Coder agent connects OUTBOUND over the tailnet and would be
# misread as idle. MVP maps workspace stop -> terminate. (Fast-follow: drive
# suspend/resume from the harness on task state.)
args=(
  --region "$AWS_REGION"
  --image-identifier "$MICROVM_IMAGE_ID"
  --image-version "$MICROVM_IMAGE_VERSION"
  --execution-role-arn "$MICROVM_EXEC_ROLE"
  --maximum-duration-in-seconds "$MICROVM_MAX_DURATION"
  --resources "[{\"minimumVcpus\":${MICROVM_CPU},\"minimumMemoryInMiB\":$((MICROVM_MEM_GB * 1024))}]"
  --run-hook-payload "fileb://${payload_file}"
  --tags "com.coder.workspace.id=${CODER_WS_ID},com.coder.workspace.name=${CODER_WS_NAME:-unknown}"
)

# VPC egress connector (reach EFS mount targets on 2049 and/or a private coderd).
if [ -n "${MICROVM_EGRESS_CONN:-}" ]; then
  args+=(--egress-network-connectors "[\"${MICROVM_EGRESS_CONN}\"]")
fi

log "Launching MicroVM for workspace ${CODER_WS_ID} (${MICROVM_CPU} vCPU / ${MICROVM_MEM_GB} GB, max ${MICROVM_MAX_DURATION}s)"
run_json="$(aws lambda-microvms run-microvm "${args[@]}")"

microvm_id="$(printf '%s' "$run_json" | (command -v jq >/dev/null && jq -r '.microvmId' || sed -n 's/.*"microvmId"[: ]*"\([^"]*\)".*/\1/p'))"
endpoint="$(printf '%s' "$run_json"  | (command -v jq >/dev/null && jq -r '.endpoint // empty' || true))"

if [ -z "${microvm_id:-}" ] || [ "$microvm_id" = "null" ]; then
  log "ERROR: run-microvm did not return a microvmId. Response was: $run_json"
  exit 1
fi

log "MicroVM ${microvm_id} launched. endpoint=${endpoint:-<none>}"

# --- 4. Wait for RUNNING (the /run hook launches the Coder agent) -----------
for _ in $(seq 1 60); do
  state="$(aws lambda-microvms get-microvm --region "$AWS_REGION" \
    --microvm-identifier "$microvm_id" --query 'state' --output text 2>/dev/null || echo '')"
  case "$state" in
    RUNNING)      log "MicroVM ${microvm_id} is RUNNING."; echo "$microvm_id"; exit 0 ;;
    TERMINATING|TERMINATED) log "ERROR: MicroVM entered ${state} before RUNNING (check /run hook)."; exit 1 ;;
    *)            sleep 5 ;;
  esac
done

log "ERROR: timed out waiting for MicroVM ${microvm_id} to reach RUNNING."
exit 1
