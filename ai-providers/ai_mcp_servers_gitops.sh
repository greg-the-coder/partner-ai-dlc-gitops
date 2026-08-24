#!/bin/bash
# Register external MCP servers for **Coder Agents** (AI Settings > Coder Agents >
# MCP servers) on the current Coder deployment via the admin REST API.
#
# There is no `coderd` Terraform resource for Coder Agents MCP servers (unlike
# coderd_ai_provider / coderd_agents_model), so this mirrors how the AI providers
# were ORIGINALLY wired — a direct, idempotent API call:
#
#   POST /api/v2/organizations/{org}/mcp-servers   (CreateMCPServerConfigRequest)
#
# It registers the AWS Knowledge MCP Server: a remote, fully-managed, AWS-hosted
# MCP server (no install, no credentials) exposing AWS docs, API references,
# What's New, Getting Started, Builder Center, blogs, architectural references,
# and Well-Architected guidance — ideal for Citizen Developers/Builders.
#   https://github.com/awslabs/mcp/tree/main/src/aws-knowledge-mcp-server
#
# Usage:  ./ai_mcp_servers_gitops.sh <coder_session_token>
#
# Environment (set by the CloudFormation buildspec):
#   CODER_AGENT_URL          - Coder deployment URL                    (required)
#   MCP_KNOWLEDGE_SLUG       - server slug        (default: aws-knowledge)
#   MCP_KNOWLEDGE_URL        - server endpoint    (default: https://knowledge-mcp.global.api.aws)
#   MCP_KNOWLEDGE_AVAIL      - availability       (default: default_on; force_on|default_on|default_off)
#
# SAFE BY DESIGN: this script is best-effort and ALWAYS exits 0. It no-ops on
# Coder versions without the MCP-servers API (HTTP 404) and never fails a deploy.
set -uo pipefail

CODER_TOKEN="${1:-}"
BASE="${CODER_AGENT_URL:-}"
SLUG="${MCP_KNOWLEDGE_SLUG:-aws-knowledge}"
URL="${MCP_KNOWLEDGE_URL:-https://knowledge-mcp.global.api.aws}"
AVAIL="${MCP_KNOWLEDGE_AVAIL:-default_on}"

warn() { echo "ai_mcp_servers_gitops: $*" >&2; }

# Trim trailing slash from base URL.
BASE="${BASE%/}"
if [ -z "$CODER_TOKEN" ] || [ -z "$BASE" ]; then
  warn "missing CODER_AGENT_URL or session token; skipping (non-fatal)."
  exit 0
fi

H_TOKEN="Coder-Session-Token: ${CODER_TOKEN}"
api() { # method path [json-body]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -m 30 -o /tmp/mcp_resp.json -w "%{http_code}" \
      -X "$method" -H "$H_TOKEN" -H "Content-Type: application/json" \
      --data "$body" "${BASE}${path}" 2>/dev/null || echo "000"
  else
    curl -sS -m 30 -o /tmp/mcp_resp.json -w "%{http_code}" \
      -X "$method" -H "$H_TOKEN" "${BASE}${path}" 2>/dev/null || echo "000"
  fi
}

# --- Resolve the (default) organization -------------------------------------
code=$(api GET "/api/v2/organizations")
if [ "$code" != "200" ]; then
  warn "could not list organizations (HTTP $code); skipping (non-fatal)."
  exit 0
fi
ORG=$(python3 -c '
import json,sys
try:
    orgs=json.load(open("/tmp/mcp_resp.json"))
except Exception:
    sys.exit(0)
if isinstance(orgs,dict): orgs=orgs.get("organizations",[])
d=[o for o in orgs if o.get("is_default")]
print((d[0] if d else (orgs[0] if orgs else {})).get("id",""))
' 2>/dev/null)
if [ -z "$ORG" ]; then
  warn "could not resolve an organization id; skipping (non-fatal)."
  exit 0
fi

# --- Feature/idempotency guard ----------------------------------------------
code=$(api GET "/api/v2/organizations/${ORG}/mcp-servers")
if [ "$code" = "404" ]; then
  warn "Coder Agents MCP-servers API not available on this deployment (HTTP 404) — needs Coder 2.36+ with the AI Governance Add-On. Skipping (non-fatal)."
  exit 0
fi
if [ "$code" != "200" ]; then
  warn "could not list MCP servers (HTTP $code); skipping (non-fatal)."
  exit 0
fi
if python3 -c '
import json,sys
try: data=json.load(open("/tmp/mcp_resp.json"))
except Exception: sys.exit(1)
items=data.get("mcp_servers", data) if isinstance(data,dict) else data
items=items if isinstance(items,list) else []
sys.exit(0 if any(s.get("slug")=="'"$SLUG"'" for s in items) else 1)
' 2>/dev/null; then
  echo "MCP server '${SLUG}' already registered; nothing to do."
  exit 0
fi

# --- Create the AWS Knowledge MCP server ------------------------------------
BODY=$(python3 -c '
import json,os
print(json.dumps({
  "display_name": "AWS Knowledge",
  "slug": os.environ["SLUG"],
  "description": "AWS-hosted MCP server: docs, API refs, Whats New, Builder Center, and Well-Architected guidance.",
  "icon_url": "/icon/aws.png",
  "transport": "streamable_http",
  "url": os.environ["URL"],
  "auth_type": "none",
  "availability": os.environ["AVAIL"],
  "enabled": True,
  "model_intent": True
}))
' SLUG="$SLUG" URL="$URL" AVAIL="$AVAIL")

code=$(api POST "/api/v2/organizations/${ORG}/mcp-servers" "$BODY")
if [ "$code" = "200" ] || [ "$code" = "201" ]; then
  echo "Registered AWS Knowledge MCP server ('${SLUG}' -> ${URL}) for Coder Agents."
else
  warn "create returned HTTP $code: $(head -c 300 /tmp/mcp_resp.json 2>/dev/null)"
  warn "skipping (non-fatal)."
fi
exit 0
