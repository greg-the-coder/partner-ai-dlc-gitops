---
display_name: AWS Workshop - Kubernetes with Kiro CLI
description: Fargate workspace with the Kiro CLI AI assistant, the Agent Toolkit for AWS MCP Server, AWS CLI/CDK, Node.js, and Amazon Bedrock access.
icon: ../../../site/static/icon/k8s.png
maintainer_github: coder
verified: true
tags: [kubernetes, fargate, ai, kiro, kiro-cli, bedrock, mcp]
---

# Kubernetes with Kiro CLI

A serverless Coder workspace running on **AWS Fargate** with the
[Kiro CLI](https://kiro.dev/docs) AI assistant. The home directory is persisted on
**Amazon EFS** so installed tools and work survive workspace restarts.

## Capabilities

### AI assistant
- **Kiro CLI** (`kiro-cli`) for interactive, command-line AI development
- **Kiro IDE** web app
- **MCP** (Model Context Protocol) support — pre-seeded `~/.kiro/settings/mcp.json`
  with the [Agent Toolkit for AWS](https://aws.amazon.com/products/developer-tools/agent-toolkit-for-aws/)
  **AWS MCP Server**, reached over stdio via `uvx mcp-proxy-for-aws` (SigV4).
  One managed, remote endpoint provides AWS API access (`call_aws`), live AWS
  documentation/API-reference search, runtime-discoverable skills, and a
  sandboxed Python tool — superseding the former AWS Labs servers
  (docs, IaC, pricing, Serverless, CloudWatch). Calls use the workspace IAM role (IRSA).
- **Amazon Bedrock** access via the workspace IAM role

### Developer environment
- **code-server** (VS Code in the browser)
- One-click **Kiro CLI** authentication app (`kiro-auth`)
- Web terminal
- Node.js 20 LTS, AWS CLI v2, AWS CDK

## Runtime & infrastructure
- **Compute lanes (`Compute Lane` parameter):**
  - `fargate` (default) — serverless AWS Fargate, Firecracker microVM isolation; no nodes to manage.
  - `spot` — EKS Auto Mode EC2 Spot NodePool (`coder-ws-spot`); auto-scaled, low cost.
  - `nitro` — **AWS Nitro Sandbox** (`isolated-sandbox` RuntimeClass) on a dedicated on-demand NodePool (`coder-ws-nitro`): each workspace Pod runs in its own hardware-isolated microVM with its own kernel — the strongest per-workspace isolation, ideal for untrusted or agent-generated code. Requires EKS Auto Mode on Kubernetes 1.36+; egress uses the deployment's existing network controls.
  - **Choosing a lane:** see the [compute-lane comparison](../../README.md#choosing-a-lane) for each lane's benefits, trade-offs, and when to use it.
- **Storage:** Amazon EFS access point mounted at `/home/coder` (`ReadWriteMany`, persistent)
- **Image:** [`images/coder-workspace-kiro-cli/Dockerfile`](../../images/coder-workspace-kiro-cli/Dockerfile)

## Parameters

| Parameter | Default | Range |
|-----------|---------|-------|
| CPU cores | 2 | 2–8 |
| Memory (GB) | 4 | 4–16 |
| Compute Lane | fargate | fargate / spot / nitro |

Storage is provisioned automatically via EFS; there is no disk-size parameter.

## Getting started
1. Create a workspace from this template.
2. Click the **Kiro CLI** (authenticate) app to log in.
3. Open code-server or Kiro and use `kiro-cli chat` in the terminal.

## Notes
- Tools installed outside `/home/coder` are part of the container image; rebuild the image to
  add system packages. Files under `/home/coder` persist across restarts.
- For building and deploying AI agents to AWS, the [`awshp-k8s-challenge-agent`](../awshp-k8s-challenge-agent)
  template ships the agent frameworks + AWS deploy tooling (also optimized for Coder Agents).
