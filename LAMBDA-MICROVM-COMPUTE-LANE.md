# Lambda MicroVM Compute Lane for Coder — Implementation Summary

Run Coder workspaces as **AWS Lambda MicroVMs** (Firecracker-isolated, snapshot-fast,
Graviton/ARM64) as an opt-in compute lane alongside the existing Fargate and EC2 Spot
lanes — purpose-built for ephemeral, agentic ("Construction") runs from the Coder Agents
harness.

> **Status: working prototype, validated live** on `partner.coderdemo.io` (region
> `us-east-1`). A workspace created from the `awshp-microvm-agent` template provisions a
> MicroVM, the Coder agent connects, and the workspace is **connected / healthy** with
> working `coder ssh`. See [Validation](#validation--current-status).

---

## 1. What was built

| Piece | Path | Purpose |
|---|---|---|
| MVP plan / analysis | [`docs/lambda-microvm-mvp-plan.md`](docs/lambda-microvm-mvp-plan.md) | Why MicroVMs, fit vs. Fargate, constraints, phased plan |
| Coder template | [`templates/awshp-microvm-agent/`](templates/awshp-microvm-agent/) | Simplified, MicroVM-only variant of `awshp-k8s-challenge-agent` |
| └ template main | [`templates/awshp-microvm-agent/main.tf`](templates/awshp-microvm-agent/main.tf) | Invokes the controller Lambda via `aws_lambda_invocation` (no CLI on provisioner) |
| └ template README | [`templates/awshp-microvm-agent/README.md`](templates/awshp-microvm-agent/README.md) | Per-template usage, constraints, variables |
| └ CLI fallback scripts | [`templates/awshp-microvm-agent/scripts/`](templates/awshp-microvm-agent/scripts/) | `microvm_run.sh` / `microvm_terminate.sh` for an external provisioner *with* the AWS CLI |
| MicroVM image | [`images/coder-microvm-agent/`](images/coder-microvm-agent/) | Firecracker image (AL2023) + lifecycle hook server |
| └ Dockerfile | [`images/coder-microvm-agent/Dockerfile`](images/coder-microvm-agent/Dockerfile) | Base `public.ecr.aws/lambda/microvms:al2023-minimal` + toolchain |
| └ hook server | [`images/coder-microvm-agent/hooks/hook_server.py`](images/coder-microvm-agent/hooks/hook_server.py) | Lifecycle hooks; on `/run` mounts EFS + launches the **server-matched** Coder agent |
| └ image build | [`images/coder-microvm-agent/build.sh`](images/coder-microvm-agent/build.sh) | Package → S3 → `create/update-microvm-image` → wait for snapshot |
| └ IAM/bucket setup | [`images/coder-microvm-agent/provision-iam.sh`](images/coder-microvm-agent/provision-iam.sh) | S3 artifact bucket + build/exec roles |
| └ validation notes | [`images/coder-microvm-agent/VALIDATION.md`](images/coder-microvm-agent/VALIDATION.md) | Everything proven live + the exact findings |
| Controller Lambda | [`infrastructure/microvm-controller/`](infrastructure/microvm-controller/) | boto3 Lambda that runs/terminates MicroVMs; keeps the workspace→id index in S3 |
| └ handler | [`infrastructure/microvm-controller/handler.py`](infrastructure/microvm-controller/handler.py) | `create`/`update`→run-or-reuse, `delete`→terminate |
| └ deploy | [`infrastructure/microvm-controller/deploy.sh`](infrastructure/microvm-controller/deploy.sh) | Role + bundled boto3 + function + account invoke permission |
| coderd DERP setting | [`infrastructure/helm/coder-values.yaml`](infrastructure/helm/coder-values.yaml) | `CODER_DERP_FORCE_WEBSOCKETS=true` (required — see prereqs) |

---

## 2. Architecture

```
 Coder provisioner (coderd)                         AWS
 ┌───────────────────────────┐   aws_lambda_       ┌───────────────────────────────┐
 │ Terraform: awshp-microvm- │   invocation (CRUD) │ coder-microvm-controller       │
 │ agent template            │────────────────────▶│ (boto3)                        │
 │  • coder_agent (arm64)    │  create/update/     │  • RunMicrovm / GetMicrovm     │
 │  • aws provider           │  delete + input     │  • TerminateMicrovm            │
 │  NO aws CLI needed         │◀────────────────────│  • S3 index: ws-id → microvmId │
 └───────────────────────────┘   {microvm_id,      └───────────────┬───────────────┘
                                    endpoint}                       │ RunMicrovm
                                                                    ▼
                                              ┌──────────────────────────────────────┐
                                              │ Lambda MicroVM (Firecracker, AL2023)   │
   agent dials OUT (tailnet/DERP-ws)          │  hook_server.py                        │
   ◀──────────────────────────────────────── │   /run → mount EFS + download SERVER-  │
      partner.coderdemo.io                    │          matched agent + `coder agent` │
                                              └──────────────────────────────────────┘
```

**Key design choices**
- **No AWS CLI on the Coder provisioner.** The template calls a **controller Lambda** via
  the Terraform `aws_lambda_invocation` resource (`lifecycle_scope = "CRUD"`): create/update
  runs-or-reuses the MicroVM, destroy terminates it. The provisioner needs only the AWS
  provider (already used for EFS) + `lambda:InvokeFunction`.
- **Agent connectivity is outbound.** The Coder agent dials coderd over the tailnet; the
  MicroVM needs only egress. No inbound/ingress infra required.
- **Server-matched agent.** The hook downloads the agent from
  `${CODER_AGENT_URL}/bin/coder-linux-arm64` at run time so it always matches the server’s
  RPC API version (a baked agent that is newer than the server is rejected).
- **Per-workspace mapping without tags.** `run-microvm` has no tags, so the controller keeps
  a `workspace-id → microvmId` index in S3 (`coder-microvm-index/<ws-id>`).

---

## 3. Coder deployment prerequisites

1. **Coder v2.32+ with the AI Gateway / AI Governance Add-On** (for the model routing env the
   template sets). This deployment is v2.34.4.
2. **`CODER_DERP_FORCE_WEBSOCKETS=true` on coderd** — **required for this lane.** coderd sits
   behind CloudFront/ALB that strips the raw `Upgrade: derp` header (DERP returns HTTP 426),
   so MicroVMs (reaching coderd via public egress) can’t use classic DERP. Without this the
   agent shows *healthy* but `coder ssh`/apps time out. Set in
   [`infrastructure/helm/coder-values.yaml`](infrastructure/helm/coder-values.yaml); already
   applied live.
3. **AWS provider credentials on the provisioner.** The internal provisioner already has AWS
   creds (it creates `aws_efs_access_point` for the K8s lanes). It additionally needs
   `lambda:InvokeFunction` on the controller — satisfied here by a **resource-based policy**
   on the function granting the account, so no provisioner identity change is required.
4. **Commercial AWS region only.** Lambda MicroVMs is **not** available in GovCloud — keep this
   lane feature-flagged off for Public Sector/Defense (they stay on the Fargate Firecracker
   lane).

---

## 4. AWS prerequisites (one-time per deployment)

Run from [`images/coder-microvm-agent/`](images/coder-microvm-agent/) and
[`infrastructure/microvm-controller/`](infrastructure/microvm-controller/):

```bash
# 1) S3 artifact bucket + build/exec IAM roles (trust lambda.amazonaws.com)
cd images/coder-microvm-agent
AWS_REGION=us-east-1 ./provision-iam.sh

# 2) Build the MicroVM image snapshot (ARM_64) — prints IMAGE_ARN / IMAGE_VERSION
export AWS_REGION=us-east-1 ARCH=ARM_64
export BUCKET=coder-microvm-artifacts-<acct>-us-east-1
export BUILD_ROLE_ARN=arn:aws:iam::<acct>:role/coder-microvm-build-role
./build.sh coder-microvm-agent

# 3) Deploy the controller Lambda (+ role + account invoke permission)
cd ../../infrastructure/microvm-controller
AWS_REGION=us-east-1 \
  BUCKET=coder-microvm-artifacts-<acct>-us-east-1 \
  EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/coder-microvm-exec-role \
  ./deploy.sh
```

**IAM notes (validated):** the `lambda-microvms` API uses the **`lambda:`** IAM action
namespace. `RunMicrovm` requires `iam:PassRole` on the exec role (no `PassedToService`
condition) **and** `lambda:PassNetworkConnector` on the connector (even the default
`INTERNET_EGRESS`). Full breakdown in
[`VALIDATION.md`](images/coder-microvm-agent/VALIDATION.md).

**Optional:** a `VPC_EGRESS` network connector if coderd/EFS are private or you want to
restrict egress; an EFS file system for persistent `$HOME`.

---

## 5. Push the template & create a workspace

```bash
coder templates push awshp-microvm-agent -d templates/awshp-microvm-agent \
  --variable microvm_image_identifier=arn:aws:lambda:us-east-1:<acct>:microvm-image:coder-microvm-agent \
  --variable microvm_execution_role_arn=arn:aws:iam::<acct>:role/coder-microvm-exec-role \
  --variable microvm_state_bucket=coder-microvm-artifacts-<acct>-us-east-1 \
  --variable microvm_controller_function=coder-microvm-controller \
  --variable aws_region=us-east-1 --yes

coder create my-microvm-ws --template awshp-microvm-agent --yes
```

### Template variables

| Variable | Default | Notes |
|---|---|---|
| `microvm_image_identifier` | — (required) | MicroVM image ARN |
| `microvm_image_version` | `""` | Empty resolves the ACTIVE version |
| `microvm_execution_role_arn` | — (required) | Workspace’s AWS identity inside the MicroVM (replaces IRSA) |
| `microvm_state_bucket` | — (required) | S3 bucket for the workspace→id index |
| `microvm_controller_function` | `coder-microvm-controller` | Controller Lambda name/ARN |
| `microvm_egress_connector_arn` | `""` | Optional VPC egress connector |
| `efs_file_system_id` | `""` | Optional persistent home |
| `microvm_max_duration_seconds` | `28800` | Hard 8h cap |
| `aws_region` | `us-east-1` | Commercial region only |

---

## 6. Validation & current status

Validated end-to-end via the authenticated `coder` CLI:
- **Build** → Firecracker snapshot (ARM_64).
- **`coder create`** → controller runs the MicroVM; idempotent restart reuses the same VM.
- **Agent** → downloads server-matched binary, connects: `dev (linux, arm64) ⦿ connected ✔ healthy v2.34.4`.
- **`coder ssh`** → runs commands inside the MicroVM (after DERP WebSocket setting).
- **Stop/delete** → controller terminates the MicroVM (via S3 index).

Full findings & how-to-reproduce: [`images/coder-microvm-agent/VALIDATION.md`](images/coder-microvm-agent/VALIDATION.md).

### Key constraints discovered (all handled)
- **ARM_64 (Graviton) only** — X86_64 image build is rejected → `coder_agent.arch = "arm64"`.
- **8h max MicroVM lifetime** — ephemeral/agent runs only; humans stay on Fargate/Spot.
- **Single-size image** — size baked at build; `run-microvm` takes no `--resources`.
- **`runHookPayload` ≤ 4096 bytes** — init script inlined only if it fits, else the hook launches the agent itself.
- **`/run` body is wrapped** `{microvmId, runHookPayload:"…"}`; hook unwraps it and `/run` must return 200.
- **Base image Python is 3.9** — hook uses `from __future__ import annotations`.
- **Runtime stdout not shipped to CloudWatch** — prototype `/debug/*` endpoints used for diagnosis (remove before prod).

---

## 7. Known gaps / follow-ups
- **Agent runs as root** inside the MicroVM (K8s lanes use uid 1000) — align if desired.
- **EFS in-guest mount** wired but not yet exercised end-to-end (needs a VPC_EGRESS connector to the mount targets on 2049).
- **Harness-driven suspend/resume** for idle-cost savings (MVP maps stop→terminate).
- **First-class Terraform provider** for `lambda-microvms` to replace the controller Lambda / CLI scripts.
- **Remove/gate the `/debug/exec` endpoint** before any non-prototype use.
- **GovCloud** — revisit if/when Lambda MicroVMs lands in that partition.

---

## 8. Cost & cleanup
Prototype resources in the account: the S3 artifact bucket, `coder-microvm-*` IAM roles, the
controller Lambda, the `coder-microvm-agent` image (v7.0 ACTIVE), and any running workspace
MicroVMs. MicroVMs bill while RUNNING and terminate on workspace stop/delete or at the 8h cap;
image versions incur storage cost (prune old ones). To fully tear down: delete workspaces,
`delete-microvm-image`, delete the controller Lambda + roles, and empty/delete the bucket.

---

_Branch: `feat/lambda-microvm-compute-lane`. Start with the
[MVP plan](docs/lambda-microvm-mvp-plan.md) and
[VALIDATION.md](images/coder-microvm-agent/VALIDATION.md)._
