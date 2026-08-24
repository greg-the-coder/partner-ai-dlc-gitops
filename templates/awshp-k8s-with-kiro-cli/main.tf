terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "2.37.1"
    }
    coder = {
      source  = "coder/coder"
      version = ">= 2.13"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.2"
    }
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

variable "namespace" {
  type        = string
  description = "The Kubernetes namespace to create workspaces in (must exist prior to creating workspaces). If the Coder host is itself running as a Pod on the same Kubernetes cluster as you are deploying workspaces to, set this to the same namespace."
  default     = "coder-ws"
}

variable "workspace_image" {
  type        = string
  description = "Container image for workspace pods"
  default     = "codercom/enterprise-base:ubuntu"
}

variable "efs_file_system_id" {
  type        = string
  description = "EFS file system ID for persistent workspace storage"
  default     = ""
}

locals {
  home_dir = "/home/coder"

  # AWS MCP servers wired into Kiro CLI (mcp.json). All run over stdio via `uvx`
  # (pinned @latest) with quiet logging, giving the workshop agents first-class
  # AWS documentation, CDK, Terraform, and diagram tooling out of the box.
  # See https://github.com/awslabs/mcp for each server's capabilities.
  mcp_servers = {
    "awslabs.core-mcp-server" = {
      command = "uvx"
      args    = ["awslabs.core-mcp-server@latest"]
      env     = { FASTMCP_LOG_LEVEL = "ERROR" }
    }
    "awslabs.aws-documentation-mcp-server" = {
      command = "uvx"
      args    = ["awslabs.aws-documentation-mcp-server@latest"]
      env     = { FASTMCP_LOG_LEVEL = "ERROR", AWS_DOCUMENTATION_PARTITION = "aws" }
    }
    "awslabs.cdk-mcp-server" = {
      command = "uvx"
      args    = ["awslabs.cdk-mcp-server@latest"]
      env     = { FASTMCP_LOG_LEVEL = "ERROR" }
    }
    "awslabs.aws-diagram-mcp-server" = {
      command = "uvx"
      args    = ["awslabs.aws-diagram-mcp-server@latest"]
      env     = { FASTMCP_LOG_LEVEL = "ERROR" }
    }
    "awslabs.terraform-mcp-server" = {
      command = "uvx"
      args    = ["awslabs.terraform-mcp-server@latest"]
      env     = { FASTMCP_LOG_LEVEL = "ERROR" }
    }
  }
  mcp_json = jsonencode({ mcpServers = local.mcp_servers })
}

# Minimum vCPUs needed 
data "coder_parameter" "cpu" {
  name        = "CPU cores"
  type        = "number"
  description = "CPU cores for your individual workspace"
  icon        = "https://png.pngtree.com/png-clipart/20191122/original/pngtree-processor-icon-png-image_5165793.jpg"
  validation {
    min = 2
    max = 8
  }
  form_type = "input"
  mutable   = true
  default   = 2
  order     = 1
}

# Minimum GB memory needed 
data "coder_parameter" "memory" {
  name        = "Memory (__ GB)"
  type        = "number"
  description = "Memory (__ GB) for your individual workspace"
  icon        = "https://www.vhv.rs/dpng/d/33-338595_random-access-memory-logo-hd-png-download.png"
  validation {
    min = 4
    max = 16
  }
  form_type = "input"
  mutable   = true
  default   = 4
  order     = 2
}


# Compute lane: which schedulable surface this workspace runs on. The home
# directory is EFS-backed (ReadWriteMany) in BOTH lanes; only pod scheduling differs.
data "coder_parameter" "compute_lane" {
  name         = "Compute Lane"
  display_name = "Compute Lane"
  description  = "fargate = serverless, Firecracker microVM isolation. spot = EC2 Spot node group, auto-scaled by EKS Auto Mode (lower cost, allows larger/GPU/privileged workloads). Home directory is on EFS in either lane."
  type         = "string"
  default      = "fargate"
  mutable      = true
  order        = 3
  icon         = "/icon/aws.png"
  option {
    name  = "Fargate (serverless, isolated)"
    value = "fargate"
    icon  = "/icon/aws.png"
  }
  option {
    name  = "EC2 Spot (auto-scaled, low cost)"
    value = "spot"
    icon  = "/icon/aws.png"
  }
}

# Opt-in KiroCrew: multi-agent Kiro orchestration gateway + dashboard. When
# enabled the kirocrew module installs the KiroCrew CLI/gateway and surfaces a
# self-authenticating dashboard app tile. Disabled by default to keep the base
# workspace lean.
data "coder_parameter" "enable_kirocrew" {
  name         = "enable_kirocrew"
  display_name = "Enable KiroCrew"
  description  = "Install KiroCrew (multi-agent Kiro orchestration) and expose its dashboard as a Coder app. Adds ~1-2 min to first start while the gateway + managed Python venv are provisioned."
  type         = "bool"
  default      = "false"
  mutable      = true
  order        = 4
  icon         = "/icon/kiro.svg"
}

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

locals {
  cost        = 2
  home_folder = "/home/coder"
}

resource "coder_agent" "dev" {
  arch = "amd64"
  os   = "linux"

  env = {
    PATH = "/home/coder/.local/bin:/home/coder/bin:/home/coder/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  }
  display_apps {
    vscode          = false
    vscode_insiders = false
    web_terminal    = true
    ssh_helper      = false
  }

  # Live workspace resource utilization shown in the Coder dashboard,
  # using the agent's built-in `coder stat` command (pod/container-scoped).
  metadata {
    display_name = "CPU Usage"
    key          = "0_cpu_usage"
    script       = "coder stat cpu"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "RAM Usage"
    key          = "1_ram_usage"
    script       = "coder stat mem"
    interval     = 10
    timeout      = 1
  }

  metadata {
    display_name = "Home Disk"
    key          = "2_home_disk"
    script       = "coder stat disk --path $HOME"
    interval     = 60
    timeout      = 1
  }
  startup_script = <<-EOT
    set -e

    # Create persistent bin directory
    mkdir -p $HOME/bin
    mkdir -p $HOME/.local/bin

    # Update PATH for current session
    export PATH="$HOME/.local/bin:$HOME/bin:$HOME/.npm-global/bin:$PATH"

    # Ensure uvx is available for the AWS MCP servers (each runs over stdio via
    # `uvx`). The workshop image pre-bakes uv/uvx under /usr/local/bin, so this is
    # a no-op there; otherwise it installs into $HOME/.local/bin (on PATH above).
    if ! command -v uvx >/dev/null 2>&1; then
      export UV_UNMANAGED_INSTALL="$HOME/.local/bin"
      curl -LsSf https://astral.sh/uv/install.sh | sh || true
    fi

    # The AWS diagram MCP server renders via graphviz's `dot`; install it once if
    # missing so diagram generation works (best-effort, non-fatal).
    if ! command -v dot >/dev/null 2>&1; then
      sudo apt-get update -qq || true
      sudo apt-get install -y graphviz >/dev/null 2>&1 || true
    fi

    # Configure Kiro CLI MCP servers (AWS: core, docs, CDK, diagram, terraform)
    echo "Configuring Kiro CLI MCP servers..."

    # Create user-level MCP configuration
    mkdir -p $HOME/.kiro/settings
    cat > $HOME/.kiro/settings/mcp.json <<'MCP_EOF'
${local.mcp_json}
MCP_EOF

    echo "Kiro CLI MCP configuration completed (user-level)"

    # KiroCrew prerequisites (only when the KiroCrew option is enabled). KiroCrew
    # provisions a managed Python venv (>=3.10) and its installer verifies a
    # signed manifest with openssl/sha256sum. Best-effort; the module's script
    # installs the CLI/gateway and its healthcheck surfaces any failure.
    if [ "${data.coder_parameter.enable_kirocrew.value}" = "true" ]; then
      echo "Ensuring KiroCrew prerequisites..."
      if ! python3 -m venv --help >/dev/null 2>&1; then
        sudo apt-get update -qq || true
        sudo apt-get install -y python3-venv python3-dev >/dev/null 2>&1 || true
      fi
    fi

    # Configure workspace trust settings for Kiro IDE
    echo "Configuring Kiro IDE workspace trust..."
    mkdir -p $HOME/.local/share/code-server/User

    # Create or update settings.json to trust the home folder
    cat > $HOME/.local/share/code-server/User/settings.json <<'SETTINGS_EOF'
{
  "security.workspace.trust.enabled": true,
  "security.workspace.trust.startupPrompt": "never",
  "security.workspace.trust.emptyWindow": false,
  "security.workspace.trust.untrustedFiles": "open"
}
SETTINGS_EOF

    # Add trusted folders configuration
    mkdir -p $HOME/.kiro/settings
    cat > $HOME/.kiro/settings/trusted-workspaces.json <<'TRUST_EOF'
{
  "trustedFolders": [
    "/home/coder"
  ]
}
TRUST_EOF

    echo "Kiro IDE workspace trust configuration completed"

    #Symlink Coder Agent
    ln -sf /tmp/coder.*/coder "$CODER_SCRIPT_BIN_DIR/coder"

    EOT

}

module "coder-login" {
  source   = "registry.coder.com/coder/coder-login/coder"
  version  = "1.1.1"
  agent_id = coder_agent.dev.id
}

# Python 3.12 venv + Jupyter kernel for the workshop agent notebooks
# (LangGraph/LangChain, LlamaIndex, Strands, Bedrock AgentCore). The workshop
# images pre-bake this at /opt/venvs/agents with a system-wide "Python (Agents)"
# kernel, so this script is a fast no-op there. On a non pre-baked base image it
# falls back to provisioning into the EFS-persistent home (one-time).
resource "coder_script" "agent_python_kernel" {
  agent_id           = coder_agent.dev.id
  display_name       = "Python/Jupyter agent kernel"
  icon               = "/icon/python.svg"
  run_on_start       = true
  start_blocks_login = false
  script             = <<-EOT
    #!/bin/sh
    set -eu

    # Fast path: pre-baked in the workshop image (outside the EFS-mounted home).
    if [ -x /opt/venvs/agents/bin/python ]; then
      echo "Agent Python kernel pre-installed in image (/opt/venvs/agents)."
      exit 0
    fi

    # Fallback for non pre-baked base images: provision into the persistent home.
    VENV="$HOME/.venvs/agents"
    SENTINEL="$VENV/.provisioned"
    if [ -f "$SENTINEL" ]; then
      echo "Agent Python kernel already provisioned at $VENV"
      exit 0
    fi
    command -v uv >/dev/null 2>&1 || { echo "uv unavailable; skipping kernel setup."; exit 0; }

    export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
    export UV_LINK_MODE=copy
    mkdir -p "$HOME/.venvs"
    uv venv --python 3.12 --seed "$VENV"
    uv pip install --python "$VENV/bin/python" \
      ipykernel \
      "boto3>=1.39.0" "botocore>=1.39.0" "pydantic>=2.0.0" \
      bedrock-agentcore bedrock-agentcore-starter-toolkit \
      langchain langchain-core langchain-aws langchain-anthropic langchain-community langgraph \
      "llama-index>=0.12.0" llama-index-core llama-index-llms-bedrock \
      llama-index-llms-bedrock-converse llama-index-embeddings-bedrock \
      llama-index-readers-file llama-cloud \
      strands-agents strands-agents-tools
    "$VENV/bin/python" -m ipykernel install --user \
      --name agents --display-name "Python (Agents)"
    touch "$SENTINEL"
    echo "Provisioned Jupyter kernel 'Python (Agents)' -> $VENV"
    EOT
}

module "code-server" {
  source     = "registry.coder.com/coder/code-server/coder"
  version    = "1.5.2"
  agent_id   = coder_agent.dev.id
  folder     = local.home_folder
  subdomain  = false
  order      = 0
  extensions = ["ms-toolsai.jupyter"]
}

module "kiro" {
  source   = "registry.coder.com/coder/kiro/coder"
  version  = "1.2.1"
  agent_id = coder_agent.dev.id
  order    = 1
}

# Opt-in KiroCrew gateway + self-authenticating dashboard app. Instantiated only
# when the enable_kirocrew parameter is set. The module derives the dashboard
# app's subdomain origin (which, in this deployment, INCLUDES the agent-name
# segment "dev"), trusts it in the gateway Host/Origin allowlist, sets
# dashboard.url, and runs a token-minting redirector so the visible tile
# self-authenticates.
module "kirocrew" {
  count      = data.coder_parameter.enable_kirocrew.value == "true" ? 1 : 0
  source     = "./modules/kirocrew"
  agent_id   = coder_agent.dev.id
  agent_name = "dev"
  port       = 8899
  use_cached = true
  order      = 3
}

# Auto-install the Jupyter extension for the Kiro IDE.
# Kiro connects as a desktop client and downloads its remote server on first
# connect, so we install into the (EFS-persistent) Kiro server extensions dir:
# immediately if the server is already present, otherwise via a one-time
# background poller. Dependencies resolve automatically from Open VSX.
resource "coder_script" "kiro_jupyter_extension" {
  agent_id           = coder_agent.dev.id
  display_name       = "Kiro: install Jupyter extension"
  icon               = "/icon/kiro.svg"
  run_on_start       = true
  start_blocks_login = false
  script             = <<-EOT
    #!/bin/sh
    set -eu
    EXT_ID="ms-toolsai.jupyter"
    KIRO_BIN="$HOME/.kiro-server/bin"
    SENTINEL="$HOME/.kiro-server/.jupyter-ext-installed"

    if [ -f "$SENTINEL" ]; then
      echo "Kiro: $EXT_ID already provisioned."
      exit 0
    fi

    install_ext() {
      SRV=$(find "$KIRO_BIN" -maxdepth 3 -type f -name kiro-server 2>/dev/null | head -1)
      [ -n "$SRV" ] || return 1
      "$SRV" --install-extension "$EXT_ID" && touch "$SENTINEL"
    }

    if install_ext; then
      echo "Kiro: installed $EXT_ID."
    else
      # Server not downloaded yet (first connect pending) - poll in background.
      (
        i=0
        while [ "$i" -lt 120 ]; do
          sleep 30
          if install_ext; then
            echo "Kiro: installed $EXT_ID after connect."
            break
          fi
          i=$((i + 1))
        done
      ) >/tmp/kiro-jupyter-install.log 2>&1 &
      echo "Kiro: will install $EXT_ID on first connect (background)."
    fi
    EOT
}

resource "coder_app" "kiro_cli" {
  agent_id     = coder_agent.dev.id
  slug         = "kiro-auth"
  display_name = "Kiro CLI"
  icon         = "${data.coder_workspace.me.access_url}/icon/kiro.svg"
  command      = "kiro-cli"
  share        = "owner"
  order        = 2
}


resource "aws_efs_access_point" "home" {
  file_system_id = var.efs_file_system_id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/workspaces/${data.coder_workspace.me.id}"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = {
    Name                     = "coder-${data.coder_workspace.me.name}-home"
    "com.coder.workspace.id" = data.coder_workspace.me.id
  }
}

resource "kubernetes_persistent_volume" "home" {
  metadata {
    name = "coder-${data.coder_workspace.me.id}-home"
  }
  spec {
    capacity = {
      storage = "50Gi"
    }
    access_modes                     = ["ReadWriteMany"]
    persistent_volume_reclaim_policy = "Retain"
    storage_class_name               = "efs-static"
    volume_mode                      = "Filesystem"
    persistent_volume_source {
      csi {
        driver        = "efs.csi.aws.com"
        volume_handle = "${var.efs_file_system_id}::${aws_efs_access_point.home.id}"
      }
    }
  }
}

resource "kubernetes_persistent_volume_claim" "home" {
  metadata {
    name      = "coder-${data.coder_workspace.me.id}-home"
    namespace = var.namespace
  }
  wait_until_bound = true
  spec {
    access_modes       = ["ReadWriteMany"]
    storage_class_name = "efs-static"
    volume_name        = kubernetes_persistent_volume.home.metadata.0.name
    resources {
      requests = {
        storage = "50Gi"
      }
    }
  }
}

resource "kubernetes_deployment" "dev" {
  count            = data.coder_workspace.me.start_count
  wait_for_rollout = false
  metadata {
    name      = "coder-${data.coder_workspace.me.id}"
    namespace = var.namespace
    labels = {
      "app.kubernetes.io/name"     = "coder-workspace"
      "app.kubernetes.io/instance" = "coder-workspace-${data.coder_workspace.me.id}"
      "app.kubernetes.io/part-of"  = "coder"
      "com.coder.resource"         = "true"
      "com.coder.workspace.id"     = data.coder_workspace.me.id
      "com.coder.workspace.name"   = data.coder_workspace.me.name
      "com.coder.user.id"          = data.coder_workspace_owner.me.id
      "com.coder.user.username"    = data.coder_workspace_owner.me.name
    }
    annotations = {
      "com.coder.user.email" = data.coder_workspace_owner.me.email
    }
  }

  spec {
    replicas = 1
    selector {
      match_labels = {
        "app.kubernetes.io/name"     = "coder-workspace"
        "app.kubernetes.io/instance" = "coder-workspace-${data.coder_workspace.me.id}"
        "app.kubernetes.io/part-of"  = "coder"
        "com.coder.resource"         = "true"
        "com.coder.workspace.id"     = data.coder_workspace.me.id
        "com.coder.workspace.name"   = data.coder_workspace.me.name
        "com.coder.user.id"          = data.coder_workspace_owner.me.id
        "com.coder.user.username"    = data.coder_workspace_owner.me.name
      }
    }
    strategy {
      type = "Recreate"
    }

    template {
      metadata {
        labels = {
          "app.kubernetes.io/name"     = "coder-workspace"
          "app.kubernetes.io/instance" = "coder-workspace-${data.coder_workspace.me.id}"
          "app.kubernetes.io/part-of"  = "coder"
          "com.coder.resource"         = "true"
          "com.coder.workspace.id"     = data.coder_workspace.me.id
          "com.coder.workspace.name"   = data.coder_workspace.me.name
          "com.coder.user.id"          = data.coder_workspace_owner.me.id
          "com.coder.user.username"    = data.coder_workspace_owner.me.name
          # Compute-lane selector: matched by the EKS Fargate profile
          # (compute=fargate) or excluded by it (compute=spot -> EC2 Spot NodePool).
          "compute" = data.coder_parameter.compute_lane.value
        }
      }
      spec {
        security_context {
          run_as_user = 1000
          fs_group    = 1000
        }
        service_account_name = "coder-ws"

        # Spot lane: pin to the auto-scaled EKS Auto Mode Spot NodePool and tolerate
        # its taint. Fargate lane: leave empty so the Fargate profile schedules the
        # compute=fargate pod serverlessly.
        node_selector = data.coder_parameter.compute_lane.value == "spot" ? { "coder.workspace/lane" = "spot" } : {}
        dynamic "toleration" {
          for_each = data.coder_parameter.compute_lane.value == "spot" ? [1] : []
          content {
            key      = "coder.workspace/lane"
            operator = "Equal"
            value    = "spot"
            effect   = "NoSchedule"
          }
        }
        container {
          name              = "dev"
          image             = var.workspace_image
          image_pull_policy = "Always"
          command           = ["sh", "-c", coder_agent.dev.init_script]
          security_context {
            run_as_user                = "1000"
            allow_privilege_escalation = false
          }
          env {
            name  = "CODER_AGENT_TOKEN"
            value = coder_agent.dev.token
          }
          resources {
            requests = {
              "cpu"    = "${data.coder_parameter.cpu.value}"
              "memory" = "${data.coder_parameter.memory.value}Gi"
            }
            limits = {
              "cpu"    = "${data.coder_parameter.cpu.value}"
              "memory" = "${data.coder_parameter.memory.value}Gi"
            }
          }
          volume_mount {
            mount_path = "/home/coder"
            name       = "home"
            read_only  = false
          }
        }

        volume {
          name = "home"
          persistent_volume_claim {
            claim_name = kubernetes_persistent_volume_claim.home.metadata.0.name
            read_only  = false
          }
        }

      }
    }
  }
}

resource "coder_metadata" "pod_info" {
  count       = data.coder_workspace.me.start_count
  resource_id = kubernetes_deployment.dev[0].id
  daily_cost  = local.cost
}
