terraform {
  required_version = ">= 1.9"
  required_providers {
    coder = {
      source  = "coder/coder"
      version = ">= 2.13"
    }
  }
}

# ──────────────────────────────────────────────────────────────────────────────
# Input variables
# ──────────────────────────────────────────────────────────────────────────────

variable "agent_id" {
  type        = string
  description = "The ID of the Coder agent in the workspace."
}

variable "agent_name" {
  type        = string
  description = <<-EOT
    The Coder agent's name (the coder_agent resource label, e.g. "dev"). This
    deployment serves subdomain apps at <slug>--<agent>--<workspace>--<owner>.<host>,
    so the agent name is REQUIRED to build the dashboard app's external origin.
  EOT
  default     = "dev"
}

variable "port" {
  type        = number
  description = "Port the KiroCrew gateway listens on inside the workspace."
  default     = 8899
}

variable "log_path" {
  type        = string
  description = "Path to write the KiroCrew gateway log."
  default     = "/tmp/kirocrew-gateway.log"
}

variable "install_prefix" {
  type        = string
  description = "Directory where Kiro CLI and KiroCrew are installed."
  default     = "/home/coder/.local/bin"
}

variable "order" {
  type        = number
  description = "Display order for the KiroCrew app in the Coder dashboard."
  default     = null
}

variable "group" {
  type        = string
  description = "App group for the KiroCrew app in the Coder dashboard."
  default     = null
}

variable "share" {
  type    = string
  default = "owner"
  validation {
    condition     = contains(["owner", "authenticated", "public"], var.share)
    error_message = "share must be 'owner', 'authenticated', or 'public'."
  }
}

variable "open_in" {
  type    = string
  default = "slim-window"
  validation {
    condition     = contains(["tab", "slim-window"], var.open_in)
    error_message = "open_in must be 'tab' or 'slim-window'."
  }
}

variable "use_cached" {
  type        = bool
  description = "Skip re-downloading Kiro CLI / KiroCrew if already present."
  default     = true
}

# Optional: pass a fully-rendered MCP JSON string to overwrite mcp.json.
# Leave empty to keep whatever mcp.json was already written by the startup script.
variable "extra_mcp_json" {
  type        = string
  description = "Optional JSON string to write/merge into ~/.kiro/settings/mcp.json."
  default     = ""
}

# Optional: AWS Builder ID external auth token, injected from a Coder
# external-auth data source at the template level.
variable "aws_builder_id_token" {
  type        = string
  description = "AWS Builder ID access token from coder_external_auth. Leave empty when not using Builder ID auth."
  default     = ""
  sensitive   = true
}

variable "allowed_origins" {
  type        = string
  description = <<-EOT
    Comma-separated extra origins the KiroCrew gateway should trust in its
    Host/Origin allowlist (its DNS-rebinding guard). The gateway only accepts
    loopback hosts by default, so a Coder reverse-proxy request arrives with a
    non-loopback Host and is rejected ("Host header not allowed."). Set this to
    the Coder access URL origin (path-based apps) and/or the app's subdomain
    origin (subdomain apps), e.g. "https://coder.example.com". Passed through to
    KIROCREW_CORS_ORIGINS.
  EOT
  default     = ""
}

variable "slug" {
  type        = string
  description = "Base slug for the visible KiroCrew app tile."
  default     = "kirocrew"
}

variable "redirect_port" {
  type        = number
  description = "Loopback port for the token-minting redirector that fronts the dashboard app so the Coder app tile self-authenticates."
  default     = 8898
}

# ──────────────────────────────────────────────────────────────────────────────
# Workspace identity (used to build the dashboard app's subdomain origin)
# ──────────────────────────────────────────────────────────────────────────────

data "coder_workspace" "me" {}
data "coder_workspace_owner" "me" {}

# ──────────────────────────────────────────────────────────────────────────────
# Locals
# ──────────────────────────────────────────────────────────────────────────────

locals {
  extra_mcp_b64 = var.extra_mcp_json != "" ? base64encode(var.extra_mcp_json) : ""
  aws_token_b64 = var.aws_builder_id_token != "" ? base64encode(var.aws_builder_id_token) : ""

  # This deployment is a basic CloudFront distribution with NO wildcard domain,
  # so Coder subdomain apps do not resolve (*.<host> is NXDOMAIN) and the
  # KiroCrew dashboard SPA (which must be served at the root of its own origin)
  # cannot be proxied as a subdomain/path Coder app. Default instead to the
  # localhost / `coder port-forward` model KiroCrew is built for: the gateway
  # binds localhost:<port>, dashboard.url is the localhost origin (correct
  # cookie/CORS scope for a forwarded port), and the visible tile is a small
  # path-based launcher page (the redirector) that prints the port-forward
  # command + a freshly-minted, tokenized http://localhost:<port>/ URL. A
  # consumer that later adds a custom domain + wildcard DNS/Route53 can switch
  # these back to subdomain apps for a one-click dashboard.
  dashboard_origin        = "http://localhost:${var.port}"
  gateway_allowed_origins = trimspace(var.allowed_origins)
}

# ──────────────────────────────────────────────────────────────────────────────
# Startup script: install + launch KiroCrew gateway
# ──────────────────────────────────────────────────────────────────────────────

resource "coder_script" "kirocrew" {
  agent_id           = var.agent_id
  display_name       = "KiroCrew Gateway"
  icon               = "/icon/kiro.svg"
  run_on_start       = true
  start_blocks_login = false

  script = templatefile("${path.module}/run.sh", {
    INSTALL_PREFIX  = var.install_prefix
    PORT            = var.port
    LOG_PATH        = var.log_path
    USE_CACHED      = var.use_cached
    EXTRA_MCP_B64   = local.extra_mcp_b64
    AWS_TOKEN_B64   = local.aws_token_b64
    ALLOWED_ORIGINS = local.gateway_allowed_origins
    DASHBOARD_URL   = local.dashboard_origin
    REDIRECT_PORT   = var.redirect_port
    WS_NAME         = data.coder_workspace.me.name
    OWNER           = data.coder_workspace_owner.me.name
  })
}

# ──────────────────────────────────────────────────────────────────────────────
# Coder app
#
# No wildcard domain on this deployment, so subdomain apps don't resolve. The
# visible "KiroCrew" tile is a PATH-BASED app (subdomain=false) that serves a
# small launcher page (the redirector in run.sh): it prints the `coder
# port-forward` command and a freshly-minted, tokenized http://localhost:<port>/
# URL. The dashboard SPA is served by the gateway on localhost:<port> and is
# reached over that forwarded port (an SPA can't be proxied under a Coder path
# prefix). Add a custom domain + wildcard DNS later to switch back to a
# one-click subdomain dashboard.
# ──────────────────────────────────────────────────────────────────────────────

resource "coder_app" "kirocrew" {
  agent_id     = var.agent_id
  slug         = var.slug
  display_name = "KiroCrew"
  url          = "http://localhost:${var.redirect_port}/"
  icon         = "/icon/kiro.svg"
  subdomain    = false
  share        = var.share
  order        = var.order
  group        = var.group
  open_in      = "tab"

  healthcheck {
    url       = "http://localhost:${var.redirect_port}/healthz"
    interval  = 5
    threshold = 6
  }
}

# ──────────────────────────────────────────────────────────────────────────────
# Outputs
# ──────────────────────────────────────────────────────────────────────────────

output "kirocrew_url" {
  value       = "http://localhost:${var.port}/"
  description = "Local URL for the KiroCrew gateway."
}
