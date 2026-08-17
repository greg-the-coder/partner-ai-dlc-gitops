#!/usr/bin/env python3
"""
partner-coder-wizard — GenAI-assisted install of the Partner AI-DLC Coder demo on AWS.

Deploys Coder 2.36.0 on EKS Auto Mode with two workspace compute lanes
(AWS Fargate + EC2 Spot NodePool) and EFS-backed home directories, from the
greg-the-coder/partner-ai-dlc-gitops CloudFormation stacks.

Usage
-----
  python -m coder_wizard          # interactive wizard (recommended)
  python -m coder_wizard preflight --region us-east-1 --cluster coder-aws-cluster
  python -m coder_wizard cost --developers 25 --spot-fraction 0.4
  python -m coder_wizard deploy   --region us-east-1 --cluster coder-aws-cluster \\
                                  --admin-email admin@example.com --admin-user admin --yes
  python -m coder_wizard validate --coder-url https://xxx.cloudfront.net \\
                                  --cluster coder-aws-cluster --efs-id fs-xxxx
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from coder_wizard import preflight, deploy, validate, cost_estimate, summary, dryrun


# ---------------------------------------------------------------------------
# Defaults specific to the Partner AI-DLC demo deployment
# ---------------------------------------------------------------------------

DEFAULT_GIT_REPO   = "https://github.com/greg-the-coder/partner-ai-dlc-gitops.git"
DEFAULT_CLUSTER    = "coder-aws-cluster"
DEFAULT_K8S        = "1.35"
DEFAULT_CODER_VER  = "2.36.0"


# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
DIM    = "\033[2m"


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def ok(msg: str)   -> str: return _color(f"  ✅  {msg}", GREEN)
def warn(msg: str) -> str: return _color(f"  ⚠️   {msg}", YELLOW)
def fail(msg: str) -> str: return _color(f"  ❌  {msg}", RED)
def info(msg: str) -> str: return _color(f"  ℹ️   {msg}", CYAN)
def bold(msg: str) -> str: return _color(msg, BOLD)
def dim(msg: str)  -> str: return _color(msg, DIM)


def _status_line(result) -> str:
    icon_map = {"ok": ok, "warn": warn, "fail": fail}
    fn = icon_map.get(result.status, info)
    line = fn(f"{result.name}: {result.message}")
    if result.detail:
        line += f"\n        {dim(result.detail)}"
    if getattr(result, "fix", "") and result.status != "ok":
        fix_lines = result.fix.replace("\n", "\n        ")
        line += f"\n        {_color('Fix: ' + fix_lines, YELLOW)}"
    return line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_current_region() -> str:
    result = subprocess.run(
        ["aws", "configure", "get", "region"],
        capture_output=True, text=True,
    )
    region = result.stdout.strip()
    if region:
        return region
    return os.environ.get("AWS_DEFAULT_REGION", "us-east-1")


def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!#$%^&*"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!#$%^&*" for c in pwd)
        ):
            return pwd


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    display_default = f" [{default}]" if default and not secret else (" [auto-generated]" if secret and default else "")
    prompt_str = f"  {bold(label)}{display_default}: "
    try:
        if secret:
            import getpass
            value = getpass.getpass(prompt_str)
        else:
            value = input(prompt_str)
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return value.strip() or default


def _confirm(question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = _prompt(f"{question} {hint}").lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _section(title: str) -> None:
    print()
    print(bold(f"{'─' * 60}"))
    print(bold(f"  {title}"))
    print(bold(f"{'─' * 60}"))


def _aws_current_account() -> str:
    result = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--output", "json"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        try:
            return json.loads(result.stdout).get("Account", "unknown")
        except Exception:
            pass
    return "unknown"


def _get_cfn_output(stack_name: str, key: str, region: str) -> str:
    result = subprocess.run(
        ["aws", "cloudformation", "describe-stacks",
         "--stack-name", stack_name, "--region", region, "--output", "json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout)
        for o in data["Stacks"][0].get("Outputs", []):
            if o["OutputKey"] == key:
                return o["OutputValue"]
    except Exception:
        pass
    return ""


def _get_secret_value(secret_arn: str, region: str) -> str:
    result = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value",
         "--secret-id", secret_arn,
         "--region", region,
         "--query", "SecretString",
         "--output", "text"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _parse_spot_fraction(raw: str) -> float:
    """Accept '40', '40%', or '0.4' and return a 0.0–1.0 fraction."""
    raw = (raw or "").strip().rstrip("%").strip()
    if not raw:
        return 0.0
    try:
        val = float(raw)
    except ValueError:
        return 0.0
    if val > 1.0:
        val = val / 100.0
    return max(0.0, min(1.0, val))


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return Path(os.path.expanduser("~/.coder-wizard/last-deploy.json"))


def _save_deploy_state(args: argparse.Namespace) -> None:
    """Persist the last deployment's non-secret parameters for --retry / resume.

    Secrets (admin password, license key) are intentionally NOT stored; on retry
    the CFN RetryFlag=True path skips first-user creation and license add.
    """
    data = {
        "saved_at":      datetime.now(tz=timezone.utc).isoformat(),
        "region":        args.region,
        "cluster":       args.cluster,
        "developers":    getattr(args, "developers", 10),
        "spot_fraction": str(getattr(args, "spot_fraction", "0")),
        "git_repo_url":  args.git_repo_url,
        "git_branch":    args.git_branch,
        "admin_email":   args.admin_email,
        "admin_user":    args.admin_user,
        "admin_name":    args.admin_name,
        "k8s_version":   args.k8s_version,
        "coder_version": args.coder_version,
        "pipeline_template": args.pipeline_template,
        "core_template":     args.core_template,
        "license_provided":  bool(getattr(args, "license_key", "")),
    }
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))
    except OSError:
        pass  # best-effort; state persistence is a convenience, not required


def _load_deploy_state() -> dict | None:
    p = _state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _apply_state_to_args(args: argparse.Namespace, state: dict) -> None:
    """Overlay saved (non-secret) parameters onto args for a retry/resume."""
    for key in (
        "region", "cluster", "developers", "spot_fraction", "git_repo_url",
        "git_branch", "admin_email", "admin_user", "admin_name",
        "k8s_version", "coder_version", "pipeline_template", "core_template",
    ):
        if key in state and state[key] is not None:
            setattr(args, key, state[key])
    # spot_fraction is stored as a string; keep it stringy for _parse_spot_fraction
    if "spot_fraction" in state:
        args.spot_fraction = str(state["spot_fraction"])


def _retry_args() -> argparse.Namespace:
    """Minimal args for a retry/resume; cmd_deploy overlays the rest from saved state."""
    ra = argparse.Namespace()
    ra.retry_flag = True
    ra.license_key = ""
    ra.admin_password = ""
    ra.yes = True
    ra.dry_run = False
    ra.dry_run_output = None
    return ra


def _prepare_stack_for_create(stack_name: str, region: str, label: str,
                              on_event) -> str:
    """Assess an existing CloudFormation stack before (re)creating it.

    Returns one of: 'create' (proceed to create-stack), 'skip' (already healthy),
    or 'abort' (in progress, unexpected, or delete failed).
    """
    status = deploy.get_stack_status(stack_name, region)
    if status is None:
        return "create"
    if status in deploy.SUCCESS_STATUSES:
        print(ok(f"{label} stack already exists and is healthy ({status})."))
        return "skip"
    if deploy.is_in_progress(status):
        print(fail(f"{label} stack is currently {status}."))
        print(info("Wait for the in-progress operation to finish, then re-run."))
        return "abort"
    if status in deploy.RECREATE_STATUSES:
        print(warn(f"{label} stack exists in a failed state ({status}). Deleting and recreating..."))
        res = deploy.delete_stack(stack_name, region, on_event=on_event)
        if not res.success:
            print()
            print(fail(f"Could not delete the failed {label} stack: {res.error}"))
            print(info("Delete it manually in the CloudFormation console, then re-run."))
            return "abort"
        print(ok(f"Failed {label} stack deleted; recreating."))
        return "create"
    print(fail(f"{label} stack is in an unexpected state '{status}'."))
    print(info("Resolve it in the CloudFormation console, then re-run."))
    return "abort"


def cmd_preflight(args: argparse.Namespace) -> int:
    _section("Pre-flight Checks")
    print(info(f"Region: {args.region}  |  EKS cluster name: {args.cluster}"))
    print()

    report = preflight.run_all(
        region=args.region,
        eks_cluster_name=args.cluster,
        skip_ecr=args.skip_ecr,
    )

    for r in report.results:
        print(_status_line(r))

    print()
    if report.passed:
        print(ok("All checks passed — ready to deploy."))
        return 0
    print(fail(f"{len(report.failures)} check(s) failed. Resolve them before deploying."))
    return 1


def cmd_cost(args: argparse.Namespace) -> int:
    _section("Cost Estimate")
    spot_fraction = _parse_spot_fraction(getattr(args, "spot_fraction", "0"))
    estimate = cost_estimate.estimate(
        developer_count=args.developers,
        avg_workspace_hours_per_day=args.hours_per_day,
        region=args.region,
        spot_fraction=spot_fraction,
    )
    print(info(f"Team size: {args.developers} developers  |  Spot lane share: {spot_fraction*100:.0f}%"))
    print()
    print(estimate.format_table())
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    _section("Post-Install Validation")
    print(info(f"Coder URL: {args.coder_url}"))
    print()

    token = args.token
    if not token:
        print(info("No --token provided; attempting to retrieve from Secrets Manager..."))
        token_arn = _get_cfn_output(args.stack_name, "CoderSessionTokenSecretArn", args.region)
        if token_arn:
            token = _get_secret_value(token_arn, args.region)
        if not token:
            print(fail("Could not obtain a session token. Provide --token or --stack-name with valid AWS credentials."))
            return 1

    report = validate.run_all(
        coder_url=args.coder_url,
        session_token=token,
        eks_cluster_name=args.cluster,
        efs_id=args.efs_id,
        region=args.region,
    )

    for r in report.results:
        print(_status_line(r))

    print()
    if report.passed:
        print(ok("All validations passed — Coder is production-ready."))
        return 0
    failures = [r for r in report.results if r.status == "fail"]
    print(fail(f"{len(failures)} validation(s) failed."))
    return 1


def _build_params(args: argparse.Namespace, admin_password: str) -> tuple[dict, dict]:
    """Return (pipeline_params, core_params) dicts from parsed args."""
    pipeline_params = {
        "EKSClusterName": args.cluster,
        "GitRepoURL":     args.git_repo_url,
        "GitBranch":      args.git_branch,
    }
    core_params: dict = {
        "EKSClusterName":             args.cluster,
        "KubernetesVersion":          args.k8s_version,
        "CoderVersion":               args.coder_version,
        "CoderAdminEmail":            args.admin_email,
        "CoderAdminUser":             args.admin_user,
        "CoderAdminName":             args.admin_name,
        "CoderAdminPassword":         admin_password,
        "CoderGitOpsTemplateRepoURL": args.git_repo_url,
        "RetryFlag":                  "True" if getattr(args, "retry_flag", False) else "False",
    }
    if getattr(args, "license_key", ""):
        core_params["CoderLicenseKey"] = args.license_key
    return pipeline_params, core_params


def cmd_deploy(args: argparse.Namespace) -> int:
    """Full orchestrated deploy: preflight → cost → pipeline stack → core stack → validate."""

    retrying = getattr(args, "retry_flag", False)
    if retrying:
        state = _load_deploy_state()
        if not state:
            print(fail(f"No saved deployment state found at {_state_path()}."))
            print(info("Run a normal deploy first, or drop --retry and pass parameters explicitly."))
            return 1
        _apply_state_to_args(args, state)
        print(info(f"Resuming last deployment (saved {state.get('saved_at', '?')}) with RetryFlag=True."))

    if not retrying and not getattr(args, "admin_email", ""):
        print(fail("--admin-email is required (or use --retry to resume the last deployment)."))
        return 1

    spot_fraction = _parse_spot_fraction(getattr(args, "spot_fraction", "0"))

    print()
    print(bold("╔══════════════════════════════════════════════════════════════╗"))
    print(bold("║     Partner Demo — Coder Install Wizard  v0.2                ║"))
    if getattr(args, "dry_run", False):
        print(bold("║     *** DRY RUN — no AWS resources will be created ***       ║"))
    print(bold("╚══════════════════════════════════════════════════════════════╝"))
    print()
    if not getattr(args, "dry_run", False):
        print(info(f"Account : {_aws_current_account()}"))
    print(info(f"Region  : {args.region}"))
    print(info(f"Cluster : {args.cluster}"))
    print(info(f"Coder   : {args.coder_version}  |  Spot lane share: {spot_fraction*100:.0f}%"))
    if retrying:
        print(info("Mode    : RETRY — reusing saved parameters; assessing stacks to resume."))

    # ── 1. Pre-flight ──────────────────────────────────────────────────────
    _section("Step 1 — Pre-flight Checks")
    report = preflight.run_all(
        region=args.region,
        eks_cluster_name=args.cluster,
        skip_ecr=True,   # images not built yet; pipeline runs first
    )
    for r in report.results:
        print(_status_line(r))

    if not report.passed:
        print()
        print(fail("Pre-flight checks failed. Fix the issues above and re-run."))
        return 1

    print()
    print(ok("Pre-flight passed."))

    # ── 2. Cost estimate ───────────────────────────────────────────────────
    _section("Step 2 — Cost Estimate")
    est = cost_estimate.estimate(
        developer_count=args.developers, region=args.region, spot_fraction=spot_fraction,
    )
    print(est.format_table())

    # ── 3a. DRY RUN: generate files and exit ──────────────────────────────
    if getattr(args, "dry_run", False):
        _section("Step 3 — Generating Parameter Files (Dry Run)")

        admin_password = getattr(args, "admin_password", "") or _generate_password()
        pipeline_params, core_params = _build_params(args, admin_password)

        pipeline_stack = f"{args.cluster}-image-pipeline"
        core_stack     = f"{args.cluster}-coder"
        output_dir = getattr(args, "dry_run_output", None) or f"coder-deploy-{args.cluster}"

        result = dryrun.generate(
            output_dir=output_dir,
            region=args.region,
            cluster=args.cluster,
            pipeline_template=args.pipeline_template,
            core_template=args.core_template,
            pipeline_params=pipeline_params,
            core_params=core_params,
            pipeline_stack_name=pipeline_stack,
            core_stack_name=core_stack,
            estimated_monthly_cost=est.total,
            developer_count=args.developers,
            spot_fraction=spot_fraction,
        )

        print()
        print(ok(f"Dry run complete. Files written to: {result.output_dir.resolve()}"))
        print()
        print(f"  {bold('Parameter files:')}")
        print(f"    {result.pipeline_params_path.resolve()}")
        print(f"    {result.core_params_path.resolve()}")
        print()
        print(f"  {bold('Deploy script:')}")
        print(f"    {result.deploy_script_path.resolve()}")
        print()
        print(f"  {bold('Summary:')}")
        print(f"    {result.summary_path.resolve()}")
        print()
        print(info("When ready to deploy for real, run:"))
        print(f"    bash {result.deploy_script_path.resolve()}")
        return 0

    # ── 3b. LIVE DEPLOY ───────────────────────────────────────────────────
    if not args.yes and not _confirm("Proceed with this estimated spend?"):
        print(info("Aborted. Use --dry-run to generate parameter files without deploying."))
        return 0

    pipeline_stack = f"{args.cluster}-image-pipeline"
    _section(f"Step 3 — Image Pipeline Stack ({pipeline_stack})")

    admin_password = getattr(args, "admin_password", "") or _generate_password()
    if not getattr(args, "admin_password", ""):
        print(info("Admin password auto-generated and will be stored in Secrets Manager."))

    pipeline_params, core_params = _build_params(args, admin_password)

    # Persist non-secret params so a later `deploy --retry` (or wizard resume) can
    # reuse them and pick up where this attempt left off.
    _save_deploy_state(args)

    def _on_pipeline_event(evt: deploy.StackEvent) -> None:
        icon = "🔄" if "IN_PROGRESS" in evt.status else ("✅" if "COMPLETE" in evt.status else "❌")
        print(f"  {icon}  {evt.timestamp[11:19]}  {evt.logical_id:<35} {evt.status}")
        if evt.reason:
            print(dim(f"            {evt.reason}"))

    # ── Pre-check: is the image pipeline stack already present? ────────────
    #   healthy (CREATE/UPDATE_COMPLETE) -> skip the build
    #   in progress                     -> ask the user to wait and re-run
    #   failed/unusable                  -> delete, then recreate
    skip_pipeline = False
    pstatus = deploy.get_stack_status(pipeline_stack, args.region)
    if pstatus in deploy.SUCCESS_STATUSES:
        print(ok(f"Image pipeline stack already exists and is healthy ({pstatus})."))
        print(info("Skipping image build. (To force a rebuild, delete the stack or re-run "
                   f"the CodeBuild project '{args.cluster}-workspace-image-build'.)"))
        skip_pipeline = True
    elif deploy.is_in_progress(pstatus):
        print(fail(f"Image pipeline stack is currently {pstatus}."))
        print(info("Wait for the in-progress operation to finish, then re-run the wizard."))
        return 1
    elif pstatus in deploy.RECREATE_STATUSES:
        print(warn(f"Image pipeline stack exists in a failed state ({pstatus}). "
                   "Deleting it and recreating..."))
        del_result = deploy.delete_stack(pipeline_stack, args.region, on_event=_on_pipeline_event)
        if not del_result.success:
            print()
            print(fail(f"Could not delete the failed pipeline stack: {del_result.error}"))
            print(info("Delete it manually in the CloudFormation console, then re-run."))
            return 1
        print(ok("Failed pipeline stack deleted; recreating."))
    elif pstatus is not None:
        print(fail(f"Image pipeline stack is in an unexpected state '{pstatus}'."))
        print(info("Resolve it in the CloudFormation console, then re-run the wizard."))
        return 1

    if not skip_pipeline:
        print(info("Building and pushing the workspace images to ECR (~10–20 min)."))
        print()

        pipeline_result = deploy.deploy_stack(
            stack_name=pipeline_stack,
            template_path=args.pipeline_template,
            parameters=pipeline_params,
            region=args.region,
            on_event=_on_pipeline_event,
        )

        if not pipeline_result.success:
            print()
            print(fail(f"Image pipeline stack failed: {pipeline_result.error}"))
            print(info(f"Check CodeBuild logs: /aws/codebuild/{args.cluster}-workspace-image-build"))
            return 1

        print()
        print(ok("Image pipeline stack complete. Waiting for CodeBuild to finish building images..."))

        codebuild_project = f"{args.cluster}-workspace-image-build"
        built = deploy.wait_for_codebuild(codebuild_project, args.region, timeout=2400)
        if not built:
            print(fail("CodeBuild workspace image build did not complete successfully."))
            print(info(f"Check logs: aws logs tail /aws/codebuild/CodeBuild-{pipeline_stack}"))
            return 1

        print(ok("Workspace images built and pushed to ECR."))

    # ── 4. Core Coder stack ────────────────────────────────────────────────
    core_stack = f"{args.cluster}-coder"
    _section(f"Step 4 — Core Coder Stack ({core_stack})")

    def _on_core_event(evt: deploy.StackEvent) -> None:
        icon = "🔄" if "IN_PROGRESS" in evt.status else ("✅" if "COMPLETE" in evt.status else "❌")
        print(f"  {icon}  {evt.timestamp[11:19]}  {evt.logical_id:<35} {evt.status}")
        if evt.reason and "reason" not in evt.reason.lower():
            print(dim(f"            {evt.reason}"))

    # Assess the existing core stack: skip if healthy, delete+recreate if failed,
    # abort if mid-operation. On --retry this recreates with RetryFlag=True, and the
    # buildspec's idempotency checks reuse the existing EKS cluster / CloudFront.
    core_action = _prepare_stack_for_create(core_stack, args.region, "Core Coder", _on_core_event)
    if core_action == "abort":
        return 1

    if core_action == "skip":
        print(info("Core stack already deployed; validating the existing deployment."))
        core_result = deploy.DeployResult(
            stack_name=core_stack, success=True,
            outputs=deploy.get_stack_outputs(core_stack, args.region),
        )
    else:
        print(info("This provisions EKS (Auto Mode) + Fargate profile + EC2 Spot NodePool, "
                   "Aurora, EFS, CloudFront, installs Coder, and applies the license (~35–45 min)."))
        print()
        core_result = deploy.deploy_stack(
            stack_name=core_stack,
            template_path=args.core_template,
            parameters=core_params,
            region=args.region,
            on_event=_on_core_event,
        )
        if not core_result.success:
            print()
            print(fail(f"Core stack failed: {core_result.error}"))
            print(info(f"Check CodeBuild logs: aws logs tail /aws/codebuild/CodeBuild-{core_stack} --follow"))
            return 1
        print()
        print(ok("Core Coder stack deployed successfully."))

    # ── 5. Post-install validation ─────────────────────────────────────────
    _section("Step 5 — Post-Install Validation")

    coder_url = core_result.outputs.get("CoderURL", "")
    efs_id    = core_result.outputs.get("EfsFileSystemId", "")
    token_arn = core_result.outputs.get("CoderSessionTokenSecretArn", "")

    print(info(f"Coder URL: {coder_url}"))
    print()

    session_token = _get_secret_value(token_arn, args.region) if token_arn else ""

    val_report = validate.run_all(
        coder_url=coder_url,
        session_token=session_token,
        eks_cluster_name=args.cluster,
        efs_id=efs_id,
        region=args.region,
    )

    for r in val_report.results:
        print(_status_line(r))

    # ── Summary ────────────────────────────────────────────────────────────
    install_summary = summary.build_summary(
        cfn_outputs=core_result.outputs,
        region=args.region,
        eks_cluster_name=args.cluster,
        validation_passed=val_report.passed,
        validation_results=val_report.results,
        coder_version=args.coder_version,
        license_provided=bool(getattr(args, "license_key", "")),
    )
    summary_path = summary.write_summary(install_summary)
    summary.print_summary(install_summary)

    print(info(f"Summary written to: {summary_path.resolve()}"))
    return 0


# ---------------------------------------------------------------------------
# Interactive wizard mode
# ---------------------------------------------------------------------------

def cmd_wizard(_args: argparse.Namespace) -> int:
    """Conversational setup: asks questions, runs preflight, cost estimate, then deploys."""

    if getattr(_args, "retry_flag", False):
        return cmd_deploy(_retry_args())

    print()
    print(bold("╔══════════════════════════════════════════════════════════════╗"))
    print(bold("║     Partner Demo — Coder Install Wizard  v0.2                ║"))
    print(bold("║     Fargate + EC2 Spot workspaces · EFS · Bedrock agents      ║"))
    print(bold("╚══════════════════════════════════════════════════════════════╝"))
    print()
    print("  This wizard guides you through a Blue/Green install of the Partner")
    print("  AI-DLC demo: Coder on EKS Auto Mode with two workspace compute lanes")
    print("  (AWS Fargate + EC2 Spot) and EFS-backed home directories.")
    print()
    print(dim("  It asks a few questions, runs pre-flight checks, shows a cost"))
    print(dim("  estimate, then deploys both CloudFormation stacks in the correct order."))
    print()

    # ── Gather config ──────────────────────────────────────────────────────
    _saved = _load_deploy_state()
    if _saved and _confirm(
        f"Resume the last deployment (cluster '{_saved.get('cluster')}', "
        f"region '{_saved.get('region')}', saved {_saved.get('saved_at', '?')})?",
        default=False,
    ):
        print(info("Resuming with saved parameters and RetryFlag=True."))
        return cmd_deploy(_retry_args())

    _section("Configuration")

    region = _prompt("AWS region", default=_get_current_region())
    cluster = _prompt("EKS cluster name (use a NEW name for Blue/Green)", default=DEFAULT_CLUSTER)
    coder_version = _prompt("Coder version", default=DEFAULT_CODER_VER)
    developers = _prompt("How many concurrent developers will use Coder?", default="10")
    try:
        developers = int(developers)
    except ValueError:
        developers = 10

    print()
    print(dim("  Workspaces run across two lanes (EFS home dir in both):"))
    print(dim("    • fargate — serverless, Firecracker isolation (default)"))
    print(dim("    • spot    — EC2 Spot NodePool, auto-scaled, low cost"))
    spot_fraction = _parse_spot_fraction(
        _prompt("Approx. % of workspaces on the EC2 Spot lane (for cost estimate)", default="0")
    )

    git_repo_url = _prompt("GitOps template repo URL", default=DEFAULT_GIT_REPO)
    git_branch = _prompt("Git branch", default="main")

    print()
    print(dim("  Admin user configuration (credentials stored in Secrets Manager — never in CloudFormation console history):"))
    admin_email = _prompt("Admin email", default="admin@example.com")
    admin_user  = _prompt("Admin username", default="admin")
    admin_name  = _prompt("Admin full name", default="Coder Admin")

    license_key = _prompt(
        "Coder Premium license key (enables HA + premium features; blank = Community Edition)",
        default="", secret=True,
    )

    print()
    pipeline_template = _prompt(
        "Path to codebuild_image_pipeline.yaml",
        default="./infrastructure/codebuild_image_pipeline.yaml",
    )
    core_template = _prompt(
        "Path to coder_deployment.yaml",
        default="./infrastructure/coder_deployment.yaml",
    )

    # Validate template paths
    for path_str, name in [
        (pipeline_template, "Image pipeline template"),
        (core_template, "Core deployment template"),
    ]:
        p = Path(path_str)
        if not p.exists():
            print(warn(f"{name} not found at '{path_str}'."))
            print(info("Make sure you have cloned the repo and are running the wizard from its root."))
            if not _confirm("Continue anyway?", default=False):
                return 1

    # ── Pre-flight ─────────────────────────────────────────────────────────
    _section("Pre-flight Checks")
    report = preflight.run_all(region=region, eks_cluster_name=cluster, skip_ecr=True)
    for r in report.results:
        print(_status_line(r))

    if not report.passed:
        print()
        print(fail("Pre-flight checks failed. Resolve the issues above and re-run this wizard."))
        return 1

    print()
    print(ok("Pre-flight checks passed!"))

    # ── Cost estimate ──────────────────────────────────────────────────────
    _section("Cost Estimate")
    est = cost_estimate.estimate(developer_count=developers, region=region, spot_fraction=spot_fraction)
    print(est.format_table())

    if not _confirm("Does this look acceptable? Proceed to deploy?"):
        print(info("Deployment cancelled. Re-run when ready."))
        return 0

    # ── Dry run or live deploy? ────────────────────────────────────────────
    dry_run = getattr(_args, "dry_run", False)
    if not dry_run:
        dry_run = not _confirm(
            "Deploy now? (choose 'n' to generate parameter files only — dry run)",
            default=True,
        )

    class _Args:
        pass

    deploy_args = _Args()
    deploy_args.region = region
    deploy_args.cluster = cluster
    deploy_args.developers = developers
    deploy_args.spot_fraction = str(spot_fraction)
    deploy_args.git_repo_url = git_repo_url
    deploy_args.git_branch = git_branch
    deploy_args.admin_email = admin_email
    deploy_args.admin_user = admin_user
    deploy_args.admin_name = admin_name
    deploy_args.admin_password = ""   # auto-generate
    deploy_args.license_key = license_key
    deploy_args.pipeline_template = pipeline_template
    deploy_args.core_template = core_template
    deploy_args.k8s_version = DEFAULT_K8S
    deploy_args.coder_version = coder_version
    deploy_args.yes = True   # cost already confirmed above
    deploy_args.dry_run = dry_run
    deploy_args.dry_run_output = f"coder-deploy-{cluster}"
    deploy_args.retry_flag = False

    return cmd_deploy(deploy_args)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="partner-coder-wizard",
        description="GenAI-assisted install wizard for the Partner AI-DLC Coder demo on AWS.",
    )
    parser.set_defaults(func=cmd_wizard)

    sub = parser.add_subparsers(dest="command", title="commands")

    # ── wizard (default) ───────────────────────────────────────────────────
    wizard_p = sub.add_parser("wizard", help="Interactive guided install (default)")
    wizard_p.add_argument("--dry-run", action="store_true",
                          help="Skip the 'deploy now?' prompt and go straight to dry-run output")
    wizard_p.add_argument("--retry", dest="retry_flag", action="store_true",
                          help="Resume the last deployment using saved parameters (RetryFlag=True)")
    wizard_p.set_defaults(func=cmd_wizard)

    # ── preflight ──────────────────────────────────────────────────────────
    pf = sub.add_parser("preflight", help="Run pre-flight checks only")
    pf.add_argument("--region",   default=_get_current_region(), help="AWS region")
    pf.add_argument("--cluster",  default=DEFAULT_CLUSTER,       help="EKS cluster name")
    pf.add_argument("--skip-ecr", action="store_true",           help="Skip ECR image check")
    pf.set_defaults(func=cmd_preflight)

    # ── cost ───────────────────────────────────────────────────────────────
    cost_p = sub.add_parser("cost", help="Show monthly cost estimate")
    cost_p.add_argument("--region",        default=_get_current_region())
    cost_p.add_argument("--developers",    type=int, default=10)
    cost_p.add_argument("--hours-per-day", type=float, default=6.0)
    cost_p.add_argument("--spot-fraction", default="0",
                        help="Share of workspaces on the EC2 Spot lane (e.g. 40, 40%%, or 0.4)")
    cost_p.set_defaults(func=cmd_cost)

    # ── deploy ─────────────────────────────────────────────────────────────
    dep = sub.add_parser("deploy", help="Full deploy (preflight → cost → stacks → validate)")
    dep.add_argument("--region",          default=_get_current_region())
    dep.add_argument("--cluster",         default=DEFAULT_CLUSTER)
    dep.add_argument("--developers",      type=int, default=10)
    dep.add_argument("--spot-fraction",   default="0",
                     help="Share of workspaces on the EC2 Spot lane (cost estimate only)")
    dep.add_argument("--pipeline-template", default="./infrastructure/codebuild_image_pipeline.yaml")
    dep.add_argument("--core-template",     default="./infrastructure/coder_deployment.yaml")
    dep.add_argument("--git-repo-url",    default=DEFAULT_GIT_REPO)
    dep.add_argument("--git-branch",      default="main")
    dep.add_argument("--admin-email",     default="",
                     help="Coder admin email (required unless --retry resumes saved state)")
    dep.add_argument("--admin-user",      default="admin")
    dep.add_argument("--admin-name",      default="Coder Admin")
    dep.add_argument("--admin-password",  default="", help="Leave blank to auto-generate")
    dep.add_argument("--license-key",     default="", help="Coder Premium license JWT (enables HA/premium)")
    dep.add_argument("--k8s-version",     default=DEFAULT_K8S)
    dep.add_argument("--coder-version",   default=DEFAULT_CODER_VER)
    dep.add_argument("--yes",             action="store_true", help="Skip cost confirmation")
    dep.add_argument("--dry-run",         action="store_true",
                     help="Generate parameter files and deploy script without creating any AWS resources")
    dep.add_argument("--dry-run-output",  default=None, metavar="DIR",
                     help="Directory to write dry-run files (default: coder-deploy-<cluster>)")
    dep.add_argument("--retry", dest="retry_flag", action="store_true",
                     help="Resume the last deployment using saved parameters: reloads the last "
                          "params, assesses the image-pipeline and core stacks, and recreates/resumes "
                          "the core stack with CFN RetryFlag=True.")
    dep.set_defaults(func=cmd_deploy)

    # ── validate ───────────────────────────────────────────────────────────
    val = sub.add_parser("validate", help="Run post-install validation against an existing deployment")
    val.add_argument("--coder-url",   required=True)
    val.add_argument("--cluster",     required=True)
    val.add_argument("--efs-id",      required=True)
    val.add_argument("--region",      default=_get_current_region())
    val.add_argument("--token",       default="", help="Coder session token (or set --stack-name to auto-retrieve)")
    val.add_argument("--stack-name",  default="",  help="CloudFormation stack name for token auto-retrieval")
    val.set_defaults(func=cmd_validate)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
