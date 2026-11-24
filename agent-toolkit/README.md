# Agent Toolkit for AWS integration

This directory holds shared assets for integrating the
[Agent Toolkit for AWS](https://aws.amazon.com/products/developer-tools/agent-toolkit-for-aws/)
into the Partner AI-DLC demo. The Agent Toolkit is AWS's designated successor to
the AWS Labs MCP servers and the AWS Knowledge MCP server.

## What changed

| Surface | Before | After |
|---|---|---|
| **Org-level Coder Agents** (`ai-providers/ai_providers.tf`) | AWS Knowledge MCP (`knowledge-mcp.global.api.aws`, anonymous) | **AWS MCP Server** (`aws-mcp.us-west-2.api.aws/mcp`), `auth_type = oauth2` (per-user AWS Sign-in, auto-discovery) |
| **Workspace IDE/CLI** (Kiro, Claude Code, Codex templates) | 4 AWS Labs stdio servers (`awslabs.*`) | single **`aws-mcp`** via `uvx mcp-proxy-for-aws==1.6.3` (SigV4, IRSA creds) |
| **Base image** (`images/coder-workspace-base/Dockerfile`) | pre-warm 4 `awslabs.*` packages | pre-warm `mcp-proxy-for-aws==1.6.3` |
| **IAM** (`infrastructure/coder_deployment.yaml`) | — | `AWSMCPSignInOAuthAccessPolicy` on `WorkshopCoderRole` |
| **Rules** | — | `aws-agent-rules.md` seeded to `~/.coder/AGENTS.md` per workspace |

## Why two auth models

- **Org-level chat (OAuth 2.1):** runs in the Coder control plane and is
  per-user, so each developer authorizes interactively with their own AWS
  identity via the connect popup. Coder discovers AWS Sign-in's OAuth endpoints
  (RFC 9728 → 8414 → 7591) and stores/refreshes the token per user.
- **Workspace IDE/CLI (SigV4):** headless pods with no browser, but they already
  carry IRSA (`AWS_ROLE_ARN` / `AWS_WEB_IDENTITY_TOKEN_FILE`) credentials, which
  `mcp-proxy-for-aws` signs with directly.

## Skills

Skills are **discovered at runtime** through the AWS MCP Server, so no local
install or image bake is required — important here because `/home/coder` is
overlaid by the EFS persistent volume at runtime. To pre-install instead, run
`npx skills add aws/agent-toolkit-for-aws/skills` inside a workspace.

## Rules

`aws-agent-rules.md` is the canonical recommended ruleset. Each template's
startup writes it to `~/.coder/AGENTS.md` so the Coder workspace agent shares it
with every chat. Customize the project specifics (Region, tags, naming) for your
environment.

## Validation checklist

See the implementation plan (`PLAN-…md`) §7. In short: confirm each client shows
`aws-mcp` connected (no `-32601/-32602`), a docs query + a read-only API query
succeed, and CloudTrail records `aws:CalledViaAWSMCP=true`.
