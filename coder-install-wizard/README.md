# Partner Demo — Coder Install Wizard

A GenAI-assisted CLI that guides you through a **Blue/Green install** of the
Partner AI-DLC demo platform — [Coder](https://coder.com) **2.36.0** on Amazon EKS
(Auto Mode) — into your own AWS account, using the
[`partner-ai-dlc-gitops`](https://github.com/greg-the-coder/partner-ai-dlc-gitops)
CloudFormation stacks.

It replaces the manual two-stack README process with:

1. **Pre-flight checks** — AWS credentials, Bedrock model access, service quotas
   (EKS, NAT Gateway, Aurora ACUs, EIP, **EC2 Spot vCPUs**), ECR image dependency,
   and EKS cluster-name conflicts.
2. **Cost estimate** — a per-team-size monthly breakdown, including the **Fargate /
   EC2-Spot compute-lane split**, before a single resource is created.
3. **Ordered deployment** — deploys the image pipeline stack first (CodeBuild → ECR),
   waits for images, then deploys the core Coder stack — eliminating the most common
   `ImagePullBackOff` failure.
4. **Real-time progress** — CloudFormation events streamed to your terminal.
5. **Post-install validation** — Coder API reachable, admin token works, **Premium
   license** applied, AI providers wired to Bedrock, templates deployed, **both compute
   lanes ready** (Fargate profile ACTIVE + EC2 Spot NodePool present), EFS CSI driver
   installed, and EFS available.
6. **Install summary** — writes `install-summary.json` with endpoints, secret ARNs,
   and validation results.

---

## What this deploys

| Capability | Detail |
|---|---|
| Coder control plane | **v2.36.0**, HA (2 replicas) when a Premium license is provided |
| Compute lane 1 — **Fargate** | EKS Fargate profile `coder-workspaces`; pods labelled `compute=fargate`; Firecracker microVM isolation |
| Compute lane 2 — **EC2 Spot** | EKS Auto Mode Spot NodePool `coder-ws-spot`; pods opt in via the template **Compute Lane** parameter; auto-scaled, scale-to-zero |
| Storage standard | **Amazon EFS** per-workspace access point mounted at `/home/coder` in **both** lanes |
| AI | Amazon Bedrock (native) + Bedrock Mantle (OpenAI-compatible) via Coder Agents |

---

## Prerequisites

- Python 3.10+
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) configured (`aws configure` or `aws sso login`)
- IAM permissions to create EKS, VPC, Aurora, CloudFront, EFS, ECR, CodeBuild, IAM, Lambda, S3, and Secrets Manager resources
  (S3 is used to stage the core template — see [Large templates](#large-templates))
- The `partner-ai-dlc-gitops` repository cloned locally
- (Optional) `kubectl` — only used to validate the EC2 Spot NodePool post-install

---

## Installation

```bash
# From the repo root
pip install ./coder-install-wizard
# or for development
pip install -e ./coder-install-wizard
```

Or run directly without installing:

```bash
python -m coder_wizard
```

---

## Usage

### Interactive wizard (recommended)

```bash
partner-coder-wizard
```

Asks a few questions (region, cluster, Coder version, team size, **Spot lane share**,
admin user, **Premium license**), runs pre-flight, shows a cost estimate, and deploys.

### Sub-commands

```bash
# Pre-flight checks only (fast — no deploy)
partner-coder-wizard preflight --region us-east-1 --cluster coder-aws-cluster

# Cost estimate for a 25-developer team with 40% of workspaces on the Spot lane
partner-coder-wizard cost --developers 25 --spot-fraction 40 --region us-east-1

# Fully non-interactive deploy (Premium license enables HA)
partner-coder-wizard deploy \
  --region us-east-1 \
  --cluster coder-aws-cluster \
  --coder-version 2.36.0 \
  --admin-email ops@example.com \
  --admin-user admin \
  --admin-name "Platform Team" \
  --developers 20 \
  --spot-fraction 40 \
  --license-key "$CODER_LICENSE_JWT" \
  --yes

# Generate parameter files + deploy.sh without creating resources
partner-coder-wizard deploy --admin-email ops@example.com --dry-run

# Validate an existing deployment
partner-coder-wizard validate \
  --coder-url  https://xxxx.cloudfront.net \
  --cluster    coder-aws-cluster \
  --efs-id     fs-0123456789abcdef0 \
  --stack-name coder-aws-cluster-coder
```

---

## Most commonly used input parameters

The wizard captures the CloudFormation inputs teams change most often:

| Wizard prompt / flag | CloudFormation parameter | Default |
|---|---|---|
| AWS region | (deploy region) | current AWS CLI region |
| EKS cluster name | `EKSClusterName` | `coder-aws-cluster` |
| Coder version | `CoderVersion` | `2.36.0` |
| Kubernetes version | `KubernetesVersion` | `1.35` |
| Admin email / user / full name | `CoderAdminEmail` / `CoderAdminUser` / `CoderAdminName` | `admin@example.com` / `admin` / `Coder Admin` |
| Admin password | `CoderAdminPassword` | auto-generated → Secrets Manager |
| Premium license key | `CoderLicenseKey` | empty (Community Edition) |
| GitOps repo URL / branch | `CoderGitOpsTemplateRepoURL` / `GitRepoURL` / `GitBranch` | this repo / `main` |
| Developers, Spot lane share | *(cost estimate only)* | `10`, `0%` |

> **Compute Lane** (`fargate` vs `spot`) is a **per-workspace template parameter**, not
> a stack input — the Spot lane share here only affects the cost estimate. Both stacks
> always provision both lanes.

---

## Pre-flight Checks

| Check | What it verifies |
|---|---|
| AWS Credentials | `sts get-caller-identity` — valid credentials exist |
| AWS Region | Warns if deploying outside us-east-1 (Bedrock inference hardcoded there) |
| Bedrock Model Access | Claude Opus 4, Claude Haiku 4.5, Mistral Large 3, Devstral 2 accessible |
| Quota: EKS Clusters / VPCs / NAT Gateways / EIPs | Headroom for a fresh cluster |
| Quota: Aurora ACUs | ≥ 40 Serverless v2 ACUs |
| Quota: EC2 Spot Standard vCPUs | ≥ 32 (for the Spot workspace lane) |
| EKS Cluster Name Conflict | No existing cluster with the same name (Blue/Green) |
| ECR Workspace Images | All 3 `:latest` images exist (Step 1 complete) |

---

## Post-Install Validation

| Check | What it verifies |
|---|---|
| Coder API Reachable | `/api/v2/buildinfo` returns HTTP 200 |
| Admin Session Token | `/api/v2/users/me` returns the admin user |
| Coder Premium License | A license is applied (HA + premium features enabled) |
| Coder AI Providers | At least one provider enabled (bedrock + openai-compat) |
| Workspace Templates | At least one active template deployed via GitOps |
| Fargate Lane (profile) | `coder-workspaces` Fargate profile is ACTIVE |
| Spot Lane (NodePool) | Auto Mode NodePool `coder-ws-spot` present (via kubectl if available) |
| EFS CSI Driver | `aws-efs-csi-driver` addon is ACTIVE |
| EFS File System | EFS is in `available` state |

---

## Large templates

`infrastructure/coder_deployment.yaml` exceeds CloudFormation's **51,200-byte inline
`--template-body` limit** (it is ~57 KB). A raw `aws cloudformation create-stack
--template-body file://...` therefore fails with:

```
'templateBody' failed to satisfy constraint: Member must have length less than or equal to 51200
```

The wizard handles this automatically: `deploy` (and the generated `deploy.sh`) inline
small templates with `--template-body`, but stage anything over the limit to a private,
public-access-blocked S3 bucket (`coder-wizard-templates-<account>-<region>`) and deploy
with `--template-url`. This is why the caller needs S3 permissions
(`s3:CreateBucket`, `s3:PutObject`, `s3:GetObject`, `s3:PutBucketPublicAccessBlock`,
`s3:PutEncryptionConfiguration`, `s3:PutLifecycleConfiguration`).

On creation the staging bucket is hardened (best-effort — missing permissions are
skipped, not fatal):

- **Encryption:** SSE-S3 (`AES256`) by default. Set `CODER_WIZARD_TEMPLATE_KMS_KEY_ARN`
  to a CMK ARN to use SSE-KMS with an S3 Bucket Key instead (the caller then also needs
  `kms:GenerateDataKey`/`kms:Decrypt` on that key).
- **Lifecycle:** staged templates are transient, so objects expire after **7 days** and
  incomplete multipart uploads are aborted after 1 day.

## Re-running the wizard

`deploy` is safe to re-run. Before Step 3 it checks the image pipeline stack
(`<cluster>-image-pipeline`) and:

- **healthy** (`CREATE_COMPLETE` / `UPDATE_COMPLETE`) — skips the image build and goes
  straight to the core stack (images are already in ECR);
- **failed / unusable** (`ROLLBACK_COMPLETE`, `CREATE_FAILED`, `REVIEW_IN_PROGRESS`, …)
  — deletes the stack and recreates it;
- **in progress** (`*_IN_PROGRESS`) — stops and asks you to wait for the current
  operation to finish, then re-run.

To force a rebuild of a healthy pipeline, delete the stack or re-run its CodeBuild
project (`<cluster>-workspace-image-build`).

## Architecture of the Wizard

```
coder_wizard/
├── __main__.py       ← CLI entry point, wizard UI, sub-command dispatch
├── preflight.py      ← Pre-flight check suite (credentials, quotas, Bedrock, Spot, ECR)
├── deploy.py         ← CloudFormation deploy orchestrator + CodeBuild waiter
├── validate.py       ← Post-install validation (Coder API, license, both lanes, EFS)
├── cost_estimate.py  ← Monthly cost estimator (Fargate + EC2 Spot split)
├── dryrun.py         ← Parameter-file + deploy.sh generator (no AWS calls)
└── summary.py        ← install-summary.json writer + human-readable summary
```

---

## Roadmap

- [ ] Query live Bedrock token consumption post-install for actual AI spend
- [ ] Detect running Coder version and offer an in-place upgrade path (e.g. 2.34 → 2.36)
- [ ] Per-lane cost breakdown from real instance-type Spot prices
- [ ] Uninstall wizard with ordered resource cleanup (Blue/Green teardown of the old stack)
