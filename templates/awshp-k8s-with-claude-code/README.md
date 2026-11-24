---
display_name: AWS Workshop - Kubernetes with Claude Code
description: Fargate Claude Code workspace routed through the Coder AI Gateway, with the Agent Toolkit for AWS MCP Server, AWS CLI/CDK, Node.js, and Amazon Bedrock access.
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
- **MCP** (Model Context Protocol) — the
  [Agent Toolkit for AWS](https://aws.amazon.com/products/developer-tools/agent-toolkit-for-aws/)
  **AWS MCP Server** is added to Claude Code at user
  scope, reached over stdio via `uvx mcp-proxy-for-aws` (SigV4). One managed,
  remote endpoint provides AWS API access (`call_aws`), live AWS
  documentation/API-reference search, runtime-discoverable skills, and a
  sandboxed Python tool — superseding the former AWS Labs servers (IaC,
  pricing, Serverless, CloudWatch). Calls use the workspace IAM role (IRSA).
  > SigV4 (local proxy) is used in the workspace because it is headless and
  > already carries IRSA credentials; the org-level Coder Agents chat connects to
  > the same AWS MCP Server anonymously for docs + skills (OAuth is not usable
  > for a server-side MCP registration — AWS Sign-in DCR rejects its callback
  > URL). An earlier remote `aws-mcp`
  > attempt was removed after `-32602` handshake failures and is re-introduced
  > here pinned to a known-good proxy version against the regional endpoint.

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
- **Compute lanes (`Compute Lane` parameter):**
  - `fargate` (default) — serverless AWS Fargate, Firecracker microVM isolation; no nodes to manage.
  - `spot` — EKS Auto Mode EC2 Spot NodePool (`coder-ws-spot`); auto-scaled, low cost.
  - `nitro` — **AWS Nitro Sandbox** (`isolated-sandbox` RuntimeClass) on a dedicated on-demand NodePool (`coder-ws-nitro`): each workspace Pod runs in its own hardware-isolated microVM with its own kernel — the strongest per-workspace isolation, ideal for untrusted or agent-generated code. Requires EKS Auto Mode on Kubernetes 1.36+; egress uses the deployment's existing network controls.
  - **Choosing a lane:** see the [compute-lane comparison](../../README.md#choosing-a-lane) for each lane's benefits, trade-offs, and when to use it.
- **Storage:** Amazon EFS access point mounted at `/home/coder` (`ReadWriteMany`, persistent)
- **Image:** [`images/coder-workspace-claude-code/Dockerfile`](../../images/coder-workspace-claude-code/Dockerfile)

## Parameters

| Parameter | Default | Range |
|-----------|---------|-------|
| CPU cores | 4 | 2–8 |
| Memory (GB) | 8 | 4–16 |
| Compute Lane | fargate | fargate / spot / nitro |

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
