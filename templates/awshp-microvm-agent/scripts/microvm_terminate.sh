#!/usr/bin/env bash
##############################################################################
# microvm_terminate.sh — PROTOTYPE
#
# Terminate the AWS Lambda MicroVM backing this Coder workspace. Called by the
# destroy-time local-exec provisioner in main.tf (workspace stop/delete).
#
# run-microvm has no tags, so we resolve the MicroVM id from the S3 index that
# microvm_run.sh wrote:
#   s3://$MICROVM_STATE_BUCKET/coder-microvm-index/<workspace-id> -> <microvmId>
#
# Best-effort: never fail the destroy (a stuck terminate must not wedge teardown;
# the 8h max-duration cap is the backstop).
##############################################################################
set -uo pipefail
log() { printf '[microvm_terminate] %s\n' "$*" >&2; }

: "${CODER_WS_ID:?}"; : "${AWS_REGION:?}"; : "${MICROVM_STATE_BUCKET:?}"
command -v aws >/dev/null 2>&1 || { log "WARNING: aws CLI missing; MicroVM will expire at 8h cap."; exit 0; }

INDEX_URI="s3://${MICROVM_STATE_BUCKET}/coder-microvm-index/${CODER_WS_ID}"
id="$(aws s3 cp "$INDEX_URI" - --region "$AWS_REGION" 2>/dev/null || true)"

if [ -z "${id:-}" ]; then
  log "No index entry for workspace ${CODER_WS_ID}; nothing to terminate."
  exit 0
fi

log "Terminating MicroVM ${id} (workspace ${CODER_WS_ID})"
aws lambda-microvms terminate-microvm --region "$AWS_REGION" --microvm-identifier "$id" >/dev/null 2>&1 \
  && log "Terminated ${id}." \
  || log "WARNING: terminate-microvm failed for ${id} (will expire at 8h cap)."

# Clean up the index entry regardless.
aws s3 rm "$INDEX_URI" --region "$AWS_REGION" >/dev/null 2>&1 || true
exit 0
