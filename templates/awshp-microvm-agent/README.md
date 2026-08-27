# AWS Lambda MicroVM Agent (prototype)

A **prototype** Coder template that provisions each workspace as an **AWS Lambda
MicroVM** instead of a Kubernetes pod, to test/validate Lambda MicroVMs as a
Coder workspace compute lane. It is a simplified, MicroVM-only variant of
[`awshp-k8s-challenge-agent`](../awshp-k8s-challenge-agent).

See [`docs/lambda-microvm-mvp-plan.md`](../../docs/lambda-microvm-mvp-plan.md)
for the full analysis and MVP plan.

> **Status: prototype / not production.** The `aws lambda-microvms` CLI shape is
> per the AWS docs at time of writing. Flags marked `TODO` in
> `scripts/microvm_run.sh` (notably `--tags` on `run-microvm`, and the
> `--resources` / `--run-hook-payload` shapes) need a live-account smoke test.

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
| **8h max MicroVM lifetime** | `microvm_max_duration_seconds` capped at 28800; ephemeral agent runs only — humans stay on Fargate/Spot |
| **Commercial regions only (no GovCloud)** | Do not deploy this template in GovCloud; Defense customers keep the Fargate Firecracker lane |
| **Idle measured by inbound traffic only** | No `idlePolicy` set (agent is outbound-only); stop = terminate. Fast-follow: harness-driven suspend/resume |
| **Snapshot memory shared across runs** | Agent token injected at run time via the `/run` hook payload, never baked into the image |
| **No first-class TF resource** | Provisioned via `null_resource` + `aws lambda-microvms` CLI in `scripts/` |

## Architecture / flow

1. **Image (once):** `images/coder-microvm-agent/` is built into a Firecracker
   snapshot with `aws lambda-microvms create-microvm-image`
   (`--additional-os-capabilities '["ALL"]'` for EFS mounts). Bake the agent
   toolchain in so runs start instantly.
2. **Start:** the create-time provisioner runs `scripts/microvm_run.sh`, which
   find-or-creates a MicroVM tagged `com.coder.workspace.id`, injects the Coder
   agent token/URL/init script via the `/run` hook payload, and waits for
   `RUNNING`.
3. **Inside the MicroVM:** `hooks/hook_server.py` handles `/run` — mounts the
   per-workspace EFS access point at `/home/coder` and launches `coder agent`,
   which dials **out** to coderd over the tailnet (no inbound required).
4. **AI:** the built-in Coder Agents harness + AI Gateway env vars route model
   calls to Bedrock/openai-compat over egress, governed by the AI Governance
   Add-On — identical to the K8s templates. Direct AWS/Bedrock calls use the
   MicroVM `executionRoleArn` (replaces EKS IRSA).
5. **Stop/delete:** the destroy-time provisioner runs
   `scripts/microvm_terminate.sh`, terminating the workspace's MicroVM(s) by tag.

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
