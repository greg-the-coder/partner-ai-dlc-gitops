# coder-microvm-agent — PROTOTYPE MicroVM image
#
# Built into a Firecracker snapshot by `aws lambda-microvms create-microvm-image`
# (see build.sh), NOT pushed to ECR like the Fargate workspace images. The build
# packages this directory as a zip in S3 and points create-microvm-image at it
# with a Lambda-published AL2023 base image (--base-image-arn).
#
# Base registry: use public.ecr.aws/lambda/microvms:al2023-minimal so a plain
# `docker build` reproduces the MicroVM base locally (see test/local-hook-test.sh).
#
# Bake the heavy agent toolchain + uv/uvx MCP cache in here so it lands in the
# snapshot and every RunMicrovm starts near-instantly (the main win over Fargate
# cold-starts pulling a multi-GB image each launch).
#
# IMPORTANT:
#  * Build with additional-os-capabilities=["ALL"] so the /run hook can mount EFS.
#  * Do NOT bake any per-workspace secret here — the snapshot's memory state is
#    shared across all runs; the Coder agent token is injected at run time via
#    the /run hook payload.
#  * Installs below are best-effort (|| true) so a transient package/repo issue
#    does not fail the whole snapshot build; the hook server itself needs only
#    the Python stdlib.

FROM public.ecr.aws/lambda/microvms:al2023-minimal

ENV HOME=/home/coder \
    HOOK_PORT=9000 \
    UV_CACHE_DIR=/opt/uv-cache \
    PATH=/home/coder/.local/bin:/home/coder/bin:/usr/local/bin:/usr/bin:/bin

# System deps: python (hook server + stdlib http.server), NFS/EFS mount utils,
# git/tar, shadow (useradd). amazon-efs-utils may be absent on minimal repos —
# tolerate it (EFS mount is only needed on the full workspace path).
RUN dnf -y install python3 tar gzip which shadow-utils util-linux nfs-utils git \
      && (dnf -y install amazon-efs-utils || echo "WARN: amazon-efs-utils unavailable; EFS mount path degraded") \
      && dnf clean all

# Non-root workspace user (uid/gid 1000 to match the EFS access point posix_user).
RUN groupadd -g 1000 coder 2>/dev/null || true; \
    useradd -m -u 1000 -g 1000 -s /bin/bash coder 2>/dev/null || true

# Coder agent CLI baked in (the /run hook can also exec the injected init
# script; this is the fallback path and keeps first-connect fast). Best-effort.
RUN (curl -fsSL https://coder.com/install.sh | sh -s -- --version 2.36.3) \
      || echo "WARN: coder install skipped/failed; /run will exec the injected init script instead"

# uv/uvx + warm cache for the AWS MCP servers (parity with the Fargate image so
# MCP servers connect on first call). Best-effort.
RUN (curl -LsSf https://astral.sh/uv/install.sh | sh) || echo "WARN: uv install skipped"; \
    mkdir -p /opt/uv-cache

# Lifecycle hook server (Python stdlib only — no extra deps required).
COPY hooks/hook_server.py /opt/hooks/hook_server.py

# The MicroVM entrypoint IS the hook server; /run mounts EFS + launches the agent.
EXPOSE 8080 9000
CMD ["python3", "/opt/hooks/hook_server.py"]
