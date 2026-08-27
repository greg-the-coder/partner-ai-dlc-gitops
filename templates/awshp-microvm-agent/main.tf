##############################################################################
# awshp-microvm-agent — PROTOTYPE
#
# A SIMPLIFIED, MicroVM-only variant of `awshp-k8s-challenge-agent`, built to
# test/validate the AWS Lambda MicroVMs integration as a Coder workspace compute
# lane (see docs/lambda-microvm-mvp-plan.md).
#
# What is the same as the challenge-agent template:
#   * coder_agent + Coder AI Gateway env wiring (Bedrock / openai-compat)
#   * coder-login, code-server, and the built-in Coder Agents harness
#   * per-workspace EFS access point for the persistent home directory
#
# What is DIFFERENT (the point of this prototype):
#   * The workspace is NOT a Kubernetes Deployment. It is an AWS Lambda MicroVM
#     provisioned via the `aws lambda-microvms` API from a null_resource
#     (there is no first-class Terraform resource for the service yet).
#   * Workspace AWS identity comes from the MicroVM executionRoleArn instead of
#     the EKS pod service account / IRSA.
#   * The Coder agent token + init script are injected at run time via the
#     MicroVM `/run` lifecycle hook payload (kept OUT of the image snapshot,
#     whose memory state is shared across runs).
#   * EFS is mounted IN-GUEST by the /run hook (needs the image built with
#     additional-os-capabilities=ALL and a VPC_EGRESS connector), not via the
#     Kubernetes EFS CSI driver.
#
# HARD CONSTRAINTS (enforced/observed here):
#   * 8h max MicroVM lifetime  -> var.microvm_max_duration_seconds capped 28800.
#   * Commercial regions only  -> NOT for GovCloud deployments.
#   * Idle is measured by INBOUND proxy traffic only, so this MVP does NOT set
#     an idlePolicy; stop = terminate. (Fast-follow: harness-driven suspend.)
#
# STATUS: prototype. The `aws lambda-microvms` CLI shape is per the AWS docs at
# time of writing; flags marked TODO below need a live-account smoke test.
##############################################################################

terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = ">= 2.13"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.2"
    }
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.44"
    }
  }
}

# The AWS provider is used to (a) manage the per-workspace EFS access point and
# (b) invoke the coder-microvm-controller Lambda. Credentials come from the
# Coder provisioner's environment (the same identity the K8s templates use to
# create aws_efs_access_point). The provisioner needs lambda:InvokeFunction on
# the controller — it does NOT need the aws CLI.
provider "aws" {
  region = var.aws_region
}

##############################################################################
# Variables — MicroVM plumbing (set at template push / per-deployment)
##############################################################################

variable "aws_region" {
  type        = string
  description = "Commercial AWS region where Lambda MicroVMs, the S3 artifact bucket, the MicroVM image, and the network connectors all live. Lambda MicroVMs is NOT available in GovCloud."
  default     = "us-east-1"
}

variable "microvm_image_identifier" {
  type        = string
  description = "ARN of the MicroVM image built from images/coder-microvm-agent (see the image build in Phase 1 of the MVP plan). e.g. arn:aws:lambda:us-east-1:<acct>:microvm-image:coder-microvm-agent"
}

variable "microvm_image_version" {
  type        = string
  description = "MicroVM image version (major.minor) to run. Empty resolves the ACTIVE version."
  default     = ""
}

variable "microvm_execution_role_arn" {
  type        = string
  description = "IAM role assumed by the running MicroVM. This is the workspace's AWS identity (replaces EKS IRSA) — grant it Bedrock invoke + the AWS MCP scope + EFS access. Region/account-scoped, with aws:SourceAccount/aws:SourceArn conditions."
}

variable "microvm_egress_connector_arn" {
  type        = string
  description = "Optional VPC_EGRESS network connector ARN so the MicroVM can reach EFS mount targets (2049) and a private coderd. Leave empty to use default public internet egress."
  default     = ""
}

variable "microvm_controller_function" {
  type        = string
  description = "Name (or ARN) of the coder-microvm-controller Lambda that runs/terminates the MicroVM (see infrastructure/microvm-controller). Invoked by Terraform so the provisioner needs NO aws CLI."
  default     = "coder-microvm-controller"
}

variable "microvm_state_bucket" {
  type        = string
  description = "S3 bucket used to index workspace-id -> microvmId (run-microvm has no tags). Typically the same artifact bucket used to build the image."
}

variable "efs_file_system_id" {
  type        = string
  description = "EFS file system ID backing the persistent workspace home directory."
  default     = ""
}

variable "microvm_max_duration_seconds" {
  type        = number
  description = "Hard wall-clock lifetime for the MicroVM. AWS caps this at 28800 (8h)."
  default     = 28800
  validation {
    condition     = var.microvm_max_duration_seconds > 0 && var.microvm_max_duration_seconds <= 28800
    error_message = "Lambda MicroVMs cap the maximum duration at 28800 seconds (8 hours)."
  }
}

locals {
  home_dir = "/home/coder"
  bin_path = "/home/coder/.local/bin:/home/coder/bin:/home/coder/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  cost     = 2

  # EFS mount target for the per-workspace access point, mounted in-guest by the
  # /run hook. NFS DNS name is <fs-id>.efs.<region>.amazonaws.com.
  efs_dns = var.efs_file_system_id == "" ? "" : "${var.efs_file_system_id}.efs.${var.aws_region}.amazonaws.com"
}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

##############################################################################
# Sizing parameters (mapped to MicroVM --resources). MicroVM caps: 16 vCPU / 32GB.
##############################################################################

data "coder_parameter" "cpu" {
  name        = "CPU cores"
  type        = "number"
  description = "vCPUs for your MicroVM workspace (max 16)."
  icon        = "/icon/aws.png"
  validation {
    min = 2
    max = 16
  }
  form_type = "input"
  mutable   = true
  default   = 4
  order     = 1
}

data "coder_parameter" "memory" {
  name        = "Memory (GB)"
  type        = "number"
  description = "Memory in GB for your MicroVM workspace (max 32)."
  icon        = "/icon/aws.png"
  validation {
    min = 4
    max = 32
  }
  form_type = "input"
  mutable   = true
  default   = 8
  order     = 2
}

# Retained for parity with the challenge-agent template's compute_lane UX, but
# this prototype template only implements the "microvm" lane. Fargate/Spot live
# in the K8s templates.
data "coder_parameter" "compute_lane" {
  name         = "Compute Lane"
  display_name = "Compute Lane"
  description  = "microvm = AWS Lambda MicroVM (Firecracker/Graviton ARM64, snapshot-instant start, suspend/resume, 8h max session). Ephemeral agent runs only; commercial regions only (no GovCloud)."
  type         = "string"
  default      = "microvm"
  mutable      = false
  order        = 3
  icon         = "/icon/aws.png"
  option {
    name  = "Lambda MicroVM (isolated, snapshot-instant)"
    value = "microvm"
    icon  = "/icon/aws.png"
  }
}

##############################################################################
# Coder agent + AI Gateway wiring (unchanged from the challenge-agent template)
##############################################################################

resource "coder_env" "path" {
  agent_id = coder_agent.dev.id
  name     = "PATH"
  value    = local.bin_path
}

# Route agent-framework SDK LLM calls through the Coder AI Gateway (governed by
# the AI Governance Add-On). Pure outbound HTTPS to coderd — works over MicroVM
# egress exactly as it does from a pod. Routes by PROVIDER NAME (bedrock /
# openai-compat), not API type. Requires Coder v2.32+ with the AI Gateway.
resource "coder_env" "anthropic_base_url" {
  agent_id = coder_agent.dev.id
  name     = "ANTHROPIC_BASE_URL"
  value    = "${trimsuffix(data.coder_workspace.me.access_url, "/")}/api/v2/ai-gateway/bedrock"
}

resource "coder_env" "anthropic_api_key" {
  agent_id = coder_agent.dev.id
  name     = "ANTHROPIC_API_KEY"
  value    = data.coder_workspace_owner.me.session_token
}

resource "coder_env" "openai_base_url" {
  agent_id = coder_agent.dev.id
  name     = "OPENAI_BASE_URL"
  value    = "${trimsuffix(data.coder_workspace.me.access_url, "/")}/api/v2/ai-gateway/openai-compat/v1"
}

resource "coder_env" "openai_api_key" {
  agent_id = coder_agent.dev.id
  name     = "OPENAI_API_KEY"
  value    = data.coder_workspace_owner.me.session_token
}

resource "coder_agent" "dev" {
  # Lambda MicroVMs currently build/run on ARM_64 (Graviton) ONLY, so the Coder
  # agent must be arm64. (Validated: X86_64 create-microvm-image is rejected.)
  arch = "arm64"
  os   = "linux"

  display_apps {
    vscode          = false
    vscode_insiders = false
    web_terminal    = true
    ssh_helper      = false
  }

  metadata {
    display_name = "CPU Usage"
    key          = "0_cpu_usage"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }
  metadata {
    display_name = "RAM Usage"
    key          = "1_ram_usage"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }
  metadata {
    display_name = "Home Disk"
    key          = "2_home_disk"
    script       = "coder stat disk --path $HOME"
    interval     = 60
    timeout      = 1
  }

  startup_script = <<-EOT
    set -e
    mkdir -p $HOME/bin $HOME/.local/bin
    export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.npm-global/bin:$PATH"
    ln -sf /tmp/coder.*/coder "$CODER_SCRIPT_BIN_DIR/coder" 2>/dev/null || true
  EOT
}

module "coder-login" {
  source   = "registry.coder.com/coder/coder-login/coder"
  version  = "1.1.1"
  agent_id = coder_agent.dev.id
}

module "code-server" {
  source     = "registry.coder.com/coder/code-server/coder"
  version    = "1.5.2"
  agent_id   = coder_agent.dev.id
  folder     = local.home_dir
  subdomain  = false
  order      = 0
  extensions = ["ms-toolsai.jupyter"]
}

##############################################################################
# Per-workspace EFS access point (persistent home). Same pattern as the K8s
# templates; here it is mounted IN-GUEST by the /run hook (not via CSI).
##############################################################################

resource "aws_efs_access_point" "home" {
  count          = var.efs_file_system_id == "" ? 0 : 1
  file_system_id = var.efs_file_system_id

  posix_user {
    uid = 1000
    gid = 1000
  }
  root_directory {
    path = "/workspaces/${data.coder_workspace.me.id}"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }
  tags = {
    Name                     = "coder-${data.coder_workspace.me.name}-home"
    "com.coder.workspace.id" = data.coder_workspace.me.id
  }
}

##############################################################################
# The MicroVM itself.
#
# No first-class Terraform resource exists for lambda-microvms, and the Coder
# provisioner has no aws CLI. So instead of a null_resource + local-exec, we
# invoke the coder-microvm-controller Lambda via `aws_lambda_invocation` with
# `lifecycle_scope = "CRUD"`: Terraform calls it on create/update (run or reuse
# the workspace's MicroVM) and on destroy (terminate it), injecting a `tf.action`
# the controller switches on. This needs only the AWS provider (already
# authenticated) + lambda:InvokeFunction — NO CLI, NO boto3 on the provisioner.
#
# The controller uses boto3 to call lambda-microvms and maintains the
# workspace-id -> microvmId index in S3 (run-microvm has no tags). The Coder
# agent token/URL/init script are passed in the invocation input (sensitive,
# stored in state) and forwarded to the guest via the /run hook payload.
#
# NB: the scripts/ dir (microvm_run.sh / microvm_terminate.sh) is retained as an
# alternative for deployments that prefer an EXTERNAL provisioner WITH the aws
# CLI; this template uses the Lambda path.
##############################################################################

resource "aws_lambda_invocation" "microvm" {
  count           = data.coder_workspace.me.start_count
  function_name   = var.microvm_controller_function
  lifecycle_scope = "CRUD" # invoke on create/update AND destroy (tf.action)

  input = jsonencode({
    workspace_id         = data.coder_workspace.me.id
    state_bucket         = var.microvm_state_bucket
    image_identifier     = var.microvm_image_identifier
    image_version        = var.microvm_image_version
    execution_role_arn   = var.microvm_execution_role_arn
    egress_connector_arn = var.microvm_egress_connector_arn
    max_duration         = var.microvm_max_duration_seconds
    efs_dns              = local.efs_dns
    efs_access_point_id  = var.efs_file_system_id == "" ? "" : aws_efs_access_point.home[0].id
    coder_agent_token    = coder_agent.dev.token
    coder_agent_url      = data.coder_workspace.me.access_url
    coder_agent_init_b64 = base64encode(coder_agent.dev.init_script)
  })
}

locals {
  microvm_result = try(jsondecode(aws_lambda_invocation.microvm[0].result), {})
}

resource "coder_metadata" "microvm_info" {
  count       = data.coder_workspace.me.start_count
  resource_id = aws_lambda_invocation.microvm[0].id
  daily_cost  = local.cost
  item {
    key   = "compute"
    value = "AWS Lambda MicroVM (Firecracker/Graviton)"
  }
  item {
    key   = "microvm id"
    value = try(local.microvm_result.microvm_id, "n/a")
  }
  item {
    key   = "region"
    value = var.aws_region
  }
  item {
    key   = "image"
    value = "${var.microvm_image_identifier}@${var.microvm_image_version}"
  }
  item {
    key   = "max session"
    value = "${var.microvm_max_duration_seconds}s (8h cap)"
  }
}
