#!/usr/bin/env bash
##############################################################################
# deploy.sh — deploy the coder-microvm-controller Lambda (PROTOTYPE).
#
# Creates/updates:
#   * IAM role coder-microvm-controller-role (trust lambda.amazonaws.com) with
#     lambda-microvms run/get/terminate/list, iam:PassRole on the exec role,
#     s3 get/put/delete on the index prefix, and logs.
#   * Lambda function coder-microvm-controller (python3.12) with a bundled
#     up-to-date boto3/botocore so the brand-new lambda-microvms client model is
#     present regardless of the managed runtime's boto3 version.
#
# Usage:
#   AWS_REGION=us-east-1 BUCKET=coder-microvm-artifacts-<acct>-us-east-1 \
#   EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/coder-microvm-exec-role ./deploy.sh
#
# Prints the function name to set as microvm_controller_function in the template.
##############################################################################
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PAGER=""
R="${AWS_REGION:-us-east-1}"
FN="${FN:-coder-microvm-controller}"
ROLE="${ROLE:-coder-microvm-controller-role}"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
: "${BUCKET:?set BUCKET (S3 index bucket)}"
EXEC_ROLE_ARN="${EXEC_ROLE_ARN:-arn:aws:iam::${ACCT}:role/coder-microvm-exec-role}"

log() { printf '[deploy] %s\n' "$*" >&2; }

# --- IAM role ---------------------------------------------------------------
trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
perms=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["lambda:RunMicrovm","lambda:GetMicrovm","lambda:TerminateMicrovm","lambda:ListMicrovms","lambda:SuspendMicrovm","lambda:ResumeMicrovm","lambda:CreateMicrovmAuthToken"],"Resource":"*"},
 {"Effect":"Allow","Action":["lambda:PassNetworkConnector"],"Resource":"*"},
 {"Effect":"Allow","Action":["iam:PassRole"],"Resource":"${EXEC_ROLE_ARN}"},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":"arn:aws:s3:::${BUCKET}/coder-microvm-index/*"},
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"}
]}
JSON
)
if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  log "role exists: $ROLE"
else
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document "$trust" >/dev/null
  log "created role: $ROLE"
fi
aws iam put-role-policy --role-name "$ROLE" --policy-name controller --policy-document "$perms" >/dev/null
ROLE_ARN="arn:aws:iam::${ACCT}:role/${ROLE}"

# --- package (bundle latest boto3 so lambda-microvms model is present) ------
build="$(mktemp -d)"
cp handler.py "$build/"
log "bundling boto3/botocore into package…"
python3 -m pip install --quiet --target "$build" "boto3>=1.40.0" 2>/dev/null || \
  log "WARN: pip bundle failed; relying on runtime boto3 (may lack lambda-microvms)"
( cd "$build" && zip -qr /tmp/microvm-controller.zip . ) 2>/dev/null || \
  ( cd "$build" && python3 -m zipfile -c /tmp/microvm-controller.zip . )

# --- create/update function -------------------------------------------------
if aws lambda get-function --region "$R" --function-name "$FN" >/dev/null 2>&1; then
  aws lambda update-function-code --region "$R" --function-name "$FN" \
    --zip-file fileb:///tmp/microvm-controller.zip >/dev/null
  log "updated function code: $FN"
else
  # role propagation
  sleep 10
  aws lambda create-function --region "$R" --function-name "$FN" \
    --runtime python3.12 --handler handler.handler --role "$ROLE_ARN" \
    --timeout 120 --memory-size 256 \
    --zip-file fileb:///tmp/microvm-controller.zip >/dev/null
  log "created function: $FN"
fi
aws lambda wait function-updated --region "$R" --function-name "$FN" 2>/dev/null || true

cat <<OUT

# --- set in the template ---
microvm_controller_function = "${FN}"
# (provisioner's AWS identity needs lambda:InvokeFunction on:
#  arn:aws:lambda:${R}:${ACCT}:function:${FN})
OUT
