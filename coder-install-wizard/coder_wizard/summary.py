"""
Install summary — writes install-summary.json and prints a human-readable
summary to stdout at the end of a successful installation.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class InstallSummary:
    installed_at: str = ""
    region: str = ""
    eks_cluster_name: str = ""
    coder_version: str = ""

    # URLs and access
    coder_url: str = ""
    cloudfront_distribution_id: str = ""

    # Secrets
    admin_password_secret_arn: str = ""
    admin_session_token_secret_arn: str = ""
    bedrock_openai_api_key_secret_arn: str = ""

    # Infrastructure IDs
    efs_file_system_id: str = ""
    fargate_subnet_1: str = ""
    fargate_subnet_2: str = ""
    fargate_pod_execution_role_arn: str = ""

    # Compute lanes
    compute_lanes: list = field(default_factory=lambda: ["fargate", "spot"])

    # Image URIs
    claude_code_image_uri: str = ""
    kiro_cli_image_uri: str = ""
    challenge_image_uri: str = ""

    # License / premium
    license_provided: bool = False

    # Validation
    validation_passed: bool = False
    validation_results: list = field(default_factory=list)

    # Next steps
    next_steps: list = field(default_factory=list)


def build_summary(
    cfn_outputs: dict,
    region: str,
    eks_cluster_name: str,
    validation_passed: bool,
    validation_results: list,
    coder_version: str = "",
    license_provided: bool = False,
) -> InstallSummary:
    """Construct a summary from CloudFormation stack outputs."""

    def o(key: str) -> str:
        return cfn_outputs.get(key, "")

    summary = InstallSummary(
        installed_at=datetime.now(tz=timezone.utc).isoformat(),
        region=region,
        eks_cluster_name=eks_cluster_name,
        coder_version=coder_version,
        coder_url=o("CoderURL"),
        cloudfront_distribution_id=o("CloudFrontDistributionId"),
        admin_password_secret_arn=o("CoderAdminPasswordSecretArn"),
        admin_session_token_secret_arn=o("CoderSessionTokenSecretArn"),
        bedrock_openai_api_key_secret_arn=o("BedrockOpenAIApiKeySecretArn"),
        efs_file_system_id=o("EfsFileSystemId"),
        fargate_subnet_1=o("FargateSubnet1Id"),
        fargate_subnet_2=o("FargateSubnet2Id"),
        fargate_pod_execution_role_arn=o("FargatePodExecutionRoleArn"),
        claude_code_image_uri=o("ClaudeCodeImageUri"),
        kiro_cli_image_uri=o("KiroCliImageUri"),
        challenge_image_uri=o("ChallengeImageUri"),
        license_provided=license_provided,
        validation_passed=validation_passed,
        validation_results=[
            {"name": r.name, "status": r.status, "message": r.message}
            for r in validation_results
        ],
        next_steps=[
            f"Open Coder: {o('CoderURL')}",
            f"Retrieve your admin password:  aws secretsmanager get-secret-value --secret-id {o('CoderAdminPasswordSecretArn')} --query SecretString --output text",
            "Create your first workspace from the Coder dashboard (try 'AWS Workshop — Kubernetes with Kiro CLI').",
            "Pick a Compute Lane per workspace: 'fargate' (default, serverless/isolated) or 'spot' (EC2 Spot, auto-scaled, low cost). Home dir is on EFS in both.",
            "Review the Bedrock AI provider config at:  Coder → Admin → AI.",
            (
                "Premium is active (HA + premium features enabled)."
                if license_provided else
                "Running Community Edition — add a Coder Premium license under Coder → Admin → Licenses to enable HA and premium features."
            ),
        ],
    )
    return summary


def write_summary(summary: InstallSummary, output_path: str = "install-summary.json") -> Path:
    path = Path(output_path)
    path.write_text(json.dumps(asdict(summary), indent=2))
    return path


def print_summary(summary: InstallSummary) -> None:
    status_icon = "✅" if summary.validation_passed else "⚠️"

    print()
    print("=" * 70)
    print(f"  {status_icon}  Partner Demo — Coder Installation Summary")
    print("=" * 70)
    print(f"  Installed at : {summary.installed_at}")
    print(f"  Region       : {summary.region}")
    print(f"  EKS Cluster  : {summary.eks_cluster_name}")
    if summary.coder_version:
        print(f"  Coder version: {summary.coder_version}")
    print(f"  Compute lanes: {', '.join(summary.compute_lanes)}  (EFS-backed /home/coder)")
    print(f"  Premium      : {'yes (license applied)' if summary.license_provided else 'no (Community Edition)'}")
    print()
    print("  ACCESS")
    print(f"  Coder URL    : {summary.coder_url}")
    print()
    print("  SECRETS (retrieve via AWS CLI or Console → Secrets Manager)")
    print(f"  Admin password     : {summary.admin_password_secret_arn}")
    print(f"  Session token      : {summary.admin_session_token_secret_arn}")
    print(f"  Bedrock API key    : {summary.bedrock_openai_api_key_secret_arn}")
    print()
    print("  NEXT STEPS")
    for i, step in enumerate(summary.next_steps, 1):
        print(f"  {i}. {step}")
    print()

    if summary.validation_results:
        print("  POST-INSTALL VALIDATION")
        for r in summary.validation_results:
            icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}.get(r["status"], "?")
            print(f"    {icon} {r['name']}: {r['message']}")

    print()
    print("  Full details saved to: install-summary.json")
    print("=" * 70)
