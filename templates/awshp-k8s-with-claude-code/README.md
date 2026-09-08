---
display_name: AWS Workshop - Kubernetes with Claude Code
description: Fargate Claude Code workspace routed through the Coder AI Gateway, with AWS Labs MCP servers, AWS CLI/CDK, Node.js, and Amazon Bedrock access.
icon: ../../../site/static/icon/k8s.png
maintainer_github: coder
verified: true
tags: [kubernetes, fargate, ai, claude, claude-code, coder-ai-gateway, bedrock]
---

# Kubernetes with Claude Code

A serverless Coder workspace running on **AWS Fargate** with the
[Claude Code](https://coder.com/docs/claude-code) AI assistant. The home directory is
persisted on **Amazon EFS** so work survives workspace restarts.

## Capabilities

### AI assistant
- **Claude Code** CLI (`@anthropic-ai/claude-code`), opened from the **Claude Code** app
  tile (a launcher `coder_app`) or the web terminal. Coder Tasks integration was dropped in
  the Coder 2.36.0 upgrade (and by claude-code module v5), so Claude Code runs as an
  interactive CLI rather than a Tasks web app.
- **Coder AI Gateway** routing — Claude Code *and* the notebook SDKs send every model
  request through the **Coder AI Gateway**
  (`ANTHROPIC_BASE_URL = <access_url>/api/v2/ai-gateway/bedrock`), authenticated with the
  user's Coder session token. The gateway forwards to the admin-configured Amazon Bedrock
  provider (default **Claude Opus 4.6**, `global.anthropic.claude-opus-4-6-v1`) using the
  control plane's centrally-held credentials — no AWS keys or `CLAUDE_CODE_USE_BEDROCK` in
  the workspace — so all usage is governed and observable by the **Coder AI Governance
  Add-On** (prompts, spend, and tool calls appear in Coder AI Session logs).
  > Requires Coder v2.32+ with the Coder AI Governance Add-On enabled on the deployment.
- **MCP** (Model Context Protocol) — a citizen-builder set of
  [AWS Labs MCP servers](https://github.com/awslabs/mcp) is added to Claude Code at user
  scope and run on demand via `uvx`: AWS **documentation**, **IaC** (CloudFormation + CDK),
  **pricing**, **API** (`call_aws`), **Serverless**, and **CloudWatch** — covering the
  learn → design → cost → build/deploy → operate lifecycle. Calls use the workspace IAM role.

### Notebooks & agent SDKs (Coder AI Gateway)
The template points the Python agent SDKs at the **Coder AI Gateway** (via agent-wide
`coder_env`) so notebook LLM calls are governed by the **Coder AI Governance Add-On** too.
The agent kernel (`Python (Agents)`) inherits:

- `ANTHROPIC_BASE_URL` → `<access_url>/api/v2/ai-gateway/bedrock`
- `OPENAI_BASE_URL` → `<access_url>/api/v2/ai-gateway/openai-compat/v1`

- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` → the user's Coder session token

> The path segment (`bedrock` / `openai-compat`) is the **AI Gateway provider name**
> from `ai-providers/`, not the API type — the gateway routes
> `/api/v2/ai-gateway/<provider-name>/`.

So Anthropic- and OpenAI-protocol clients route through the gateway with no extra
config:

```python
# Anthropic protocol -> gateway -> Bedrock provider
from langchain_anthropic import ChatAnthropic
llm = ChatAnthropic(model="global.anthropic.claude-opus-4-6-v1")

# OpenAI protocol -> gateway -> Bedrock (OpenAI-compatible) provider
from langchain_openai import ChatOpenAI
llm = ChatOpenAI(model="us.openai.gpt-5.6-sol")   # or "us.xai.grok-4.6"
```

> **Bedrock SigV4 is not gateway-routable.** The AI Gateway only exposes OpenAI- and
> Anthropic-compatible endpoints, so `boto3` `bedrock-runtime`, `langchain-aws`
> `ChatBedrock`, and `llama-index-llms-bedrock` still call Amazon Bedrock **directly**
> via the workspace IAM role. Use the Anthropic/OpenAI clients above to route a
> notebook through the gateway.

### Developer environment
- **code-server** (VS Code in the browser) and **Kiro IDE** web app
- Web terminal
- Node.js 20 LTS, AWS CLI v2, AWS CDK
- Playwright (headless Chromium) for web access
- Python 3

## Runtime & infrastructure
- **Compute:** AWS Fargate (namespace `coder-ws`), no EC2 worker nodes
- **Storage:** Amazon EFS access point mounted at `/home/coder` (`ReadWriteMany`, persistent)
- **Image:** [`images/coder-workspace-claude-code/Dockerfile`](../../images/coder-workspace-claude-code/Dockerfile)

## Parameters

| Parameter | Default | Range |
|-----------|---------|-------|
| CPU cores | 4 | 2–8 |
| Memory (GB) | 8 | 4–16 |
| Compute Lane | fargate | fargate / spot |

Storage is provisioned automatically via EFS; there is no disk-size parameter.

## Notes
- Tools installed outside `/home/coder` are part of the container image; rebuild the image to
  add system packages. Files under `/home/coder` persist across restarts.
- The OpenAI client libraries (`openai`, `langchain-openai`, `llama-index-llms-openai`) used
  for the gateway's OpenAI-compatible path are baked into the shared base image
  (`images/coder-workspace-base/Dockerfile`); rebuild the workspace images (Step 1 pipeline)
  to pick them up on the pre-baked `Python (Agents)` kernel.
- For building and deploying AI agents to AWS, the [`awshp-k8s-challenge-agent`](../awshp-k8s-challenge-agent)
  template ships the agent frameworks + AWS deploy tooling (also optimized for Coder Agents).
