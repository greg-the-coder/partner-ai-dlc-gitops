# AWS agent rules (Agent Toolkit for AWS)

Recommended, persistent instructions for AI coding agents working on AWS in this
deployment. Seeded into each workspace at startup (see each template's
`coder_script`) at `~/.coder/AGENTS.md`, which the Coder workspace agent scans
and shares with every chat. The native CLIs also read agent rules
(`AGENTS.md` for Codex, `CLAUDE.md` for Claude Code, `~/.kiro/steering/` for
Kiro), so keep this content tool-agnostic.

> Source of truth: the Agent Toolkit for AWS "recommended rules". Customize the
> project specifics (Region, tags, naming) for your demo below.

## Working with AWS

- Use the **AWS MCP Server** (`aws-mcp`) for all AWS API calls and AWS
  documentation lookups. Prefer it over ad-hoc `aws` CLI or `boto3` shell calls
  so actions are attributable (IAM condition keys `aws:ViaAWSMCPService` /
  `aws:CalledViaAWSMCP`) and audited in CloudTrail/CloudWatch.
- **Discover skills first.** Before starting an AWS task, check for a relevant
  Agent Toolkit **skill** and follow it. Skills are discovered at runtime through
  the AWS MCP Server (no local install required) and encode tested, Well-
  Architected procedures.
- **Verify against current docs.** Use the MCP server's documentation search to
  confirm APIs and defaults instead of relying on training data, which may be
  stale or miss newer services.
- **Prefer infrastructure as code** (CDK / CloudFormation) over imperative CLI
  mutations for anything you intend to keep.
- **Least privilege & safety.** This is a shared demo account. Treat production-
  like resources as read-only unless explicitly asked to change them; propose
  destructive actions rather than executing them.

## Project specifics (customize)

- **Default Region:** `us-west-2`.
- **Tags:** apply `Project=partner-ai-dlc`, `ManagedBy=agent` to resources you
  create.
- **Naming:** prefix demo resources with `paidlc-`.
