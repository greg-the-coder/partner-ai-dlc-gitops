# Lambda MicroVMs as a Coder Workspace Compute Lane — Revised MVP Plan

_Revised after reviewing `greg-the-coder/partner-ai-dlc-gitops` (Coder v2.36+, AI-DLC reference deployment)._

## What changed vs. the earlier plan

The earlier plan assumed a greenfield "run the Coder agent inside a MicroVM." Your latest
release already delivers the core value proposition — **Firecracker microVM isolation for
each agentic run** — through the **Fargate compute lane**, and it already has a clean
`compute_lane` parameter abstraction (Fargate default + EC2 Spot).

So the MVP is no longer "build a MicroVM workspace from scratch." It is:

> **Add Lambda MicroVMs as a third, opt-in `compute_lane` value** for ephemeral,
> agent-driven ("Construction") runs, delivering the capabilities Fargate cannot:
> snapshot-based near-instant start/resume, suspend/resume with memory+disk state
> preserved (idle-cost savings for bursty agent sessions), and per-session elevated
> OS capabilities — while leaving human dev workspaces on Fargate/Spot unchanged.

## Current-state facts (from the repo) that drive the design

| Area | Today in the repo | Implication for a MicroVM lane |
|---|---|---|
| Compute lanes | `coder_parameter.compute_lane` → `fargate` (Firecracker via Fargate profile) / `spot` (EKS Auto Mode) | Add a `microvm` option; branch resource creation on it |
| Workspace object | `kubernetes_deployment` + agent as container (`CODER_AGENT_TOKEN` env, `init_script` command) | MicroVM is **not** a K8s pod — must be provisioned via the `lambda-microvms` API |
| Persistence | EFS via K8s CSI, `ReadWriteMany` PV, access-point-per-workspace (`/workspaces/<id>`) | Reuse the **same EFS + access-point pattern**, but mount **in-guest** (needs `additional-os-capabilities ALL` + VPC egress connector to mount targets on 2049) |
| Workspace AWS identity | EKS Pod Identity / IRSA `<cluster>-workshop-user` role (Bedrock + AWS MCP calls) | Replaced by the MicroVM **`executionRoleArn`** — cleaner, no pod SA. Reconcile script's `AWS_ROLE_ARN`/`AWS_WEB_IDENTITY_TOKEN_FILE` IRSA injection must switch to the execution role's ambient creds |
| AI harness | Built-in **Coder Agents** loop + `claude-code` v5 module; **Coder Tasks dropped** (`CODER_HIDE_AI_TASKS=true`) | Unchanged. Harness is control-plane side; the workspace just needs an agent + toolchain |
| Model routing | `coder_env` `ANTHROPIC_/OPENAI_BASE_URL` → `access_url/api/v2/ai-gateway/<provider>` (Bedrock, openai-compat); governed by AI Governance Add-On | **Unchanged** — pure outbound HTTPS to coderd, works over MicroVM egress; governance still applies |
| Agent connectivity | Coder agent dials out over Tailnet; no inbound needed | **Unchanged** — MicroVM egress (public or `VPC_EGRESS`) satisfies it |
| Images | CodeBuild → ECR; Fargate pulls multi-GB image each start | MicroVM builds a **snapshot** from S3 zip + Dockerfile on a Lambda base image; frameworks baked into snapshot → near-instant start regardless of size |
| Deployment | CloudFormation + Helm; GitOps/Terraform driven | MicroVM lane adds a **non-K8s provisioning path** (no native TF resource yet) |

## Why add the lane at all (Fargate already gives Firecracker)

1. **Near-instant start/resume.** Fargate cold-starts pull the large agent-framework image
   every launch. A MicroVM resumes from a memory+disk **snapshot** (frameworks already
   imported) in ~1s per 500 MB — better UX for on-demand agent runs.
2. **Suspend/resume with state = idle cost savings.** AI-DLC "Construction" is bursty with
   long human-in-the-loop / model-thinking idle gaps. Fargate bills the full pod the whole
   time; a MicroVM **suspends to low idle cost with RAM+disk intact** and resumes where it
   left off. This is the single biggest economic lever for agent workloads.
3. **Per-session elevated OS capabilities** (eBPF, nested containers, FUSE) without running
   privileged pods on the cluster.

## Hard constraints to design around (specific to this deployment)

- **8-hour max MicroVM lifetime (28,800s cap).** Fine for ephemeral agent/Construction runs;
  **not** for long-lived human dev workspaces. ⇒ Make the MicroVM lane **opt-in for agent
  runs only**; humans stay on Fargate/Spot. All durable state must live on **EFS** (MicroVM
  local disk resets to snapshot on each `RunMicrovm`).
- **GovCloud unavailable.** Lambda MicroVMs is commercial-region-only today (API model dated
  2025-09-09; no `aws-us-gov` partition support). ⇒ **Feature-flag the lane off in GovCloud
  deployments.** Public Sector/Defense customers keep the **Fargate Firecracker** lane, which
  already satisfies their isolation requirement.
- **Idle is measured by inbound proxy traffic only.** The Coder agent's outbound Tailnet
  traffic won't reset the idle timer, and auto-resume triggers on ingress the agent never
  sends. ⇒ For MVP, **omit `idlePolicy`** (run→terminate on workspace stop). Fast-follow:
  have the harness/control plane drive `Suspend`/`Resume` explicitly on task state.
- **Not a K8s pod.** Coder's workspace RBAC/service-account perms don't govern it; it is a new
  IAM surface (`buildRoleArn` + `executionRoleArn`).
- **No first-class Terraform resource.** Drive the API via `null_resource`/`external` (or a
  thin custom provider) — a new pattern in this otherwise K8s/Terraform GitOps repo.
- **Agent token must not be baked into the snapshot** (shared memory state) — inject at
  `RunMicrovm` via `runHookPayload`.

## MVP scope

A new `compute_lane = "microvm"` option on **one** existing agent template
(`awshp-k8s-with-claude-code` or `awshp-k8s-challenge-agent`) that provisions the workspace as
a Lambda MicroVM in a commercial region, EFS-backed home, execution-role identity for
Bedrock/AWS MCP, harness + model routing unchanged. Explicitly excludes GovCloud and
long-lived human workspaces.

## Phased plan (~1.5–2 weeks)

### Phase 0 — Validate (0.5d)
- Confirm MicroVMs is live in the deployment's commercial region
  (`aws lambda-microvms list-managed-microvm-images`); check concurrent-MicroVM + launch-rate
  quotas. Co-locate S3 artifact bucket, base image, and connectors in-region.
- Confirm how EFS mount-target reachability + `executionRoleArn` cred surfacing behave inside
  a MicroVM with `VPC_EGRESS` (spike a bare MicroVM that mounts EFS and calls Bedrock).

### Phase 1 — MicroVM image build path (2–3d)
- Reuse the existing `images/coder-workspace-claude-code` Dockerfile content, repackaged for
  **`create-microvm-image`**: zip (Dockerfile + artifacts) → S3 → build on the AL2023 Lambda
  base image with `--additional-os-capabilities '["ALL"]'` (for EFS/FUSE).
- Add a hook server (port 9000): `/ready` + `/validate` (clean snapshot + prefetch);
  `/run` (≤60s): read `runHookPayload` (`CODER_AGENT_TOKEN`, `CODER_AGENT_URL`), mount EFS,
  launch `coder agent`.
- Add a CodeBuild path (parallel to the ECR pipeline) that produces the MicroVM image version.
- Bake the agent frameworks + `uv`/`uvx` MCP cache into the snapshot (kills the cold-start
  MCP timeout problem the template currently works around with `MCP_TIMEOUT=180000`).

### Phase 2 — Identity, networking, IAM (1–2d)
- Define least-privilege **`executionRoleArn`** granting exactly what `<cluster>-workshop-user`
  grants today (Bedrock invoke + the AWS MCP scope) plus EFS; region/account-scoped, with
  `aws:SourceAccount`/`aws:SourceArn` confused-deputy conditions. Define `buildRoleArn`.
- Create a `VPC_EGRESS` network connector into the existing workspace VPC/subnets/SG so the
  MicroVM reaches EFS mount targets (2049) and, if coderd is private, the control plane
  (+ NAT for public egress to the AI Gateway if needed).

### Phase 3 — Template integration (3d)
- Add `microvm` to `coder_parameter.compute_lane` (guarded by a template var so it is hidden
  in GovCloud deployments).
- Branch on the lane: keep `kubernetes_deployment` for `fargate`/`spot`; for `microvm`,
  provision via a `null_resource`/`external` that calls `run-microvm`
  (`--maximum-duration-in-seconds 28800`, `--execution-role-arn`, `--egress-network-connectors`,
  token via `--run-hook-payload`); `terminate-microvm` on stop; store `microvmId` in state.
- Swap the reconcile script's IRSA injection (`AWS_ROLE_ARN`/`AWS_WEB_IDENTITY_TOKEN_FILE`) for
  the MicroVM execution-role credential path on this lane.
- Reuse the EFS access-point-per-workspace pattern (`/workspaces/<id>`); mount in-guest.
- Leave all `coder_env` AI-Gateway routing, `claude-code` module, MCP config, and Coder Agents
  wiring **unchanged**.

### Phase 4 — E2E validation (1–2d)
- Launch an agent workspace on the `microvm` lane → Coder agent healthy; Coder Agents harness
  runs a task; Claude Code + AWS MCP work; model calls appear in AI Session logs (governance
  intact).
- Verify EFS home persists across stop/start; verify graceful behavior at the 8h cap.
- Benchmark start latency + $/idle-hour vs. the Fargate lane for a representative agent run.

### Phase 5 — Docs & guardrails (0.5d)
- Document: 8h max session, EFS-only persistence, **commercial-region-only (feature-flagged
  off in GovCloud)**, and when to pick `microvm` vs. `fargate` vs. `spot`.
- Set workspace TTLs < 8h on the lane; enable CloudTrail for MicroVM lifecycle events.

## Fast-follow (post-MVP)
- Harness-driven `Suspend`/`Resume` on task state (unlock idle savings around the ingress-only
  idle metric).
- Proper Terraform provider for `lambda-microvms` to replace `local-exec`.
- Multiple VM sizes (one image per size).
- `SHELL_INGRESS` as an out-of-band debug console.
- Re-evaluate GovCloud when/if MicroVMs lands in that partition.

## Explicitly NOT in scope
- GovCloud / Defense private-network deployments (unsupported partition → stay on Fargate
  Firecracker lane).
- Long-lived human dev workspaces (fight the 8h cap → stay on Fargate/Spot).
- Replacing the Fargate lane (MicroVMs is additive, not a replacement).
