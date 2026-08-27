#!/usr/bin/env bash
##############################################################################
# microvm_terminate.sh — PROTOTYPE
#
# Terminate the AWS Lambda MicroVM(s) backing this Coder workspace. Called by
# the destroy-time local-exec provisioner in main.tf (workspace stop/delete).
# Terminates by workspace tag so no MicroVM id needs to be persisted in state.
#
# Best-effort: never fail the destroy (a stuck terminate should not wedge the
# workspace teardown). The 8h max-duration cap is the backstop if this misses.
##############################################################################
set -uo pipefail

log() { printf '[microvm_terminate] %s\n' "$*" >&2; }

: "${CODER_WS_ID:?}"; : "${AWS_REGION:?}"

if ! command -v aws >/dev/null 2>&1; then
  log "WARNING: aws CLI not found; cannot terminate MicroVM (will expire at 8h cap)."
  exit 0
fi

ids="$(aws lambda-microvms list-microvms --region "$AWS_REGION" \
  --query "microvms[?tags.\"com.coder.workspace.id\"=='${CODER_WS_ID}' && state!='TERMINATED'].microvmId" \
  --output text 2>/dev/null || echo '')"

if [ -z "${ids:-}" ]; then
  log "No active MicroVM found for workspace ${CODER_WS_ID}; nothing to terminate."
  exit 0
fi

for id in $ids; do
  log "Terminating MicroVM ${id} (workspace ${CODER_WS_ID})"
  aws lambda-microvms terminate-microvm --region "$AWS_REGION" \
    --microvm-identifier "$id" >/dev/null 2>&1 \
    && log "Terminated ${id}." \
    || log "WARNING: terminate-microvm failed for ${id} (will expire at 8h cap)."
done

exit 0
