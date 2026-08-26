FROM public.ecr.aws/docker/library/ubuntu:22.04

# ==========================================================================
# Shared base image for all Coder workspace images (claude-code, kiro-cli,
# challenge). Everything common lives here so the expensive layers -- notably
# the Playwright/Chromium install and the /opt/venvs/agents agent-framework
# venv -- are built ONCE instead of once per image. Derived images do
# `FROM ${BASE_IMAGE}` and add only their unique tooling.
# ==========================================================================

# Avoid interactive prompts during package install
ENV DEBIAN_FRONTEND=noninteractive

# Create coder user (uid 1000) to match Fargate security context
RUN useradd -m -s /bin/bash -u 1000 coder

# System packages (common to all workspace images)
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    gnupg \
    dirmngr \
    jq \
    git \
    ca-certificates \
    python3 \
    python3-pip \
    bash \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip

# AWS CDK
RUN npm install -g aws-cdk

# uv (Python package manager / uvx)
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_UNMANAGED_INSTALL=/usr/local/bin sh

# Playwright headless browser for web scraping (used by agents to access workshop content)
RUN pip3 install playwright \
    && playwright install chromium --with-deps

# --- Pre-baked Python 3.12 virtualenv + Jupyter kernel for agent notebooks ---
# Baked at build time OUTSIDE /home/coder (which is overlaid by the EFS
# persistent volume at runtime) so workspaces start fast. Registers a
# system-wide Jupyter kernel "Python (Agents)" discoverable by code-server/Kiro.
#
# GPUs are never enabled on these workspaces, so install CPU-only PyTorch first
# from the PyTorch CPU index. This satisfies protopia's torch dependency and
# prevents the multi-GB NVIDIA CUDA wheels (torch's default deps) from being
# pulled in. --no-cache keeps the uv download cache out of the image layer.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN uv venv --python 3.12 --seed /opt/venvs/agents \
    && UV_LINK_MODE=copy uv pip install --no-cache --python /opt/venvs/agents/bin/python \
        torch --index-url https://download.pytorch.org/whl/cpu \
    && UV_LINK_MODE=copy uv pip install --no-cache --python /opt/venvs/agents/bin/python \
        ipykernel \
        "boto3>=1.39.0" "botocore>=1.39.0" "pydantic>=2.0.0" \
        bedrock-agentcore bedrock-agentcore-starter-toolkit \
        langchain langchain-core langchain-aws langchain-anthropic langchain-openai langchain-community langgraph \
        "llama-index>=0.12.0" llama-index-core llama-index-llms-bedrock \
        llama-index-llms-bedrock-converse llama-index-embeddings-bedrock llama-index-llms-openai \
        llama-index-readers-file llama-cloud \
        openai \
        strands-agents strands-agents-tools \
        hiddenlayer-sdk protopia \
    && /opt/venvs/agents/bin/python -m ipykernel install --prefix=/usr/local \
        --name agents --display-name "Python (Agents)" \
    && chown -R coder:coder /opt/venvs /opt/uv \
    && chmod -R a+rX /opt/venvs /opt/uv \
    && rm -rf /root/.cache/uv /root/.cache/pip

# Ensure coder user owns their home directory
RUN chown -R coder:coder /home/coder

# Make /usr/local/bin writable by coder user (Coder modules use sudo mv to install binaries there)
RUN chmod 777 /usr/local/bin

# Install a no-op sudo wrapper (Coder modules expect sudo to exist for `sudo mv`)
RUN printf '#!/bin/sh\nexec "$@"\n' > /usr/bin/sudo && chmod +x /usr/bin/sudo

# Install a `coder` CLI shim on PATH so the agent CLI is ALWAYS resolvable for
# coder_scripts from container start. The agent extracts its real binary to
# /tmp/coder.<random>/coder before running scripts, but templates that pin a
# fixed agent PATH (coder_env.path) drop that directory from the coder_script
# PATH. Module install pipelines (coder-utils) call `coder exp sync` on the
# FIRST line of each wrapped script, so if `coder` isn't on PATH yet they exit
# 127 and the agent reports a start_error. A startup/pre_install symlink can't
# win that race reliably because those scripts run concurrently. This shim lives
# on the image PATH (/usr/local/bin, see ENV PATH below) from t=0 and execs the
# real runtime binary, eliminating the race for every template and workspace.
RUN printf '%s\n' \
    '#!/bin/sh' \
    '# Coder agent CLI shim -- exec the agent runtime binary (see Dockerfile).' \
    'for c in /tmp/coder.*/coder /tmp/coder-script-data/bin/coder; do' \
    '  [ -x "$c" ] && exec "$c" "$@"' \
    'done' \
    'echo "coder: agent CLI binary not found under /tmp/coder.*/coder" >&2' \
    'exit 127' \
    > /usr/local/bin/coder \
    && chmod 755 /usr/local/bin/coder

# Pre-create the uv cache directory OUTSIDE /home/coder. At runtime the EFS
# persistent volume overlays /home/coder, which would hide a cache baked there,
# so the pre-warmed MCP packages must live on /opt (part of the image, never
# overlaid). Hand it to the coder user so uvx can also write to it at runtime.
RUN mkdir -p /opt/uv-cache && chown coder:coder /opt/uv-cache

USER coder
WORKDIR /home/coder

# Pre-configure npm global directory
RUN mkdir -p /home/coder/.npm-global \
    && npm config set prefix "/home/coder/.npm-global"

ENV PATH="/home/coder/.local/bin:/home/coder/bin:/home/coder/.npm-global/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Point uv/uvx at the persistent on-image cache created above. This ENV is
# inherited by the workspace container at runtime, so agent scripts and the MCP
# servers all use /opt/uv-cache (populated below) instead of the cold, EFS-backed
# ~/.cache/uv.
ENV UV_CACHE_DIR=/opt/uv-cache

# Pre-warm the uv cache with the AWS MCP servers so `uvx` first-connect is fast
# instead of timing out against the MCP startup probe (Claude Code ~30s; the
# Kiro IDE ~60s) on a brand-new workspace. The heavy work -- resolving and
# building each package's wheels -- is cached here at build time; because the
# versions are PINNED (identical to local.(aws_)mcp_servers in the templates),
# at runtime `uvx <pkg>==<ver>` is an offline cache hit and needs no index
# check or download. (Earlier the servers used @latest, so package drift
# between image build and workspace start caused a cold re-download that timed
# out the IDE MCP connect.) `--help`/`timeout`/`</dev/null` just trigger the
# resolve-and-cache then exit. KEEP THESE VERSIONS IN SYNC with
# local.(aws_)mcp_servers in templates/awshp-k8s-with-{claude-code,kiro-cli}/main.tf.
# (The challenge template has no MCP servers and simply carries the extra cache.)
RUN set -ux; for p in \
      mcp-proxy-for-aws@1.6.4 \
      awslabs.aws-iac-mcp-server==1.0.24 \
      awslabs.aws-pricing-mcp-server==1.1.0 \
      awslabs.aws-serverless-mcp-server==0.2.0 \
      awslabs.cloudwatch-mcp-server==0.2.0 ; do \
      echo "Pre-warming $p"; \
      timeout 180 uvx "$p" --help </dev/null >/dev/null 2>&1 || true; \
    done; \
    du -sh /opt/uv-cache || true
