# coder-microvm-agent — PROTOTYPE MicroVM image
#
# Built into a Firecracker snapshot by `aws lambda-microvms create-microvm-image`
# (NOT pushed to ECR like the Fargate workspace images). The build packages this
# directory as a zip in S3 and points create-microvm-image at it with a
# Lambda-published AL2023 base image.
#
# Bake the heavy agent toolchain + uv/uvx MCP cache in here so it lands in the
# snapshot and every RunMicrovm starts near-instantly (this is the main win over
# Fargate cold-starts pulling a multi-GB image each launch).
#
# IMPORTANT: build the image with additional-os-capabilities=["ALL"] so the
# /run hook can mount EFS. Do NOT bake any per-workspace secret here — the
# snapshot's memory state is shared across all runs; the Coder agent token is
# injected at run time via the /run hook payload.

# Base image is provided by create-microvm-image via --base-image-arn
# (e.g. arn:aws:lambda:<region>:aws:microvm-image:al2023-1). This FROM line is a
# local-build convenience so the image can also be exercised with plain docker.
FROM public.ecr.aws/amazonlinux/amazonlinux:2023

ENV HOME=/home/coder \
    HOOK_PORT=9000 \
    PATH=/home/coder/.local/bin:/home/coder/bin:/usr/local/bin:/usr/bin:/bin

# System deps: python (hook server), NFS/EFS mount utils, git, tar, shadow.
RUN dnf -y update && \
    dnf -y install python3 python3-pip git tar gzip which shadow-utils \
      util-linux nfs-utils amazon-efs-utils && \
    dnf clean all

# Non-root workspace user (uid/gid 1000 to match the EFS access point posix_user).
RUN groupadd -g 1000 coder && \
    useradd -m -u 1000 -g 1000 -s /bin/bash coder

# Coder agent CLI baked in (the /run hook can also exec the injected init
# script; this is the fallback path and keeps first-connect fast).
RUN curl -fsSL https://coder.com/install.sh | sh -s -- --version 2.36.3 || \
    echo "TODO: pin/verify coder install for the target deployment version"

# uv/uvx + a warm cache for the AWS MCP servers, matching the Fargate image so
# MCP servers connect on first call instead of cold-starting (which otherwise
# trips Claude Code's 30s MCP probe — see the K8s template's MCP_TIMEOUT note).
ENV UV_CACHE_DIR=/opt/uv-cache
RUN curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mkdir -p /opt/uv-cache

# Lifecycle hook server.
COPY hooks/hook_server.py /opt/hooks/hook_server.py

# The MicroVM entrypoint IS the hook server; /run launches the Coder agent.
EXPOSE 9000
CMD ["python3", "/opt/hooks/hook_server.py"]
