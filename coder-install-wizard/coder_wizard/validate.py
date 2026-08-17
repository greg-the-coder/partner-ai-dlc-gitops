"""
Post-install validation — confirms that Coder is healthy, agents are wired to
Bedrock, workspace templates are reachable, and BOTH workspace compute lanes
(Fargate profile + EC2 Spot NodePool) plus EFS storage are ready.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    name: str
    status: str        # "ok" | "warn" | "fail"
    message: str
    detail: str = ""


@dataclass
class ValidationReport:
    results: list[ValidationResult] = field(default_factory=list)

    def add(self, r: ValidationResult) -> None:
        self.results.append(r)

    @property
    def passed(self) -> bool:
        return all(r.status != "fail" for r in self.results)


def _http_get(url: str, headers: dict | None = None, timeout: int = 15) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except Exception as e:
        return 0, str(e)


def _aws_json(args: list[str]) -> tuple[int, dict | list | None]:
    result = subprocess.run(
        ["aws"] + args + ["--output", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return result.returncode, None
    try:
        return 0, json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return 0, None


# ---------------------------------------------------------------------------
# Coder API validations
# ---------------------------------------------------------------------------

def validate_coder_reachable(coder_url: str, retries: int = 6, delay: int = 20) -> ValidationResult:
    """Confirm the Coder API is reachable and returns build info."""
    status = 0
    for attempt in range(1, retries + 1):
        status, body = _http_get(f"{coder_url.rstrip('/')}/api/v2/buildinfo")
        if status == 200:
            try:
                info = json.loads(body)
                version = info.get("version", "unknown")
                return ValidationResult(
                    name="Coder API Reachable",
                    status="ok",
                    message=f"Coder {version} responding at {coder_url}",
                )
            except json.JSONDecodeError:
                return ValidationResult(
                    name="Coder API Reachable",
                    status="ok",
                    message=f"Coder responding at {coder_url} (version unknown)",
                )
        if attempt < retries:
            time.sleep(delay)

    return ValidationResult(
        name="Coder API Reachable",
        status="fail",
        message=f"Coder API not reachable at {coder_url} after {retries} attempts.",
        detail=f"Last HTTP status: {status}",
    )


def validate_admin_login(coder_url: str, session_token: str) -> ValidationResult:
    """Confirm the admin session token works against the Coder API."""
    status, body = _http_get(
        f"{coder_url.rstrip('/')}/api/v2/users/me",
        headers={"Coder-Session-Token": session_token},
    )
    if status == 200:
        try:
            user = json.loads(body)
            username = user.get("username", "unknown")
            return ValidationResult(
                name="Admin Session Token",
                status="ok",
                message=f"Admin token valid — authenticated as '{username}'",
            )
        except json.JSONDecodeError:
            pass
    return ValidationResult(
        name="Admin Session Token",
        status="fail",
        message="Admin session token did not authenticate successfully.",
        detail=f"HTTP {status}: {body[:200]}",
    )


def validate_license(coder_url: str, session_token: str) -> ValidationResult:
    """Check whether a Coder Premium license is applied (enables HA + premium features)."""
    status, body = _http_get(
        f"{coder_url.rstrip('/')}/api/v2/licenses",
        headers={"Coder-Session-Token": session_token},
    )
    if status != 200:
        return ValidationResult(
            name="Coder Premium License",
            status="warn",
            message="Could not query the licenses endpoint.",
            detail=f"HTTP {status}",
        )
    try:
        licenses = json.loads(body)
    except json.JSONDecodeError:
        licenses = []

    if licenses:
        return ValidationResult(
            name="Coder Premium License",
            status="ok",
            message=f"{len(licenses)} license(s) applied — premium features (HA, etc.) enabled.",
        )
    return ValidationResult(
        name="Coder Premium License",
        status="warn",
        message="No license applied — running Community Edition (single-replica, no premium features).",
        detail="Provide CoderLicenseKey at deploy time or add one under Coder → Admin → Licenses.",
    )


def validate_ai_providers(coder_url: str, session_token: str) -> ValidationResult:
    """Check that at least one AI provider is configured and enabled."""
    status, body = _http_get(
        f"{coder_url.rstrip('/')}/api/v2/ai/providers",
        headers={"Coder-Session-Token": session_token},
    )
    if status != 200:
        return ValidationResult(
            name="Coder AI Providers",
            status="warn",
            message="Could not query AI providers endpoint.",
            detail=f"HTTP {status}",
        )

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ValidationResult(
            name="Coder AI Providers",
            status="warn",
            message="AI providers response was not valid JSON.",
        )

    providers = data if isinstance(data, list) else data.get("providers", [])
    enabled = [p for p in providers if p.get("enabled")]

    if not enabled:
        return ValidationResult(
            name="Coder AI Providers",
            status="fail",
            message="No AI providers are enabled.",
            detail="Expected at least 'bedrock' and 'openai-compat' providers.",
        )

    names = ", ".join(p.get("name", "?") for p in enabled)
    return ValidationResult(
        name="Coder AI Providers",
        status="ok",
        message=f"{len(enabled)} AI provider(s) enabled: {names}",
    )


def validate_workspace_templates(coder_url: str, session_token: str) -> ValidationResult:
    """Check that workspace templates exist and are not deprecated."""
    status, body = _http_get(
        f"{coder_url.rstrip('/')}/api/v2/templates",
        headers={"Coder-Session-Token": session_token},
    )
    if status != 200:
        return ValidationResult(
            name="Workspace Templates",
            status="warn",
            message="Could not query workspace templates.",
            detail=f"HTTP {status}",
        )

    try:
        templates = json.loads(body)
    except json.JSONDecodeError:
        return ValidationResult(
            name="Workspace Templates",
            status="warn",
            message="Templates response was not valid JSON.",
        )

    active = [t for t in templates if not t.get("deprecated", False)]
    if not active:
        return ValidationResult(
            name="Workspace Templates",
            status="fail",
            message="No active workspace templates found.",
            detail="The GitOps template deployment may not have completed successfully.",
        )

    names = ", ".join(t.get("name", "?") for t in active)
    return ValidationResult(
        name="Workspace Templates",
        status="ok",
        message=f"{len(active)} workspace template(s) deployed: {names}",
    )


# ---------------------------------------------------------------------------
# AWS / EKS infrastructure validations
# ---------------------------------------------------------------------------

def validate_fargate_profile(eks_cluster_name: str, region: str) -> ValidationResult:
    """Confirm the Fargate profile (lane 1) is ACTIVE."""
    code, data = _aws_json([
        "eks", "describe-fargate-profile",
        "--cluster-name", eks_cluster_name,
        "--fargate-profile-name", "coder-workspaces",
        "--region", region,
    ])
    if code != 0 or data is None:
        return ValidationResult(
            name="Fargate Lane (profile)",
            status="fail",
            message="Fargate profile 'coder-workspaces' not found.",
            detail="Fargate-lane (compute=fargate) workspaces cannot schedule without it.",
        )

    profile_status = data.get("fargateProfile", {}).get("status", "UNKNOWN")
    if profile_status == "ACTIVE":
        return ValidationResult(
            name="Fargate Lane (profile)",
            status="ok",
            message="Fargate profile 'coder-workspaces' is ACTIVE.",
        )
    return ValidationResult(
        name="Fargate Lane (profile)",
        status="warn",
        message=f"Fargate profile status: {profile_status} (expected ACTIVE).",
    )


def validate_efs_csi_addon(eks_cluster_name: str, region: str) -> ValidationResult:
    """Confirm the EFS CSI driver addon is installed (required for EFS home dirs on BOTH lanes)."""
    code, data = _aws_json([
        "eks", "describe-addon",
        "--cluster-name", eks_cluster_name,
        "--addon-name", "aws-efs-csi-driver",
        "--region", region,
    ])
    if code != 0 or data is None:
        return ValidationResult(
            name="EFS CSI Driver",
            status="fail",
            message="EFS CSI driver addon (aws-efs-csi-driver) is not installed.",
            detail="EFS-backed /home/coder will not mount without it.",
        )
    addon_status = data.get("addon", {}).get("status", "UNKNOWN")
    if addon_status == "ACTIVE":
        return ValidationResult(
            name="EFS CSI Driver",
            status="ok",
            message="aws-efs-csi-driver addon is ACTIVE.",
        )
    return ValidationResult(
        name="EFS CSI Driver",
        status="warn",
        message=f"aws-efs-csi-driver addon status: {addon_status} (expected ACTIVE).",
    )


def validate_spot_nodepool(eks_cluster_name: str, region: str) -> ValidationResult:
    """
    Confirm the EC2 Spot workspace lane (Auto Mode NodePool 'coder-ws-spot') exists.

    NodePools are Karpenter CRDs (no AWS API), so this uses kubectl. If kubectl or a
    kubeconfig is unavailable it degrades to a 'warn' with remediation rather than failing.
    """
    if shutil.which("kubectl") is None:
        return ValidationResult(
            name="Spot Lane (NodePool)",
            status="warn",
            message="kubectl not found — skipped Spot NodePool check.",
            detail=(
                "Verify manually:\n"
                f"  aws eks update-kubeconfig --name {eks_cluster_name} --region {region}\n"
                "  kubectl get nodepool coder-ws-spot"
            ),
        )

    result = subprocess.run(
        ["kubectl", "get", "nodepool", "coder-ws-spot", "-o", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ValidationResult(
            name="Spot Lane (NodePool)",
            status="warn",
            message="Could not read NodePool 'coder-ws-spot' via kubectl.",
            detail=(
                "kubeconfig may not target this cluster, or the NodePool was not applied.\n"
                f"  aws eks update-kubeconfig --name {eks_cluster_name} --region {region}\n"
                "  kubectl apply -f infrastructure/k8s/spot-nodepool.yaml"
            ),
        )
    return ValidationResult(
        name="Spot Lane (NodePool)",
        status="ok",
        message="EKS Auto Mode NodePool 'coder-ws-spot' is present (Spot lane ready).",
    )


def validate_efs(efs_id: str, region: str) -> ValidationResult:
    """Confirm the EFS file system is available."""
    code, data = _aws_json([
        "efs", "describe-file-systems",
        "--file-system-id", efs_id,
        "--region", region,
    ])
    if code != 0 or data is None:
        return ValidationResult(
            name="EFS File System",
            status="fail",
            message=f"EFS file system {efs_id} not found.",
        )

    fs_list = data.get("FileSystems", [])
    if not fs_list:
        return ValidationResult(
            name="EFS File System",
            status="fail",
            message=f"EFS file system {efs_id} not found.",
        )

    fs_state = fs_list[0].get("LifeCycleState", "UNKNOWN")
    if fs_state == "available":
        return ValidationResult(
            name="EFS File System",
            status="ok",
            message=f"EFS {efs_id} is available (persistent workspace homes ready).",
        )
    return ValidationResult(
        name="EFS File System",
        status="warn",
        message=f"EFS {efs_id} state: {fs_state} (expected 'available').",
    )


# ---------------------------------------------------------------------------
# Run all validations
# ---------------------------------------------------------------------------

def run_all(
    coder_url: str,
    session_token: str,
    eks_cluster_name: str,
    efs_id: str,
    region: str,
) -> ValidationReport:
    report = ValidationReport()

    report.add(validate_coder_reachable(coder_url))
    report.add(validate_admin_login(coder_url, session_token))
    report.add(validate_license(coder_url, session_token))
    report.add(validate_ai_providers(coder_url, session_token))
    report.add(validate_workspace_templates(coder_url, session_token))
    # Compute lanes + storage
    report.add(validate_fargate_profile(eks_cluster_name, region))
    report.add(validate_spot_nodepool(eks_cluster_name, region))
    report.add(validate_efs_csi_addon(eks_cluster_name, region))
    report.add(validate_efs(efs_id, region))

    return report
