# AWS Coder AI-DLC GitOps

AI-powered development platform on AWS: [Coder](https://coder.com) on Amazon EKS, with
**serverless Fargate workspaces** and **Coder Agents** backed by Amazon Bedrock.

![Architecture Diagram](images/AWSCoderSingleRegionv2-0.png)

## Overview

This repository deploys a complete AI-assisted development environment via a single
CloudFormation stack. Two capabilities are the focus of this platform:

- **Fargate workspaces** — developer workspaces run on AWS Fargate (serverless pods), with
  persistent home directories backed by Amazon EFS. No worker nodes to manage or scale for
  workspace compute.
- **EC2 Spot workspaces** — a second, standardized compute lane on an EKS Auto Mode **Spot
  NodePool** (auto-scaled, scales to zero when idle) for cost-optimized, larger, GPU, or
  privileged workloads. Home directories stay on Amazon EFS, so storage is identical across
  both lanes; the lane is chosen per workspace via the **Compute Lane** template parameter.
- **Coder Agents** — the built-in agentic coding assistant, wired to Amazon Bedrock (native)
  and Bedrock Mantle (OpenAI-compatible) so agents run entirely on AWS-hosted models.

See [Deployment](#deployment) to run it in your own AWS account.

## Why this matters: AWS AI-DLC

This platform is purpose-built to operationalize the **AWS AI-Driven Development Life Cycle
(AI-DLC)** — AWS's methodology for adopting agentic AI across the software lifecycle while
keeping humans in control. AI-DLC structures work into a **three-phase, human-approved
workflow** — *Inception* (the what and why), *Construction* (the how), and *Review* (did it
work) — where AI agents plan, implement, and review, and engineers make the decisions at
every phase gate. Agents pause and ask when they need clarification, and requirements stay
traceable down to the code they produce.

The hard part of adopting AI-DLC is not the agents — it is giving them a place to *act*
safely. That is what this repository provides:

- **A governed execution surface.** Agentic “Construction” means agents run commands,
  install dependencies, and execute generated code. Doing that on
  [Firecracker microVM](https://firecracker-microvm.github.io/)-isolated
  [Fargate workspaces](#fargate-workspaces) confines each agent run to a disposable,
  single-tenant VM — the blast radius of an autonomous action is one sandbox.
- **Enterprise guardrails by construction.** Workspaces inherit scoped IAM roles, run inside
  your VPC, use models served through Amazon Bedrock, and are provisioned from version-
  controlled, reviewable templates. Access, networking, models, and tooling are all things
  the enterprise defines — not the agent.
- **Centralized, auditable AI access.** [Coder Agents](#coder-agents) route through
  admin-configured Bedrock providers and models, so model choice, usage, and budgets are
  governed centrally rather than per-developer.

### Fit for highly regulated enterprises — and everyone else

For **highly regulated industries** (financial services, healthcare, public sector), the
primary barrier to agentic AI is not capability but **control, isolation, and
auditability**. Running AI-DLC on this platform addresses those directly: VM-level isolation
per workspace (Firecracker), no shared kernels, data and inference kept within AWS accounts
and Regions you control, IAM/VPC boundaries around every agent, and human approval gates
built into the methodology. AWS's own [Responsible AI
Policy](https://aws.amazon.com/ai/responsible-ai/policy/) — review agent output and costs —
maps cleanly onto AI-DLC's human-in-the-loop phase gates.

The same properties let **any size enterprise** scale agentic development effectively:
start small with disposable sandboxes, keep humans deciding intent and reviewing output, and
grow agent autonomy as confidence in the guardrails grows — without standing up bespoke
isolation or governance infrastructure.

**Learn more about AI-DLC:**

- AWS DevOps Blog — [AI-Driven Development Life
  Cycle](https://aws.amazon.com/blogs/devops/ai-driven-development-life-cycle/) (methodology)
- `awslabs/aidlc-workflows` — [adaptive AI-DLC workflow rules for AI coding
  agents](https://github.com/awslabs/aidlc-workflows) (Kiro, Amazon Q, Claude Code, Cursor,
  Copilot, and more)
- `aws-samples/sample-collaborative-ai-dlc` — [collaborative AI-DLC
  platform](https://github.com/aws-samples/sample-collaborative-ai-dlc) (reference
  implementation)

## Fargate Workspaces

Workspaces are scheduled onto an EKS **Fargate profile** (`coder-workspaces`, selector
`namespace=coder-ws`) instead of EC2 worker nodes. Because Fargate does not support EBS
volumes, persistent storage uses **Amazon EFS** mounted `ReadWriteMany`.

### Firecracker microVM isolation — sandboxes for humans *and* agents

AWS Fargate runs every pod inside a dedicated
[**Firecracker microVM**](https://firecracker-microvm.github.io/). Each workspace therefore
gets hardware-virtualized, single-tenant isolation — its own kernel and a minimal,
purpose-built virtualization boundary — rather than sharing a kernel with neighboring
containers, while still booting in a fraction of a second.

That combination of **strong isolation + fast, ephemeral startup** is exactly what you want
for **cloud sandboxes used by both human developers and agentic AI**:

- **Blast-radius containment** — an autonomous agent running commands, installing packages,
  or executing generated code is confined to a single-use microVM, not a shared host.
- **Clean, reproducible environments** — workspaces are disposable; spin one up per
  developer, per task, or per agent run and throw it away.
- **Defense in depth** — VM-level isolation layers on top of Kubernetes namespaces, IAM
  scoping, and VPC network controls.

This makes the platform a safe execution surface for letting AI agents *build* — not just
suggest — inside guardrails the enterprise controls.

| Component | Purpose |
|-----------|---------|
| EKS Fargate profile `coder-workspaces` | Runs workspace pods labelled `compute=fargate` in the `coder-ws` namespace serverlessly |
| Dedicated Fargate subnets (2 AZs) | Private subnets for Fargate pod ENIs |
| EFS file system (encrypted, elastic throughput) | Persistent `/home/coder` per workspace, survives restarts |
| EFS mount targets + NFS security group | Reachable from Fargate pods over port 2049 within the VPC |
| `efs-static` StorageClass (`efs.csi.aws.com`) | Binds each workspace PVC to its EFS access point |
| `FargatePodExecutionRole` | Pulls images and runs pods under `AmazonEKSFargatePodExecutionRolePolicy` |

Workspace templates create an EFS access point + PV/PVC per workspace and mount it at
`/home/coder`. Tools installed outside the home directory live in the container image.

## Workspace compute lanes (Fargate + EC2 Spot)

Every template exposes a **Compute Lane** parameter so a workspace can run on either lane
while keeping an identical **EFS-backed `/home/coder`**:

| Lane | Where it runs | Selected by | Best for |
|------|---------------|-------------|----------|
| `fargate` (default) | EKS **Fargate profile** `coder-workspaces` | pod label `compute=fargate` matched by the profile selector `namespace=coder-ws,labels={compute=fargate}` | Strong per-workspace Firecracker isolation; serverless, nothing to scale |
| `spot` | EKS Auto Mode **Spot NodePool** `coder-ws-spot` | pod `nodeSelector` `coder.workspace/lane=spot` + toleration for the matching `NoSchedule` taint | Cost-optimized (Spot), larger pods, GPU, privileged/Docker workloads |

The Spot lane is a Karpenter `NodePool` (`infrastructure/k8s/spot-nodepool.yaml`) that plugs
into **EKS Auto Mode**: it references Auto Mode's built-in `default` NodeClass (inheriting
the node IAM role, subnets, and security groups) and auto-scales on demand, consolidating
and scaling to zero when idle. Because the lane carries a taint, only opted-in workspaces
land there; everything else (including Fargate-lane pods) is unaffected.

**Storage is the single standard:** both lanes use the same per-workspace EFS access point +
static PV/PVC (`efs-static`, `ReadWriteMany`) mounted at `/home/coder`.

## Coder Agents

Coder Agents are configured during deployment via the `coderd` Terraform provider (no
console clicks required). Two AI providers are provisioned:

| Provider (name) | Type | Endpoint | Models |
|----------|------|----------|--------|
| `bedrock` ("AWS Bedrock") | Bedrock (native, Pod Identity IAM) | `bedrock-runtime.us-east-1` | Claude Opus 4.6 (default), Claude Haiku 4.5 (small/fast) |
| `openai-compat` ("OpenAI via AWS Bedrock") | OpenAI-compatible (Bedrock Mantle) | `bedrock-mantle.us-east-1` | OpenAI gpt-oss-120b, Devstral 2 123B |

Notes:
- Anthropic models use global cross-region inference profile IDs and are served from
  **us-east-1**, independent of the stack's deployment region.
- Native Bedrock credentials come from the workspace/`coderd` pod IAM role (EKS Pod
  Identity), so no static access keys are stored for the `bedrock` provider.
- The Bedrock Mantle (OpenAI-compatible) API key is generated automatically from an IAM
  service-specific credential and stored in Secrets Manager.
- Provider and model configuration is applied declaratively by the
  [`ai-providers/`](./ai-providers) Terraform (the `coderd` provider) during the CodeBuild
  deploy step, replacing the earlier direct `/api/v2/ai/providers` and
  `/api/experimental/chats/model-configs` API calls.

## Workspace Templates

Templates live in [`templates/`](./templates) and are deployed via Terraform + the Coder
provider (see [GitOps Workflow](#gitops-workflow)). Each template's `description` and
`README.md` describe its capabilities so Coder Agents can pick the right environment.

| Template | Display Name | Best for |
|----------|--------------|----------|
| `awshp-k8s-challenge-agent` | Clash of Agents — Challenge Workspace | **Optimized for Coder Agents.** Pre-loaded Python agent frameworks (Strands, LangGraph, LangChain, LlamaIndex, Lyzr) + Bedrock. |
| `awshp-k8s-base-claudecode` | AWS Workshop — Kubernetes with Claude Code | Claude Code AI assistant with task automation. |
| `awshp-k8s-base-kirocli` | AWS Workshop — Kubernetes with Kiro CLI | Kiro CLI AI assistant for interactive development. |

All templates support both compute lanes (Fargate default, EC2 Spot optional) via the
**Compute Lane** parameter, with EFS-backed persistent home directories in either lane.

The **Claude Code** and **Kiro CLI** templates ship the
[AWS Labs MCP servers](https://github.com/awslabs/mcp) (AWS documentation, and AWS IaC —
CloudFormation + CDK) preconfigured for their assistants, running on demand via `uvx`.
The **Kiro CLI** template additionally offers an optional **KiroCrew** multi-agent
orchestration gateway + dashboard, toggled with its **Enable KiroCrew** parameter.

## Prerequisites

- AWS account with permissions to create EKS, VPC, Aurora, CloudFront, EFS, ECR, CodeBuild, IAM, Lambda, and Secrets Manager resources
- AWS CLI configured
- Sufficient quotas for EKS, Aurora PostgreSQL, CloudFront, and VPC resources (NAT Gateways, EIPs)
- Amazon Bedrock model access enabled in **us-east-1** for the configured Claude and Mistral models
- Deploy the [image pipeline stack](#step-1-build-workspace-images-codebuild_image_pipelineyaml) **before** the core stack (see [Deployment](#deployment))

## Deployment

Deployment is a **two-stack** process. The image pipeline stack must be deployed **first** —
it builds the container images the Fargate workspaces run on and publishes them to ECR. The
core Coder stack then references those images by URI, so it depends on the pipeline having
run successfully.

```
codebuild_image_pipeline.yaml   ->   builds & pushes ECR images   ->   coder_deployment.yaml
     (Step 1, run first)               (coder-workspace-*:latest)          (Step 2)
```

### Step 1: Build workspace images (`codebuild_image_pipeline.yaml`)

This stack creates three ECR repositories, a CodeBuild project, and a Lambda-backed custom
resource that runs the build automatically on stack create. It builds and pushes the images
used by the Fargate templates:

| ECR repository | Built from | Used by template |
|----------------|------------|------------------|
| `<EKSClusterName>/coder-workspace-claude-code` | [`images/coder-workspace-claude-code/`](./images/coder-workspace-claude-code) | `awshp-k8s-base-claudecode` |
| `<EKSClusterName>/coder-workspace-kiro-cli` | [`images/coder-workspace-kiro-cli/`](./images/coder-workspace-kiro-cli) | `awshp-k8s-base-kirocli` |
| `<EKSClusterName>/coder-workspace-challenge` | [`images/coder-workspace-challenge/`](./images/coder-workspace-challenge) | `awshp-k8s-challenge-agent` |

1. Create a stack from
   [`infrastructure/codebuild_image_pipeline.yaml`](./infrastructure/codebuild_image_pipeline.yaml).
2. Set parameters:
   - `EKSClusterName` — **must match** the value you will use for the core stack (default
     `coder-aws-cluster`); the ECR repository names are derived from it.
   - `GitRepoURL` — repository containing the `images/` Dockerfiles (default is this repo).
   - `GitBranch` — branch to build from (default `main`).
3. Acknowledge IAM resource creation and create the stack.
4. The custom resource starts the CodeBuild job automatically and waits for it to finish.
   Confirm success in the **CodeBuild** console (project `<EKSClusterName>-workspace-image-build`)
   and verify each ECR repository has a `latest` image before continuing.

> **Important:** Deploy this stack in the **same AWS account and Region** as the core stack,
> and use the **same `EKSClusterName`**. The core stack builds the image URIs by convention
> (`<account>.dkr.ecr.<region>.amazonaws.com/<EKSClusterName>/coder-workspace-*:latest`); if
> the repositories are missing or empty, workspaces will fail to start with image-pull errors.
>
> To rebuild images later (e.g., after changing a Dockerfile), start the
> `<EKSClusterName>-workspace-image-build` CodeBuild project again — no stack update needed.

### Step 2: Deploy Coder (`coder_deployment.yaml`)

1. Open the AWS CloudFormation console and create a stack from
   [`infrastructure/coder_deployment.yaml`](./infrastructure/coder_deployment.yaml).

   > **Deploying from the CLI?** `coder_deployment.yaml` is larger than CloudFormation's
   > **51,200-byte inline limit**, so `aws cloudformation create-stack --template-body
   > file://...` will fail with *"Member must have length less than or equal to 51200"*.
   > Upload the template to S3 and use `--template-url` instead:
   >
   > ```bash
   > aws s3 cp infrastructure/coder_deployment.yaml s3://<your-bucket>/coder_deployment.yaml
   > aws cloudformation create-stack --stack-name <name> \
   >   --template-url https://<your-bucket>.s3.<region>.amazonaws.com/coder_deployment.yaml \
   >   --parameters ... --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --region <region>
   > ```
   >
   > The **console** "upload a template file" option and the **install wizard** both stage
   > the template to S3 automatically, so they are unaffected.
2. Set the required parameters:
   - `CoderAdminEmail`, `CoderAdminUser`, `CoderAdminPassword`, `CoderAdminName`
3. Optional parameters (defaults shown):
   - `EKSClusterName` (`coder-aws-cluster`) — **use the same value as Step 1**,
     `KubernetesVersion` (`1.35`), `CoderVersion` (`2.36.0`),
     `CoderLicenseKey` (empty by default — supply a **Coder Premium** license JWT to enable
     premium features such as HA/multi-replica `coderd`; it is applied automatically at
     deploy via `coder licenses add`), `CoderGitOpsTemplateRepoURL`, `RetryFlag` (`False`)
4. Acknowledge IAM resource creation and create the stack (~30–45 minutes).

The stack provisions networking, Aurora PostgreSQL, the EKS cluster (Auto Mode + Fargate
profile + EC2 Spot NodePool), EFS storage, installs Coder via Helm, applies the Premium
license (if provided), configures CloudFront, deploys templates (pointing at the ECR images
from Step 1), and configures Coder Agents providers/models.

Monitor progress in the CloudFormation **Events** tab and the CodeBuild logs
(`/aws/codebuild/CodeBuild-<StackName>`).

### Access

When the stack completes, use these CloudFormation **Outputs**:

- `CoderURL` — CloudFront URL for the Coder dashboard
- `CoderAdminEmail` / `CoderAdminPassword` (also in Secrets Manager via
  `CoderAdminPasswordSecretArn`)
- `CoderSessionTokenSecretArn` — API token secret

Log in at `CoderURL`, then create a workspace from one of the templates.

## GitOps Workflow

Templates are versioned by Git SHA and applied with the Coder Terraform provider. The stack
runs [`templates/templates_gitops.sh`](./templates/templates_gitops.sh) automatically;
template metadata (name, display name, description, icon, image, EFS id) is defined in
[`templates/template_versions.tf`](./templates/template_versions.tf).

```bash
# Manual template update
cd templates/
export TF_VAR_coder_url="https://your-coder-url"
export TF_VAR_coder_token="your-session-token"
export TF_VAR_coder_gitsha="$(git log -1 --format=%H)"
terraform apply -auto-approve
```

## Architecture Summary

- **EKS** — Auto Mode (control plane + system workloads) with two workspace compute lanes: a dedicated **Fargate profile** (`compute=fargate` pods) and an auto-scaled **EC2 Spot NodePool** `coder-ws-spot` (`compute=spot` pods)
- **Aurora PostgreSQL Serverless v2** — Coder database (encrypted, KMS). Uses
  `DeletionPolicy: Retain` so a stack rollback/delete never destroys the database
  (re-attach or clean up the retained cluster when reusing the same `EKSClusterName`).
- **CloudFront + Network Load Balancer** — secure global access to Coder
- **VPC** — public/private/Fargate subnets across 2 AZs, NAT gateways for egress
- **EFS** — persistent workspace home directories (Fargate-compatible). Uses
  `DeletionPolicy: Retain` so a stack rollback/delete never destroys home dirs.
- **ECR** — three `coder-workspace-*` repositories holding the Fargate workspace images built by the [image pipeline stack](#step-1-build-workspace-images-codebuild_image_pipelineyaml)
- **Secrets Manager** — admin password, session token, Bedrock Mantle API key
- **IAM** — `<cluster>-workshop-user` workspace role (Bedrock, Bedrock Mantle, S3, Secrets Manager, EKS, EFS, etc.), where `<cluster>` is `EKSClusterName` so multiple environments can coexist in one account

## Troubleshooting

| Symptom | Checks |
|---------|--------|
| Stack creation fails | CodeBuild logs `/aws/codebuild/CodeBuild-<StackName>`; service quotas; IAM permissions |
| Cannot reach `CoderURL` | CloudFront status is `Deployed`; NLB target health; `kubectl get pods -n coder` |
| Workspace won't start | Fargate profile is `ACTIVE`; `kubectl get sc` shows `efs-static`; EFS mount targets healthy; `kubectl get pvc -n coder-ws` |
| Spot-lane workspace stuck `Pending` | `kubectl get nodepool coder-ws-spot`; confirm EKS Auto Mode can launch Spot capacity in the AZs; pod carries the `coder.workspace/lane=spot` toleration; Spot capacity available for the requested instance types |
| Workspace image pull error / `ImagePullBackOff` | Step 1 image pipeline ran successfully; each `<EKSClusterName>/coder-workspace-*` ECR repo has a `latest` image; `EKSClusterName` and Region match between both stacks |
| Coder Agent model errors | Bedrock model access in us-east-1; provider config via `/api/v2/ai/providers`; Bedrock Mantle secret populated |

## Cleanup

1. Delete all Coder workspaces from the UI.
2. Delete the CloudFormation stack.
3. Manually remove any retained resources (CloudFront distribution, logging S3 buckets, EKS
   cluster, Aurora cluster, EFS file system) if they were not auto-deleted.

## Resources

- [AWS AI-DLC (AI-Driven Development Life Cycle)](https://aws.amazon.com/blogs/devops/ai-driven-development-life-cycle/) and [awslabs/aidlc-workflows](https://github.com/awslabs/aidlc-workflows)
- [Firecracker microVM](https://firecracker-microvm.github.io/) (powers AWS Fargate isolation)
- [Coder Documentation](https://coder.com/docs)
- [Amazon EKS Fargate](https://docs.aws.amazon.com/eks/latest/userguide/fargate.html)
- [Amazon EFS CSI Driver](https://docs.aws.amazon.com/eks/latest/userguide/efs-csi.html)
- [AWS Responsible AI Policy](https://aws.amazon.com/ai/responsible-ai/policy/)

## License

See [LICENSE](LICENSE).

## Contributing

Designed for AWS AI Builder Lab events. Follow standard GitOps practices and test changes in
a non-production environment first.
