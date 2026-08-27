# AWS Lambda MicroVM Agent (prototype)

A **prototype** Coder template that provisions each workspace as an **AWS Lambda
MicroVM** instead of a Kubernetes pod, to test/validate Lambda MicroVMs as a
Coder workspace compute lane. It is a simplified, MicroVM-only variant of
[`awshp-k8s-challenge-agent`](../awshp-k8s-challenge-agent).

See [`docs/lambda-microvm-mvp-plan.md`](../../docs/lambda-microvm-mvp-plan.md)
for the full analysis and MVP plan.

> **Status: prototype.** The AWS-side integration is **validated live** in a
> commercial region, now via a **controller Lambda** (no aws CLI on the Coder
> provisioner) — build → run → HTTPS ingress → `/run` payload unwrap → agent
> launch, plus idempotent reuse and terminate. See
> [`images/coder-microvm-agent/VALIDATION.md`](../../images/coder-microvm-agent/VALIDATION.md).
> Still open: a real agent connection to the control plane and the EFS in-guest mount.

## Provisioning approach (no aws CLI on the provisioner)

The Coder provisioner image has no `aws` CLI, so the template does **not** shell
out. Instead it invokes a small **controller Lambda**
([`infrastructure/microvm-controller`](../../infrastructure/microvm-controller))
via the Terraform `aws_lambda_invocation` resource with `lifecycle_scope =
"CRUD"`: Terraform calls it on create/update (run or reuse the workspace's
MicroVM) and on destroy (terminate it). The provisioner needs only the AWS
provider (already authenticated) + `lambda:InvokeFunction` on the controller.
The controller uses boto3 to call `lambda-microvms` and keeps the
workspace-id → microvmId index in S3.

> The `scripts/` dir (`microvm_run.sh` / `microvm_terminate.sh`) is retained as
> an alternative for deployments that run an **external provisioner WITH the aws
> CLI**; this template uses the Lambda path.

## Why a MicroVM lane (on top of the existing Fargate Firecracker lane)

- **Snapshot-instant start/resume** — resumes from a memory+disk snapshot with
  the agent frameworks already imported, instead of Fargate pulling a multi-GB
  image on every cold start.
- **Suspend/resume with state = idle savings** — agentic runs are bursty with
  long human-in-the-loop / model-thinking gaps; a MicroVM suspends to low idle
  cost with RAM+disk intact (fast-follow; see constraints).
- **Per-session elevated OS capabilities** without privileged cluster pods.

## Hard constraints (by design)

| Constraint | Handling in this template |
|---|---|
| **ARM_64 (Graviton) only** | Image built ARM_64; `coder_agent.arch = "arm64"` (validated: X86_64 build is rejected) |
| **8h max MicroVM lifetime** | `microvm_max_duration_seconds` capped at 28800; ephemeral agent runs only — humans stay on Fargate/Spot |
| **Single-size image** | Size baked at build (`create-microvm-image --resources`); `run-microvm` takes no `--resources`. CPU/mem inputs imply one image per size (see gaps) |
| **Commercial regions only (no GovCloud)** | Do not deploy this template in GovCloud; Defense customers keep the Fargate Firecracker lane |
| **Idle measured by inbound traffic only** | No `idlePolicy` set (agent is outbound-only); stop = terminate. Fast-follow: harness-driven suspend/resume |
| **`/run` must return 200** | Hook is best-effort; a non-200 terminates the VM |
| **`/run` payload is wrapped** | Injected token arrives as the string under `runHookPayload`; the hook unwraps it |
| **Snapshot memory shared across runs** | Agent token injected at run time via the `/run` hook payload, never baked into the image |
| **No first-class TF resource** | Provisioned via `null_resource` + `aws lambda-microvms` CLI in `scripts/` |

## Architecture / flow

1. **Image (once):** run `provision-iam.sh` (bucket + build/exec roles), then
   `build.sh` builds `images/coder-microvm-agent/` into a Firecracker snapshot
   via `create-microvm-image` (`--additional-os-capabilities '["ALL"]'` for EFS,
   ARM_64). Bake the agent toolchain in so runs start instantly.
2. **Start:** the create-time `aws_lambda_invocation` calls the controller,
   which find-or-runs the workspace's MicroVM, injects the Coder agent
   token/URL/init via the `/run` hook payload, and returns `microvm_id` +
   `endpoint`.
3. **Inside the MicroVM:** `hooks/hook_server.py` handles `/run` — mounts the
   per-workspace EFS access point at `/home/coder` and launches `coder agent`,
   which dials **out** to coderd over the tailnet (no inbound required).
4. **AI:** the built-in Coder Agents harness + AI Gateway env vars route model
   calls to Bedrock/openai-compat over egress, governed by the AI Governance
   Add-On — identical to the K8s templates. Direct AWS/Bedrock calls use the
   MicroVM `executionRoleArn` (replaces EKS IRSA).
5. **Stop/delete:** the destroy-scoped `aws_lambda_invocation` (`tf.action =
   delete`) calls the controller, which terminates the workspace's MicroVM
   (looked up via the S3 index).

## Prerequisites (per deployment)

- Region where Lambda MicroVMs is available (commercial only).
- MicroVM image built from `images/coder-microvm-agent/` → set
  `microvm_image_identifier` / `microvm_image_version`.
- `microvm_execution_role_arn` — the workspace's AWS identity (Bedrock invoke +
  AWS MCP scope + EFS), scoped with `aws:SourceAccount`/`aws:SourceArn`.
- `microvm_egress_connector_arn` — a `VPC_EGRESS` connector reaching EFS mount
  targets (2049) and, if coderd is private, the control plane (leave empty for
  default public egress).
- `efs_file_system_id` — reused from the existing deployment.
- The Coder **provisioner image must include the `aws` CLI** (the scripts call
  `aws lambda-microvms ...`).

## Template variables

| Variable | Default | Purpose |
|---|---|---|
| `aws_region` | `us-east-1` | Region for MicroVMs / S3 / connectors |
| `microvm_image_identifier` | — (required) | MicroVM image ARN |
| `microvm_image_version` | `1.0` | Image version to run |
| `microvm_execution_role_arn` | — (required) | MicroVM runtime IAM identity |
| `microvm_egress_connector_arn` | `""` | Optional VPC egress connector |
| `efs_file_system_id` | `""` | Persistent home EFS |
| `microvm_max_duration_seconds` | `28800` | Hard lifetime (≤ 8h) |

## Not yet implemented (fast-follow)

- Harness-driven `Suspend`/`Resume` on task state (idle savings).
- A proper Terraform provider for `lambda-microvms` (replace `local-exec`).
- `/ready` + `/validate` build-hook wiring in `create-microvm-image` for
  snapshot prefetch.
- End-to-end validation in a live commercial-region account.
