# Coder AI Gateway — Providers & Agent Models (GitOps)

Declarative Terraform that configures the Coder **AI Gateway providers** and
**Coder Agents chat models**, replacing the imperative `curl` calls the
CloudFormation deploy script used to make against:

- `POST /api/v2/ai/providers` (and the `PATCH` fallback)
- `POST /api/experimental/chats/model-configs`

## Why Terraform instead of API calls

The [`coderd`](https://registry.terraform.io/providers/coder/coderd) provider
(**≥ 0.0.23**) added first-class resources for the AI Gateway:

| Resource | Replaces |
|----------|----------|
| `coderd_ai_provider` | `POST/PATCH /api/v2/ai/providers` |
| `coderd_agents_model` | `POST /api/experimental/chats/model-configs` |
| `coderd_default_agents_model` | the `is_default: true` flag on a model |

Benefits over the raw API calls:

- **Idempotent & declarative** — re-runs converge instead of relying on
  `POST || PATCH` fallbacks and `|| echo "(may already exist)"`.
- **Schema validation at plan time** — the provider validates `model_config`
  against the Coder SDK `ChatModelCallConfig` schema. (This immediately caught an
  `anthropic.effort` field that the raw API silently dropped.)
- **No provider-ID plumbing** — models reference `coderd_ai_provider.bedrock.id`
  directly instead of curling the provider back to read its `id`.
- **Secrets as write-only args** — the Bedrock Mantle API key is passed via the
  write-only `api_key_wo` argument (never stored in state).

## Requirements

- **Coder v2.36.0+** on the server (AI Gateway resources).
- **Terraform ≥ 1.11** on the client — the provider uses *write-only arguments*
  (`api_key_wo`, `settings.bedrock.*_wo`). The wrapper script auto-installs a
  compatible Terraform if the runner's version is older.
- A Coder **session token** with admin rights, and Bedrock model access in
  `us-east-1`.

## What it configures

| Provider (`coderd_ai_provider`) | Type | Notes |
|---|---|---|
| `bedrock` | `bedrock` | Native Bedrock; credentials via EKS Pod Identity (no static keys). Routes `model` + `small_fast_model`. |
| `openai-compat` | `openai` | Bedrock Mantle (OpenAI-compatible); authenticated with a write-only API key. |

| Model (`coderd_agents_model`) | Provider | Default |
|---|---|---|
| Claude Opus 4.6 | bedrock | ✅ (`coderd_default_agents_model`) |
| Claude Haiku 4.5 | bedrock | |
| OpenAI gpt-oss-120b | openai-compat | |
| Devstral 2 123B | openai-compat | |

## Usage (mirrors `templates/templates_gitops.sh`)

Run from this directory with the Coder session token as the first argument:

```bash
export CODER_AGENT_URL="https://<your-coder-url>"
export BEDROCK_REGION="us-east-1"
export BEDROCK_MANTLE_KEY="<mantle-service-credential>"   # from Secrets Manager
./ai_providers_gitops.sh "<coder-session-token>"
```

The wrapper maps the environment to `TF_VAR_*`, ensures Terraform ≥ 1.11, and runs
`terraform init && terraform apply -auto-approve` with retries.

### Manual apply

```bash
export TF_VAR_coder_url="https://<your-coder-url>"
export TF_VAR_coder_token="<session-token>"
export TF_VAR_bedrock_mantle_api_key="<mantle-key>"
terraform init
terraform apply
```

## CloudFormation integration

`infrastructure/coder_deployment.yaml` invokes `ai_providers_gitops.sh` in the
CodeBuild deploy step (after creating the Bedrock Mantle service credential),
in place of the previous `curl` provider/model calls.
