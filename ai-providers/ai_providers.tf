###########################################################
# Coder AI Gateway — Providers & Agent Models (GitOps)
#
# Declarative replacement for the direct Coder API calls in the CloudFormation
# CodeBuild deploy script (curl to /api/v2/ai/providers and
# /api/experimental/chats/model-configs). Managed with the Coder `coderd`
# Terraform provider (>= 0.0.23), which added first-class resources for the
# AI Gateway:
#
#   coderd_ai_provider          -> AI Gateway providers  (bedrock, openai-compat)
#   coderd_agents_model         -> Coder Agents chat models
#   coderd_default_agents_model -> the default agent model
#
# Requires Coder v2.36.0+ on the server side and Terraform >= 1.11 on the client
# (the provider uses write-only arguments, e.g. api_key_wo / *_wo credentials).
###########################################################

terraform {
  # Write-only arguments (api_key_wo, settings.bedrock.*_wo) require Terraform 1.11+.
  required_version = ">= 1.11.0"
  required_providers {
    coderd = {
      source  = "coder/coderd"
      version = ">= 0.0.23"
    }
  }
}

# --------------------------------------------------------------------------
# Variables (sourced from TF_VAR_* by ai_providers_gitops.sh)
# --------------------------------------------------------------------------

variable "coder_url" {
  type        = string
  description = "Coder deployment URL."
  default     = ""
}

variable "coder_token" {
  type        = string
  description = "Coder session token used to authenticate to the deployment."
  default     = ""
  sensitive   = true
}

variable "bedrock_region" {
  type        = string
  description = "AWS region serving the Bedrock models (Anthropic inference profiles live in us-east-1)."
  default     = "us-east-1"
}

variable "bedrock_endpoint" {
  type        = string
  description = "Base URL for the native Bedrock runtime provider."
  default     = "https://bedrock-runtime.us-east-1.amazonaws.com"
}

variable "bedrock_mantle_endpoint" {
  type        = string
  description = "Base URL for the OpenAI-compatible Bedrock Mantle provider."
  default     = "https://bedrock-mantle.us-east-1.api.aws/v1"
}

variable "bedrock_model" {
  type        = string
  description = "Primary (default) Bedrock model id."
  default     = "global.anthropic.claude-opus-4-6-v1"
}

variable "bedrock_small_fast_model" {
  type        = string
  description = "Small/fast Bedrock model id used for lightweight calls."
  default     = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_mantle_api_key" {
  type        = string
  description = "Bedrock Mantle (OpenAI-compatible) API key — an IAM service-specific credential from Secrets Manager."
  default     = ""
  sensitive   = true
}

variable "bedrock_mantle_api_key_version" {
  type        = number
  description = "Bump to rotate the Bedrock Mantle API key (write-only argument version)."
  default     = 1
}

# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------

provider "coderd" {
  url   = var.coder_url
  token = var.coder_token
}

# --------------------------------------------------------------------------
# AI Gateway providers
# --------------------------------------------------------------------------

# Native AWS Bedrock. Credentials come from the workspace/coder pod IAM role
# (EKS Pod Identity: coder-and-aws-workshop-user), so no static access keys are
# set here — only the model routing settings.
resource "coderd_ai_provider" "bedrock" {
  name         = "bedrock"
  type         = "bedrock"
  display_name = "AWS Bedrock"
  base_url     = var.bedrock_endpoint
  enabled      = true

  settings = {
    bedrock = {
      region           = var.bedrock_region
      model            = var.bedrock_model
      small_fast_model = var.bedrock_small_fast_model
      # protocol defaults to "invoke-model"; credentials via Pod Identity IAM role.
    }
  }
}

# OpenAI-compatible endpoint served by Bedrock Mantle, authenticated with an
# IAM service-specific credential (write-only api key).
resource "coderd_ai_provider" "openai_compat" {
  name         = "openai-compat"
  type         = "openai"
  display_name = "OpenAI via AWS Bedrock"
  base_url     = var.bedrock_mantle_endpoint
  enabled      = true

  api_key_wo         = var.bedrock_mantle_api_key
  api_key_wo_version = var.bedrock_mantle_api_key_version
}

# --------------------------------------------------------------------------
# Coder Agents chat models
# --------------------------------------------------------------------------

resource "coderd_agents_model" "claude_opus" {
  ai_provider_id = coderd_ai_provider.bedrock.id
  model          = var.bedrock_model
  display_name   = "Claude Opus 4.6"
  enabled        = true
  context_limit  = 1000000
  model_config = jsonencode({
    max_output_tokens = 128000
    provider_options = {
      anthropic = {
        send_reasoning = true
      }
    }
  })
}

resource "coderd_agents_model" "claude_haiku" {
  ai_provider_id = coderd_ai_provider.bedrock.id
  model          = var.bedrock_small_fast_model
  display_name   = "Claude Haiku 4.5"
  enabled        = true
  context_limit  = 200000
  model_config = jsonencode({
    max_output_tokens = 64000
    provider_options = {
      anthropic = {
        send_reasoning = true
        thinking = {
          budget_tokens = 8192
        }
      }
    }
  })
}

resource "coderd_agents_model" "gpt_oss_120b" {
  ai_provider_id = coderd_ai_provider.openai_compat.id
  model          = "openai.gpt-oss-120b"
  display_name   = "OpenAI gpt-oss-120b"
  enabled        = true
  context_limit  = 128000
  model_config = jsonencode({
    max_output_tokens = 8192
  })
}

resource "coderd_agents_model" "devstral2" {
  ai_provider_id = coderd_ai_provider.openai_compat.id
  model          = "mistral.devstral-2-123b"
  display_name   = "Devstral 2 123B"
  enabled        = true
  context_limit  = 128000
  model_config = jsonencode({
    max_output_tokens = 8192
  })
}

# Default agent model (was is_default:true on the Opus model config).
resource "coderd_default_agents_model" "default" {
  model_id = coderd_agents_model.claude_opus.id
}
