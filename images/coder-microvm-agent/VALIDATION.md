# Lambda MicroVM integration — live validation results

This prototype was validated end-to-end in a real commercial-region AWS account
(`us-east-1`, EKS Pod Identity role `coder-and-aws-workshop-user`) using the AWS
CLI `lambda-microvms` API. Summary of what was proven and what was learned.

## Proven end-to-end ✅

`images/coder-microvm-agent/build.sh` → snapshot build → `run-microvm` →
`RUNNING` → HTTPS ingress via the service proxy → lifecycle hook contract →
`/run` payload unwrap → **Coder agent process launched inside the MicroVM**.

Concretely, against image `coder-microvm-agent` v5.0:
- `create/update-microvm-image` built a Firecracker snapshot (ARM_64/Graviton).
- `run-microvm` reached `RUNNING` in ~1 poll (a few seconds) from the snapshot.
- `create-microvm-auth-token` + `curl https://<endpoint>/ -H X-aws-proxy-auth
  -H "X-aws-proxy-port: 9000"` returned **HTTP 200** `{"status":"ok",
  "agent_started":true}` — i.e. ingress works and the `/run` hook successfully
  extracted the injected Coder agent token/URL/init script and launched it.

## Key findings folded back into the code

1. **ARM_64 (Graviton) only.** `create-microvm-image` with
   `cpuConfigurations=[{architecture:X86_64}]` is rejected:
   `Member must satisfy enum value set: [ARM_64]`.
   → `build.sh` defaults `ARCH=ARM_64`; the template sets `coder_agent.arch =
   "arm64"`. The arm64 Coder agent rpm and `uv` install cleanly.

2. **Base image Python is 3.9.** The hook server crashed at import with
   `TypeError: unsupported operand type(s) for |` (PEP 604 `X | None`).
   → added `from __future__ import annotations` to `hooks/hook_server.py`.

3. **`/run` must return HTTP 200 or the MicroVM is terminated.** A raised
   exception → 503 → `stateReason: "Run lifecycle hook returned HTTP status
   503"` and the VM never reaches `RUNNING`.
   → `/run` is now best-effort: it launches the agent if a token is present but
   always answers 200.

4. **`/run` payload is WRAPPED.** The `/run` request body is
   `{"microvmId":"...","runHookPayload":"<the string passed to --run-hook-
   payload>"}`. The value you pass arrives as the **string** under
   `runHookPayload`, not at the top level.
   → the hook unwraps `outer["runHookPayload"]` then JSON-parses it to recover
   `coder_agent_token` / `coder_agent_url` / `coder_agent_init_b64` / `home_dir`.

5. **`run-microvm` takes no `--resources` and no `--tags`.** Size is baked into
   the (single-size) image at build via `create-microvm-image --resources
   [{minimumMemoryInMiB}]`; idempotency is via `clientToken`.
   → template no longer passes resources/tags on run; documented that CPU/mem
   parameters imply one image per size (see "Known gaps").

6. **Image versioning:** `update-microvm-image` always creates a NEW version
   (1.0 → 2.0 → …). Poll the version whose `state` is `IN_PROGRESS/PENDING`
   (not a single "latest" field).
   → `build.sh` poller rewritten to use `list-microvm-image-versions`.

7. **Runtime app stdout is NOT shipped to CloudWatch** (only build/validate logs
   are, under `/aws/lambda-microvms/<image>` streams named `<ver>/<buildId>`).
   → added a `GET /debug/run` endpoint to the hook server for introspection;
   for production, ship app logs explicitly if needed.

## IAM / prerequisites used

`images/coder-microvm-agent/provision-iam.sh` creates:
- S3 artifact bucket `coder-microvm-artifacts-<acct>-<region>` (same region as
  the image — cross-region is rejected).
- `coder-microvm-build-role` (trust `lambda.amazonaws.com` + `aws:SourceAccount`;
  s3:GetObject/PutObject + logs).
- `coder-microvm-exec-role` (same trust; Bedrock invoke + logs + EFS) — this is
  the workspace's AWS identity inside the MicroVM, replacing EKS IRSA.

## Update: no-CLI provisioning via a controller Lambda (validated)

The Coder provisioner image has **no `aws` CLI**, so the original
`null_resource` + `local-exec` path fails at build with `aws CLI not found`.
Replaced it with a **controller Lambda** (`infrastructure/microvm-controller`)
invoked from Terraform via `aws_lambda_invocation` (`lifecycle_scope = "CRUD"`).
Provisioner needs only the AWS provider + `lambda:InvokeFunction` — no CLI/boto3.

Validated live (invoking the deployed `coder-microvm-controller`):
- create → `{"microvm_id":"microvm-…","endpoint":"…"}`, reached `RUNNING`.
- idempotent 2nd create → **same** microvm_id (S3 index reuse).
- ingress `curl` → HTTP 200 `{"status":"ok","agent_started":true}` (token
  injected through the Lambda → run-hook path).
- delete → `{"terminated":"microvm-…"}`, VM terminated.

### IAM findings (important)

The `lambda-microvms` **API endpoint/CLI** is `lambda-microvms`, but the **IAM
action namespace is `lambda:`** (this is why a role with `lambda:*` can call it).
The controller role needs, at minimum:
- `lambda:RunMicrovm`, `lambda:GetMicrovm`, `lambda:TerminateMicrovm`,
  `lambda:ListMicrovms` (+ `Suspend/ResumeMicrovm`, `CreateMicrovmAuthToken`).
- **`iam:PassRole`** on the MicroVM execution role (no `PassedToService`
  condition — the MicroVM service principal is not `lambda.amazonaws.com`).
- **`lambda:PassNetworkConnector`** on the egress connector — even the DEFAULT
  `arn:aws:lambda:<region>:aws:network-connector:aws-network-connector:INTERNET_EGRESS`
  when no custom connector is supplied.
- `RunMicrovm` authorizes against **multiple resources at once** (image AND
  connector); grant all the above or it denies one resource at a time.

The provisioner’s ability to invoke the controller is granted by a **resource-
based policy** on the function (`add-permission --principal <account-id>`), so it
works regardless of the coderd provisioner’s identity-based policy.

### Deploy the controller

```bash
cd infrastructure/microvm-controller
AWS_REGION=us-east-1 \
  BUCKET=coder-microvm-artifacts-<acct>-us-east-1 \
  EXEC_ROLE_ARN=arn:aws:iam::<acct>:role/coder-microvm-exec-role \
  ./deploy.sh
# then set microvm_controller_function=coder-microvm-controller in the template.
```

## Update: agent must match the server version (validated)

Direct workspace tests surfaced two more issues, both fixed in `hooks/hook_server.py`:

1. **`runHookPayload` is capped at 4096 bytes** — the base64 Coder init script
   overflows it. The controller now inlines the init script only if it fits,
   else the hook launches the agent itself (below).
2. **Agent/server version skew** — the baked agent (v2.36.3, RPC API **2.10**)
   is rejected by the server (v2.34.4, RPC **2.9**):
   `400 Unknown or unsupported API version — server is at version 2.9, behind
   requested minor version 2.10`. Fix: the hook downloads the **server-matched**
   agent from `${CODER_AGENT_URL}/bin/coder-linux-arm64` (what Coder’s init
   script does) instead of using the baked binary.

After both fixes, a fresh workspace (`coder create`) reaches:
`dev (linux, arm64) ⦿ connected ✔ healthy v2.34.4` — the MicroVM agent connects
to the control plane and reports healthy. **Validated via the authenticated
coder CLI.**

### Remaining: SSH/data-plane needs DERP WebSocket mode (deployment setting)

`coder ssh`/`coder ping` to the MicroVM time out even though the agent is
healthy. In-guest agent logs show the tailnet DERP client getting **HTTP 426**
from `partner.coderdemo.io`:
`DERP server returned status 426 (a proxy could be disallowing 'Upgrade: derp')`.
The MicroVM reaches coderd through the public CloudFront/ALB, which strips the
raw `Upgrade: derp` header, so only the WebSocket DERP fallback works and the
WireGuard handshake to peers doesn’t complete.

This is a **deployment-level** matter (affects any client reaching coderd through
that proxy), not a template bug. Fix: set **`CODER_DERP_FORCE_WEBSOCKETS=true`**
on coderd (already present, commented out, in
`infrastructure/helm/coder-values.yaml`) so the DERP map advertises WebSocket
mode to all agents and clients.

### Debug endpoints (prototype only)

`hooks/hook_server.py` exposes `GET /debug/run`, `/debug/agent` (agent log tail),
and `/debug/exec?c=<base64>` (reachable only with a valid MicroVM proxy auth
token). These were invaluable for the above diagnosis but **must be removed or
gated before any non-prototype use.**

## Known gaps / not yet validated

- **Coder agent did not connect to a real control plane** in the test (a dummy
  token + a placeholder init script were injected to keep the VM up). Wiring the
  live `coder_agent.token` + `access_url` via the template is the next step.
- **EFS in-guest mount** not exercised (needs a VPC_EGRESS connector to the EFS
  mount targets on 2049); the hook's mount path is written but untested.
- **CPU/memory as coder_parameters** don't size a running MicroVM (single-size
  image). Either build one image per size or drop the sizing inputs.
- **Template not yet registered in Coder** (requires an admin token / template
  push); the AWS-side integration the template depends on is validated.
- **suspend/resume** lifecycle not yet tested end-to-end.

## Reproduce

```bash
cd images/coder-microvm-agent
AWS_REGION=us-east-1 ./provision-iam.sh          # bucket + roles (prints exports)
export AWS_REGION=us-east-1 ARCH=ARM_64
export BUCKET=coder-microvm-artifacts-<acct>-us-east-1
export BUILD_ROLE_ARN=arn:aws:iam::<acct>:role/coder-microvm-build-role
./build.sh coder-microvm-agent                    # build + wait for SUCCESSFUL
# then run-microvm / create-microvm-auth-token / curl (see README).
```
