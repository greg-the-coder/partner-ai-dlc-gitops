# Partner Demo — Coder Install Wizard

A CLI that guides you through a **full lifecycle** of the Partner AI-DLC demo
platform — [Coder](https://coder.com) **2.37.0** on Amazon EKS (Auto Mode) —
from install to teardown, using the
[`partner-ai-dlc-gitops`](https://github.com/greg-the-coder/partner-ai-dlc-gitops)
CloudFormation stacks.

---

## Quick Start

```bash
# Install
pip install ./coder-install-wizard

# Deploy (interactive)
partner-coder-wizard

# Tear down everything when done
partner-coder-wizard teardown --cluster coder-2-37-0-partnerdemo --region us-west-2
```

---

## What It Does

| Phase | Command | Description |
|-------|---------|-------------|
| **Pre-flight** | `preflight` | Validates AWS credentials, Bedrock model access, service quotas, and EKS cluster-name conflicts |
| **Cost estimate** | `cost` | Per-team-size monthly breakdown with Fargate / EC2 Spot compute-lane split |
| **Deploy** | `deploy` | Ordered two-stack deployment: image pipeline (CodeBuild → ECR) then core Coder stack, with real-time event streaming |
| **Validate** | `validate` | Confirms Coder API, admin token, Premium license, AI providers, templates, both compute lanes, and EFS |
| **Monitor** | `status` / `watch` | Check deployment status or stream CloudFormation events from any shell |
| **Tear down** | `teardown` | Removes all deployment resources in the correct dependency order |

---

## What It Deploys

| Capability | Detail |
|---|---|
| Coder control plane | **v2.37.0**, HA (2 replicas) with a Premium license |
| Compute lane 1 — **Fargate** | EKS Fargate profile `coder-workspaces`; Firecracker microVM isolation |
| Compute lane 2 — **EC2 Spot** | EKS Auto Mode Spot NodePool `coder-ws-spot`; auto-scaled, scale-to-zero |
| Storage | **Amazon EFS** per-workspace access point at `/home/coder` in both lanes |
| AI | Amazon Bedrock (native Anthropic) + OpenAI-compatible endpoint via Coder Agents |

---

## Prerequisites

- Python 3.10+
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) configured
- IAM permissions for EKS, VPC, Aurora, CloudFront, EFS, ECR, CodeBuild, IAM, Lambda, S3, and Secrets Manager
- The `partner-ai-dlc-gitops` repository cloned locally
- (Optional) `kubectl` — only used to validate the EC2 Spot NodePool
- (Optional) `eksctl` — used by `teardown` for clean EKS deletion; falls back to AWS CLI if absent

---

## Installation

```bash
pip install ./coder-install-wizard        # from the repo root
pip install -e ./coder-install-wizard     # editable / development
python -m coder_wizard                    # or run directly without installing
```

---

## Commands

### `wizard` — Interactive Install (default)

```bash
partner-coder-wizard
```

Walks you through region, cluster name, Coder version, team size, Spot lane
share, admin credentials, and Premium license — then runs preflight, cost
estimate, and deploys.

### `deploy` — Non-Interactive Install

```bash
partner-coder-wizard deploy \
  --region us-east-1 \
  --cluster coder-aws-cluster \
  --admin-email ops@example.com \
  --admin-user admin \
  --developers 20 \
  --spot-fraction 40 \
  --license-key "$CODER_LICENSE_JWT" \
  --yes
```

| Flag | Default | Description |
|------|---------|-------------|
| `--region` | current AWS CLI region | AWS region |
| `--cluster` | `coder-aws-cluster` | EKS cluster name (use a new name for Blue/Green) |
| `--coder-version` | `2.37.0` | Coder version to install |
| `--admin-email` | *(required)* | Coder admin email |
| `--admin-user` | `admin` | Coder admin username |
| `--admin-password` | auto-generated | Stored in Secrets Manager |
| `--license-key` | *(empty)* | Coder Premium JWT — enables HA + premium features |
| `--developers` | `10` | Team size (cost estimate only) |
| `--spot-fraction` | `0` | % of workspaces on the EC2 Spot lane (cost estimate only) |
| `--dry-run` | | Generate parameter files + `deploy.sh` without creating resources |
| `--no-wait` | | Submit the core stack and exit with monitoring links |
| `--retry` | | Resume a failed deployment using saved parameters |
| `--yes` | | Skip confirmation prompts |

### `teardown` — Remove All Resources

```bash
partner-coder-wizard teardown \
  --cluster coder-2-37-0-partnerdemo \
  --region us-west-2
```

Discovers all resources belonging to the cluster, displays them, and
(after confirmation) deletes them in the correct dependency order.

| Flag | Description |
|------|-------------|
| `--cluster` | EKS cluster name — the deployment identifier |
| `--region` | AWS region |
| `--delete-data` | Also delete retained Aurora database and EFS file system (**permanent data loss**) |
| `--yes` | Skip confirmation prompts |

**Deletion order:**

| Step | Resource | Notes |
|------|----------|-------|
| 1 | EKS cluster | Includes Fargate profiles, nodegroups; via `eksctl` or AWS CLI fallback |
| 2 | eksctl sub-stacks | Addon CSI drivers, cluster CloudFormation stack |
| 3 | S3 buckets | CloudFront logs, NLB logs (emptied then deleted) |
| 4 | Core Coder CFN stack | VPC, CloudFront, IAM roles, CodeBuild, KMS key |
| 5 | ECR repositories | 4 workspace images (force-deleted with images) |
| 6 | Image pipeline CFN stack | CodeBuild project, Lambda, IAM role |
| 7 | Aurora cluster + instances | **Only with `--delete-data`** — skipped by default |
| 8 | EFS file system | **Only with `--delete-data`** — mount targets removed first |
| 9 | Secrets Manager | Admin password, session token, Bedrock API key (force-deleted) |
| 10 | IAM users | Bedrock API key user (credentials + policies cleaned up first) |
| 11 | Wizard staging bucket | `coder-wizard-templates-<account>-<region>` |

> Aurora and EFS use `DeletionPolicy: Retain` in CloudFormation, so they
> survive stack deletion by default. Pass `--delete-data` to explicitly
> remove them — this is **irreversible**.

### `preflight` — Pre-flight Checks

```bash
partner-coder-wizard preflight --region us-east-1 --cluster coder-aws-cluster
```

| Check | What it verifies |
|---|---|
| AWS Credentials | `sts get-caller-identity` succeeds |
| AWS Region | Warns if deploying outside us-east-1 |
| Bedrock Model Access | Claude Opus 4.6, Haiku 4.5, GPT-5.6 Sol, Grok 4.6 |
| Service Quotas | EKS clusters, VPCs, NAT Gateways, EIPs, Aurora ACUs, EC2 Spot vCPUs |
| EKS Cluster Name | No existing cluster with the same name |
| ECR Images | Workspace images exist in ECR (skipped during deploy) |

### `cost` — Cost Estimate

```bash
partner-coder-wizard cost --developers 25 --spot-fraction 40 --region us-east-1
```

### `validate` — Post-Install Validation

```bash
partner-coder-wizard validate \
  --coder-url https://xxxx.cloudfront.net \
  --cluster coder-aws-cluster \
  --efs-id fs-0123456789abcdef0 \
  --stack-name coder-aws-cluster-coder
```

| Check | What it verifies |
|---|---|
| Coder API | `/api/v2/buildinfo` returns HTTP 200 |
| Admin Token | `/api/v2/users/me` returns the admin user |
| Premium License | License applied (HA + premium enabled) |
| AI Providers | At least one provider enabled (bedrock + openai-compat) |
| Templates | At least one active template deployed |
| Fargate Lane | `coder-workspaces` Fargate profile is ACTIVE |
| Spot Lane | `coder-ws-spot` NodePool present (requires kubectl) |
| EFS CSI Driver | `aws-efs-csi-driver` addon is ACTIVE |
| EFS File System | EFS is in `available` state |

### `status` / `watch` — Monitor Deployments

```bash
partner-coder-wizard status --cluster coder-aws-cluster       # one-shot status + links
partner-coder-wizard watch  --cluster coder-aws-cluster       # stream events (Ctrl-C safe)
```

---

## Operational Notes

### Re-running the wizard

`deploy` is safe to re-run. It checks each stack before creating it:

- **Healthy** — skips the stack and moves on
- **Failed** — deletes and recreates (unless an EKS cluster blocks VPC deletion)
- **In progress** — asks you to wait and re-run

### Resuming a failed deployment

Every live deploy saves non-secret parameters to `~/.coder-wizard/last-deploy.json`.
Resume without re-entering anything:

```bash
partner-coder-wizard deploy --retry
```

On retry the wizard reloads saved parameters, assesses both stacks, and
deploys the core stack with `RetryFlag=True` (reuses the existing EKS
cluster, CloudFront, and skips first-user creation).

### Long installs & short-lived shells

The core stack takes ~35–45 minutes. The deployment runs server-side
(CloudFormation + CodeBuild), so a dropped shell does **not** stop it.

- `deploy --no-wait` submits and exits with monitoring links
- `Ctrl-C` detaches the wizard, not the deployment
- Reattach from any shell with `status`, `watch`, or `deploy --retry`

### Large templates

`coder_deployment.yaml` (~60 KB) exceeds CloudFormation's 51,200-byte
inline limit. The wizard auto-stages it to a private S3 bucket
(`coder-wizard-templates-<account>-<region>`) and deploys with
`--template-url`. Set `CODER_WIZARD_TEMPLATE_KMS_KEY_ARN` for SSE-KMS
encryption instead of the default SSE-S3.

---

## Architecture

```
coder_wizard/
├── __main__.py       ← CLI entry point, wizard UI, sub-command dispatch
├── preflight.py      ← Pre-flight check suite (credentials, quotas, Bedrock, ECR)
├── deploy.py         ← CloudFormation deploy orchestrator + CodeBuild waiter
├── validate.py       ← Post-install validation (Coder API, license, lanes, EFS)
├── cost_estimate.py  ← Monthly cost estimator (Fargate + EC2 Spot split)
├── dryrun.py         ← Parameter-file + deploy.sh generator (no AWS calls)
├── summary.py        ← install-summary.json writer + human-readable output
└── teardown.py       ← Resource discovery + ordered teardown orchestrator
```

---

## Roadmap

- [x] ~~Uninstall wizard with ordered resource cleanup~~ → `teardown` command
- [ ] Query live Bedrock token consumption post-install for actual AI spend
- [ ] Detect running Coder version and offer an in-place upgrade path
- [ ] Per-lane cost breakdown from real instance-type Spot prices
