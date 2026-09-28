# Coder AI Gateway — Providers & Agent Models (GitOps)

Declarative Terraform that configures the Coder **AI Gateway providers** and
**Coder Agents chat models**, replacing the imperative `curl` calls the
CloudFormation deploy script used to make against:

- `POST /api/v2/ai/providers` (and the `PATCH` fallback)
- `POST /api/experimental/chats/model-configs`

## Why Terraform instead of API calls

The [`coderd`](https://registry.terraform.io/providers/coder/coderd) provider
(**≥ 0.0.25** for Coder v2.37; the resources were introduced in 0.0.23) provides
first-class resources for the AI Gateway:

| Resource | Replaces |
|----------|----------|
| `coderd_ai_provider` | `POST/PATCH /api/v2/ai/providers` |
| `coderd_agents_model` | `POST /api/experimental/chats/model-configs` |
| `coderd_agents_default_model` | the `is_default: true` flag on a model |

> **Coder 2.37 / coderd 0.0.25 upgrade.** The default-model resource was renamed
> `coderd_default_agents_model` → `coderd_agents_default_model` and now requires
> `organization_id` (resolved here via the `coderd_organization` data source).
> `coderd_ai_provider` and `coderd_agents_model` are otherwise unchanged for this
> config.

Benefits over the raw API calls:

- **Idempotent & declarative** — re-runs converge instead of relying on
  `POST || PATCH` fallbacks and `|| echo "(may already exist)"`.
- **Schema validation at plan time** — the provider validates `model_config`
  against the Coder SDK `ChatModelCallConfig` schema. (This immediately caught an
  `anthropic.effort` field that the raw API silently dropped.)
- **No provider-ID plumbing** — models reference `coderd_ai_provider.bedrock.id`
  directly instead of curling the provider back to read its `id`.
- **Secrets as write-only args** — the Amazon Bedrock API key is passed via the
  write-only `api_key_wo` argument (never stored in state).

## Requirements

- **Coder v2.37.0+** on the server (Coder Agents GA) with the **coderd provider ≥ 0.0.25**.
- **Terraform ≥ 1.11** on the client — the provider uses *write-only arguments*
  (`api_key_wo`, `settings.bedrock.*_wo`). The wrapper script auto-installs a
  compatible Terraform if the runner's version is older.
- A Coder **session token** with admin rights, and Bedrock model access in
  `us-east-1`.

## What it configures

| Provider (`coderd_ai_provider`) | Type | Notes |
|---|---|---|
| `bedrock` | `bedrock` | Native Bedrock (Anthropic Messages API); credentials via EKS Pod Identity (no static keys). Routes `model` + `small_fast_model`. |
| `openai-compat` | `openai` | Amazon Bedrock **native OpenAI endpoint** (`bedrock-runtime/openai/v1`); authenticated with a write-only Amazon Bedrock API key. Serves models from OpenAI, xAI, Mistral, DeepSeek, Qwen, Moonshot, MiniMax, NVIDIA, and Google via Chat Completions. |

All `openai-compat` models have been validated for **tool/function calling** and
**streaming** — the two hard requirements for Coder Agents — against the Bedrock
`bedrock-runtime` `/openai/v1/chat/completions` endpoint.

### Bedrock native models

| Model (`coderd_agents_model`) | Model ID | Context | Max Output | Default |
|---|---|---|---|---|
| Claude Opus 4.6 | `global.anthropic.claude-opus-4-6-v1` | 1M | 128K | ✅ (`coderd_agents_default_model`) |
| Claude Haiku 4.5 | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | 200K | 64K | |

### OpenAI-compatible models (Bedrock `/openai/v1`)

| Model (`coderd_agents_model`) | Model ID | Context | Max Output | Notes |
|---|---|---|---|---|
| OpenAI GPT-5.6 Sol | `us.openai.gpt-5.6-sol` | 400K | 128K | Cross-region (CRIS). Frontier reasoning + agentic coding. |
| OpenAI GPT-5.6 Terra | `us.openai.gpt-5.6-terra` | 1M | 128K | Cross-region. Balanced cost/performance. |
| OpenAI GPT-5.6 Luna | `us.openai.gpt-5.6-luna` | 1M | 128K | Cross-region. Fast, lowest cost. |
| OpenAI GPT-OSS 120B | `openai.gpt-oss-120b-1:0` | 128K | 16K | Open-source 120B. In-region. |
| OpenAI GPT-OSS 20B | `openai.gpt-oss-20b-1:0` | 128K | 16K | Open-source 20B. In-region, lightweight. |
| xAI Grok 4.6 | `us.xai.grok-4.6` | 256K | 32K | Cross-region. Reasoning model. |
| Mistral Large 3 (675B) | `mistral.mistral-large-3-675b-instruct` | 128K | 32K | Mistral's flagship. |
| Mistral Devstral 2 (123B) | `mistral.devstral-2-123b` | 128K | 32K | Coding-focused. |
| DeepSeek V3.2 | `deepseek.v3.2` | 128K | 32K | Strong reasoning + code. |
| Qwen3 Coder Next | `qwen.qwen3-coder-next` | 128K | 32K | Coding-specialized. |
| Qwen3 32B | `qwen.qwen3-32b-v1:0` | 128K | 32K | General-purpose. |
| Moonshot Kimi K2 Thinking | `moonshot.kimi-k2-thinking` | 128K | 32K | Reasoning with chain-of-thought. |
| MiniMax M2.5 | `minimax.minimax-m2.5` | 128K | 32K | General-purpose. |
| NVIDIA Nemotron Super 3 120B | `nvidia.nemotron-super-3-120b` | 262K | — | 120B-parameter model. |
| Google Gemma 3 12B IT | `google.gemma-3-12b-it` | 128K | — | Lightweight 12B. |

## Usage (mirrors `templates/templates_gitops.sh`)

Run from this directory with the Coder session token as the first argument:

```bash
export CODER_AGENT_URL="https://<your-coder-url>"
export BEDROCK_REGION="us-east-1"
export BEDROCK_OPENAI_KEY="<amazon-bedrock-api-key>"   # ABSK... from Secrets Manager
./ai_providers_gitops.sh "<coder-session-token>"
```

The wrapper maps the environment to `TF_VAR_*`, ensures Terraform ≥ 1.11, and runs
`terraform init && terraform apply -auto-approve` with retries.

### Manual apply

```bash
export TF_VAR_coder_url="https://<your-coder-url>"
export TF_VAR_coder_token="<session-token>"
export TF_VAR_bedrock_openai_api_key="<amazon-bedrock-api-key>"
terraform init
terraform apply
```

## CloudFormation integration

`infrastructure/coder_deployment.yaml` invokes `ai_providers_gitops.sh` in the
CodeBuild deploy step (after creating the Amazon Bedrock API key),
in place of the previous `curl` provider/model calls.

## Coder Agents MCP servers (`coderd_agents_mcp_server`)

Coder Agents can be given external **MCP servers** (AI Settings > Coder Agents >
MCP servers, `/ai/settings/mcp-servers`). As of coderd **≥ 0.0.25** (Coder 2.37)
these are a first-class Terraform resource, so they are managed declaratively in
`ai_providers.tf` alongside the providers and models — no separate script. (This
replaced the former `ai_mcp_servers_gitops.sh`, which made a direct
`POST /api/v2/organizations/{org}/mcp-servers` admin-API call.)

The `coderd_agents_mcp_server.aws_knowledge` resource registers the **AWS
Knowledge MCP Server** — a remote, AWS-hosted, no-install/no-credentials server
exposing AWS docs, API references, What's New, Builder Center, and
Well-Architected guidance (ideal for Citizen Developers/Builders):

| Field | Value |
|---|---|
| `display_name` | `AWS Knowledge` |
| `slug` | `aws-knowledge` (var `mcp_knowledge_slug`, env `MCP_KNOWLEDGE_SLUG`) |
| `transport` | `streamable_http` |
| `url` | `https://knowledge-mcp.global.api.aws` (var `mcp_knowledge_url`, env `MCP_KNOWLEDGE_URL`) |
| `auth_type` | `none` |
| `availability` | `default_on` (var `mcp_knowledge_availability`, env `MCP_KNOWLEDGE_AVAIL`) |
| `model_intent` | `true` (surfaces each tool call's purpose in Coder AI Session logs) |

Because it is now Terraform, the apply is idempotent (state-reconciled by slug)
and `coderd_agents_mcp_server` also exposes governance not available via the raw
API call: `tool_allow_list` / `tool_deny_list`, `allow_in_plan_mode`,
`forward_coder_headers`, and write-only auth args (`api_key_value_wo`,
`oauth2_client_secret_wo`, `custom_headers_wo`) for authenticated servers.
coderd 0.0.25 also adds `coderd_agents_system_prompt` for setting the Coder
Agents system prompt declaratively (not configured here).

> **Requires Coder v2.37.0+ with the AI Governance Add-On.** Unlike the former
> best-effort script (which no-op'd on deployments without the MCP-servers API),
> the Terraform apply will fail if the API/add-on is absent — acceptable now that
> 2.37 GA is the deploy target. This differs from the workspace-level MCP servers
> in the templates (Kiro / Claude Code `mcp.json`): those are per-workspace tools
> for the CLI assistants, whereas this registers a server for the server-side
> **Coder Agents** chat.
