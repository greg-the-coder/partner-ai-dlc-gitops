#!/usr/bin/env bash
##############################################################################
# build.sh — build the coder-microvm-agent image into a Firecracker snapshot.
#
# Packages this directory (Dockerfile + hooks/) as a zip, uploads it to S3, and
# calls `aws lambda-microvms create-microvm-image` (or update-microvm-image for a
# new version), then waits for the version build to reach SUCCESSFUL.
#
# Prereqs (create once; see also provision-iam.sh):
#   * S3 bucket in the SAME region as the image (cross-region is rejected).
#   * Build IAM role trusting lambda.amazonaws.com with s3:GetObject on the
#     artifact + logs:CreateLogGroup/Stream/PutLogEvents.
#
# Usage:
#   BUCKET=my-bucket BUILD_ROLE_ARN=arn:aws:iam::<acct>:role/coder-microvm-build-role \
#     ./build.sh [image-name]
#
# Env (with defaults):
#   AWS_REGION            us-east-1
#   IMAGE_NAME            coder-microvm-agent   (arg 1 overrides)
#   BUCKET                (required)
#   BUILD_ROLE_ARN        (required)
#   BASE_IMAGE_ARN        arn:aws:lambda:<region>:aws:microvm-image:al2023-1
#   ARCH                  X86_64 | ARM_64        (default X86_64)
#   MEM_MIB               4096                   (single-size image)
#   HOOK_PORT             9000
##############################################################################
set -euo pipefail
cd "$(dirname "$0")"

AWS_REGION="${AWS_REGION:-us-east-1}"
IMAGE_NAME="${1:-${IMAGE_NAME:-coder-microvm-agent}}"
BASE_IMAGE_ARN="${BASE_IMAGE_ARN:-arn:aws:lambda:${AWS_REGION}:aws:microvm-image:al2023-1}"
ARCH="${ARCH:-X86_64}"
MEM_MIB="${MEM_MIB:-4096}"
HOOK_PORT="${HOOK_PORT:-9000}"
export AWS_PAGER=""

: "${BUCKET:?set BUCKET to an S3 bucket in ${AWS_REGION}}"
: "${BUILD_ROLE_ARN:?set BUILD_ROLE_ARN to the lambda-trusted build role}"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
IMAGE_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT}:microvm-image:${IMAGE_NAME}"
KEY="microvm-images/${IMAGE_NAME}/code-artifact-$(date +%s).zip"

log() { printf '[build] %s\n' "$*" >&2; }

# --- 1. Package Dockerfile + hooks/ at the zip root -------------------------
tmpzip="$(mktemp --suffix=.zip)"
trap 'rm -f "$tmpzip"' EXIT
log "Packaging Dockerfile + hooks/ -> $tmpzip"
# Portable packaging: prefer `zip`, else fall back to Python's stdlib zipfile
# (Dockerfile must be at the zip ROOT).
if command -v zip >/dev/null 2>&1; then
  rm -f "$tmpzip"
  zip -q -r "$tmpzip" Dockerfile hooks -x '*/__pycache__/*'
else
  python3 - "$tmpzip" <<'PY'
import os, sys, zipfile
out = sys.argv[1]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write("Dockerfile", "Dockerfile")
    for root, _, files in os.walk("hooks"):
        if "__pycache__" in root:
            continue
        for f in files:
            p = os.path.join(root, f)
            z.write(p, p)
print("packaged", out)
PY
fi

log "Uploading to s3://${BUCKET}/${KEY}"
aws s3 cp "$tmpzip" "s3://${BUCKET}/${KEY}" --region "$AWS_REGION" >/dev/null

# --- 2. Assemble create/update args -----------------------------------------
HOOKS_JSON=$(cat <<JSON
{
  "port": ${HOOK_PORT},
  "microvmImageHooks": {
    "ready": "ENABLED", "readyTimeoutInSeconds": 60,
    "validate": "ENABLED", "validateTimeoutInSeconds": 30
  },
  "microvmHooks": {
    "run": "ENABLED", "runTimeoutInSeconds": 10,
    "resume": "ENABLED", "resumeTimeoutInSeconds": 5,
    "suspend": "ENABLED", "suspendTimeoutInSeconds": 5,
    "terminate": "ENABLED", "terminateTimeoutInSeconds": 5
  }
}
JSON
)

common_args=(
  --region "$AWS_REGION"
  --base-image-arn "$BASE_IMAGE_ARN"
  --build-role-arn "$BUILD_ROLE_ARN"
  --code-artifact "{\"uri\":\"s3://${BUCKET}/${KEY}\"}"
  --cpu-configurations "[{\"architecture\":\"${ARCH}\"}]"
  --resources "[{\"minimumMemoryInMiB\":${MEM_MIB}}]"
  --additional-os-capabilities '["ALL"]'
  --hooks "$HOOKS_JSON"
)

# --- 3. Create (first time) or update (new version) -------------------------
if aws lambda-microvms get-microvm-image --region "$AWS_REGION" \
      --image-identifier "$IMAGE_ARN" >/dev/null 2>&1; then
  log "Image exists — creating a NEW VERSION via update-microvm-image"
  aws lambda-microvms update-microvm-image --image-identifier "$IMAGE_ARN" "${common_args[@]}" >/dev/null
else
  log "Creating image ${IMAGE_NAME}"
  aws lambda-microvms create-microvm-image --name "$IMAGE_NAME" \
    --description "Coder workspace agent MicroVM (prototype)" "${common_args[@]}" >/dev/null
fi

# --- 4. Wait for the newest version build to finish -------------------------
log "Waiting for the snapshot build to finish (this can take several minutes)…"
# Determine the version that is actually building now (create/update always adds
# a new version). Do NOT trust a single "latest" field — list versions and pick
# the one in PENDING/IN_PROGRESS, else the highest-numbered version.
version="$(aws lambda-microvms list-microvm-image-versions --region "$AWS_REGION" \
  --image-identifier "$IMAGE_ARN" \
  --query 'items[?state==`IN_PROGRESS`||state==`PENDING`].imageVersion | [0]' \
  --output text 2>/dev/null || echo '')"
if [ -z "$version" ] || [ "$version" = "None" ]; then
  version="$(aws lambda-microvms list-microvm-image-versions --region "$AWS_REGION" \
    --image-identifier "$IMAGE_ARN" \
    --query 'reverse(sort_by(items,&imageVersion))[0].imageVersion' \
    --output text 2>/dev/null || echo '1.0')"
fi
log "Tracking version ${version}"

for _ in $(seq 1 120); do
  state="$(aws lambda-microvms list-microvm-image-versions --region "$AWS_REGION" \
    --image-identifier "$IMAGE_ARN" \
    --query "items[?imageVersion=='${version}'].state | [0]" --output text 2>/dev/null || echo '')"
  case "$state" in
    SUCCESSFUL) log "Build SUCCESSFUL. image=${IMAGE_ARN} version=${version}";
                echo "IMAGE_ARN=${IMAGE_ARN}"; echo "IMAGE_VERSION=${version}"; exit 0 ;;
    FAILED)     log "Build FAILED:";
                aws lambda-microvms list-microvm-image-builds --region "$AWS_REGION" \
                  --image-identifier "$IMAGE_ARN" --image-version "$version" 2>/dev/null \
                  | jq -c '.items[]? | {buildState, stateReason, architecture}' >&2 || true
                exit 1 ;;
    *)          sleep 10 ;;
  esac
done
log "ERROR: timed out waiting for build of ${IMAGE_ARN} ${version}"; exit 1
