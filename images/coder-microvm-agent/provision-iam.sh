#!/usr/bin/env bash
##############################################################################
# provision-iam.sh — create the prerequisites for building/running the
# coder-microvm-agent image: an S3 artifact bucket and the build + execution
# IAM roles (both trusting lambda.amazonaws.com with an aws:SourceAccount
# confused-deputy guard).
#
# Idempotent. Prints the ARNs to export for build.sh / the Terraform template.
#
# Usage:  AWS_REGION=us-east-1 ./provision-iam.sh
##############################################################################
set -euo pipefail
export AWS_PAGER=""
R="${AWS_REGION:-us-east-1}"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${BUCKET:-coder-microvm-artifacts-${ACCT}-${R}}"
BUILD_ROLE="${BUILD_ROLE:-coder-microvm-build-role}"
EXEC_ROLE="${EXEC_ROLE:-coder-microvm-exec-role}"

log() { printf '[provision-iam] %s\n' "$*" >&2; }

# --- S3 artifact bucket (must be in the image region) -----------------------
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  log "bucket exists: $BUCKET"
else
  if [ "$R" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$R" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$R" \
      --create-bucket-configuration "LocationConstraint=$R" >/dev/null
  fi
  log "created bucket: $BUCKET"
fi

trust=$(cat <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole",
 "Condition":{"StringEquals":{"aws:SourceAccount":"${ACCT}"}}}]}
JSON
)
build_perms=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::${BUCKET}/*"},
 {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::${BUCKET}"},
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"}
]}
JSON
)
# Execution role = the workspace's AWS identity inside the MicroVM (replaces
# EKS IRSA). Scope to what the agent/AWS MCP actually needs (Bedrock + logs +
# EFS). Tighten for production.
exec_perms=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"},
 {"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream","bedrock:Converse","bedrock:ConverseStream"],"Resource":"*"},
 {"Effect":"Allow","Action":["elasticfilesystem:ClientMount","elasticfilesystem:ClientWrite","elasticfilesystem:DescribeMountTargets"],"Resource":"*"}
]}
JSON
)

ensure_role() {
  local role="$1" perms="$2" pname="$3"
  if aws iam get-role --role-name "$role" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$role" --policy-document "$trust" >/dev/null
    log "role exists (trust updated): $role"
  else
    aws iam create-role --role-name "$role" --assume-role-policy-document "$trust" >/dev/null
    log "created role: $role"
  fi
  aws iam put-role-policy --role-name "$role" --policy-name "$pname" --policy-document "$perms" >/dev/null
}

ensure_role "$BUILD_ROLE" "$build_perms" build
ensure_role "$EXEC_ROLE"  "$exec_perms"  exec

cat <<OUT

# --- exports for build.sh / the Terraform template ---
export AWS_REGION="$R"
export BUCKET="$BUCKET"
export BUILD_ROLE_ARN="arn:aws:iam::${ACCT}:role/${BUILD_ROLE}"
# microvm_execution_role_arn for the template:
#   arn:aws:iam::${ACCT}:role/${EXEC_ROLE}
OUT
