"""
Tear-down orchestrator - removes all resources created by the install wizard
in the correct dependency order.

Deletion order (dependencies removed before the resources that block on them):
  1. EKS cluster (eksctl delete cluster) - removes the cluster, its managed
     eksctl sub-stacks (addons, nodegroups), the Fargate profile, load
     balancers, and ENIs that block VPC deletion.
  2. S3 buckets (except the wizard staging bucket) - CloudFormation cannot
     delete non-empty buckets, so they are emptied and deleted first.
  3. IAM users (Bedrock API-key user) - the user carries a service-specific
     credential created out of band (not tracked by CloudFormation), so its
     credentials/policies are stripped and the user deleted BEFORE the core
     stack; otherwise CloudFormation cannot delete the user.
  4. Aurora cluster/instance (only with --delete-data) - uses DeletionPolicy:
     Retain, so it is deleted BEFORE the core stack; otherwise the running
     instance blocks the (non-retained) DB subnet group + security group.
  5. Core Coder CloudFormation stack ({cluster}-coder) - removes the VPC,
     subnets, CloudFront, Secrets Manager, IAM roles, KMS key, CodeBuild
     project, etc. On DELETE_FAILED it retries while retaining any resource it
     still cannot delete, so the stack reaches DELETE_COMPLETE and the retained
     resources are surfaced as warnings.
  6. Image pipeline stack ({cluster}-image-pipeline) and ECR repos.
  7. Retained EFS file system (only with --delete-data).
  8. Leftover Secrets Manager secrets.
  9. Wizard staging bucket (coder-wizard-templates-{account}-{region}), last.

Resources with DeletionPolicy: Retain (Aurora, EFS) survive stack deletion by
design; pass --delete-data to remove them (and their stack-managed network
dependencies) as part of the teardown.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class TeardownStep:
    name: str
    status: str = "pending"  # pending | running | ok | skipped | failed
    message: str = ""
    detail: str = ""


@dataclass
class TeardownResult:
    success: bool
    steps: list[TeardownStep] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _run(args: list[str], timeout: int = 600) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "command timed out"


def _aws(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    return _run(["aws"] + args + ["--output", "json"], timeout=timeout)


def _aws_text(args: list[str], timeout: int = 120) -> tuple[int, str]:
    code, out, err = _run(["aws"] + args + ["--output", "text"], timeout=timeout)
    return code, out if code == 0 else err


def _account_id() -> str:
    code, out = _aws_text(["sts", "get-caller-identity", "--query", "Account"])
    return out.strip() if code == 0 else ""


# ---------------------------------------------------------------------------
# Discovery: find all resources belonging to a cluster
# ---------------------------------------------------------------------------

def discover_resources(cluster: str, region: str) -> dict:
    """Discover all AWS resources associated with a wizard deployment.

    Returns a dict describing the resources found, keyed by type.
    """
    resources: dict = {
        "cluster": cluster,
        "region": region,
        "eks_cluster": None,
        "cfn_stacks": [],
        "eksctl_stacks": [],
        "ecr_repos": [],
        "s3_buckets": [],
        "efs_filesystems": [],
        "aurora_clusters": [],
        "secrets": [],
        "cloudfront_distributions": [],
        "iam_users": [],
    }

    # EKS cluster
    code, out, _ = _aws(["eks", "describe-cluster", "--name", cluster, "--region", region])
    if code == 0:
        try:
            data = json.loads(out)
            resources["eks_cluster"] = {
                "name": cluster,
                "status": data.get("cluster", {}).get("status", "UNKNOWN"),
                "region": region,
            }
        except json.JSONDecodeError:
            pass

    # CloudFormation stacks (core + pipeline)
    core_stack = f"{cluster}-coder"
    pipeline_stack = f"{cluster}-image-pipeline"

    for stack_name in [core_stack, pipeline_stack]:
        code, out, _ = _aws([
            "cloudformation", "describe-stacks",
            "--stack-name", stack_name, "--region", region,
        ])
        if code == 0:
            try:
                data = json.loads(out)
                stacks = data.get("Stacks", [])
                if stacks:
                    resources["cfn_stacks"].append({
                        "name": stack_name,
                        "status": stacks[0].get("StackStatus", "UNKNOWN"),
                        "outputs": {
                            o["OutputKey"]: o["OutputValue"]
                            for o in stacks[0].get("Outputs", [])
                        },
                    })
            except json.JSONDecodeError:
                pass

    # eksctl-managed stacks
    for suffix in ["cluster", "addon-aws-ebs-csi-driver", "addon-aws-efs-csi-driver"]:
        eksctl_name = f"eksctl-{cluster}-{suffix}"
        code, out, _ = _aws([
            "cloudformation", "describe-stacks",
            "--stack-name", eksctl_name, "--region", region,
        ])
        if code == 0:
            try:
                data = json.loads(out)
                stacks = data.get("Stacks", [])
                if stacks:
                    resources["eksctl_stacks"].append({
                        "name": eksctl_name,
                        "status": stacks[0].get("StackStatus", "UNKNOWN"),
                    })
            except json.JSONDecodeError:
                pass

    # ECR repositories
    code, out, _ = _aws(["ecr", "describe-repositories", "--region", region])
    if code == 0:
        try:
            data = json.loads(out)
            for repo in data.get("repositories", []):
                name = repo.get("repositoryName", "")
                if name.startswith(f"{cluster}/"):
                    resources["ecr_repos"].append({
                        "name": name,
                        "arn": repo.get("repositoryArn", ""),
                        "uri": repo.get("repositoryUri", ""),
                    })
        except json.JSONDecodeError:
            pass

    # S3 buckets
    account = _account_id()
    candidate_buckets = [
        f"{core_stack}-cf-logs-{account}",
        f"{core_stack}-logs-{account}",
        f"coder-wizard-templates-{account}-{region}",
    ]
    for bucket in candidate_buckets:
        code, _, _ = _run(["aws", "s3api", "head-bucket", "--bucket", bucket, "--region", region])
        if code == 0:
            resources["s3_buckets"].append({"name": bucket})

    # EFS file systems
    code, out, _ = _aws(["efs", "describe-file-systems", "--region", region])
    if code == 0:
        try:
            data = json.loads(out)
            for fs in data.get("FileSystems", []):
                name = fs.get("Name", "")
                if cluster in name:
                    resources["efs_filesystems"].append({
                        "id": fs.get("FileSystemId", ""),
                        "name": name,
                        "state": fs.get("LifeCycleState", ""),
                        "size_bytes": fs.get("SizeInBytes", {}).get("Value", 0),
                    })
        except json.JSONDecodeError:
            pass

    # Aurora clusters
    code, out, _ = _aws(["rds", "describe-db-clusters", "--region", region])
    if code == 0:
        try:
            data = json.loads(out)
            for db in data.get("DBClusters", []):
                db_id = db.get("DBClusterIdentifier", "")
                if cluster in db_id:
                    resources["aurora_clusters"].append({
                        "id": db_id,
                        "status": db.get("Status", ""),
                        "engine": db.get("Engine", ""),
                        "endpoint": db.get("Endpoint", ""),
                        "instances": [
                            m.get("DBInstanceIdentifier", "")
                            for m in db.get("DBClusterMembers", [])
                        ],
                    })
        except json.JSONDecodeError:
            pass

    # Secrets Manager
    code, out, _ = _aws(["secretsmanager", "list-secrets", "--region", region])
    if code == 0:
        try:
            data = json.loads(out)
            for s in data.get("SecretList", []):
                name = s.get("Name", "")
                if cluster in name:
                    resources["secrets"].append({
                        "name": name,
                        "arn": s.get("ARN", ""),
                    })
        except json.JSONDecodeError:
            pass

    # CloudFront (from stack outputs)
    for stack in resources["cfn_stacks"]:
        cf_id = stack.get("outputs", {}).get("CloudFrontDistributionId", "")
        if cf_id:
            resources["cloudfront_distributions"].append({"id": cf_id})

    # IAM users (Bedrock API key user)
    code, out, _ = _aws(["iam", "list-users"])
    if code == 0:
        try:
            data = json.loads(out)
            for u in data.get("Users", []):
                name = u.get("UserName", "")
                if cluster in name and "bedrock" in name.lower():
                    resources["iam_users"].append({
                        "name": name,
                        "arn": u.get("Arn", ""),
                    })
        except json.JSONDecodeError:
            pass

    return resources


# ---------------------------------------------------------------------------
# Deletion helpers
# ---------------------------------------------------------------------------

def _delete_eks_cluster(
    cluster: str, region: str,
    on_status: Callable[[str], None] | None = None,
) -> TeardownStep:
    """Delete the EKS cluster using eksctl (handles Fargate profiles, addons, etc.)."""
    step = TeardownStep(name=f"EKS cluster: {cluster}", status="running")
    if on_status:
        on_status(f"Deleting EKS cluster '{cluster}' (this takes 10-15 min)...")

    code, out, err = _run(
        ["eksctl", "delete", "cluster", "--name", cluster, "--region", region, "--wait"],
        timeout=1800,  # 30 min
    )
    if code == 0:
        step.status = "ok"
        step.message = "Deleted"
    else:
        # eksctl may not be installed; fall back to AWS CLI
        if "eksctl" in err.lower() or "not found" in err.lower():
            step.detail = "eksctl not available; attempting AWS CLI fallback..."
            if on_status:
                on_status("eksctl not available; deleting Fargate profiles and cluster via AWS CLI...")
            return _delete_eks_cluster_cli(cluster, region, on_status)
        step.status = "failed"
        step.message = f"eksctl delete failed: {err[:200]}"
    return step


def _delete_eks_cluster_cli(
    cluster: str, region: str,
    on_status: Callable[[str], None] | None = None,
) -> TeardownStep:
    """Fallback: delete EKS cluster via AWS CLI when eksctl is not available."""
    step = TeardownStep(name=f"EKS cluster: {cluster}", status="running")

    # 1. Delete Fargate profiles
    code, out, _ = _aws(["eks", "list-fargate-profiles", "--cluster-name", cluster, "--region", region])
    if code == 0:
        try:
            profiles = json.loads(out).get("fargateProfileNames", [])
            for profile in profiles:
                if on_status:
                    on_status(f"  Deleting Fargate profile '{profile}'...")
                _aws(["eks", "delete-fargate-profile",
                       "--cluster-name", cluster,
                       "--fargate-profile-name", profile,
                       "--region", region])
                # Wait for profile deletion
                for _ in range(60):
                    c, o, _ = _aws(["eks", "describe-fargate-profile",
                                     "--cluster-name", cluster,
                                     "--fargate-profile-name", profile,
                                     "--region", region])
                    if c != 0:
                        break
                    time.sleep(10)
        except (json.JSONDecodeError, KeyError):
            pass

    # 2. Delete nodegroups
    code, out, _ = _aws(["eks", "list-nodegroups", "--cluster-name", cluster, "--region", region])
    if code == 0:
        try:
            nodegroups = json.loads(out).get("nodegroups", [])
            for ng in nodegroups:
                if on_status:
                    on_status(f"  Deleting nodegroup '{ng}'...")
                _aws(["eks", "delete-nodegroup",
                       "--cluster-name", cluster,
                       "--nodegroup-name", ng,
                       "--region", region])
                _aws(["eks", "wait", "nodegroup-deleted",
                       "--cluster-name", cluster,
                       "--nodegroup-name", ng,
                       "--region", region], timeout=600)
        except (json.JSONDecodeError, KeyError):
            pass

    # 3. Delete the cluster itself
    if on_status:
        on_status(f"  Deleting EKS cluster '{cluster}'...")
    code, _, err = _aws(["eks", "delete-cluster", "--name", cluster, "--region", region])
    if code != 0 and "ResourceNotFoundException" not in err:
        step.status = "failed"
        step.message = f"delete-cluster failed: {err[:200]}"
        return step

    # Wait for cluster deletion
    for _ in range(120):  # up to 20 min
        c, _, _ = _aws(["eks", "describe-cluster", "--name", cluster, "--region", region])
        if c != 0:
            break
        time.sleep(10)

    step.status = "ok"
    step.message = "Deleted via AWS CLI"
    return step


def _stack_failed_resources(stack_name: str, region: str) -> list[str]:
    """Return the logical IDs of resources currently in DELETE_FAILED."""
    code, out, _ = _aws([
        "cloudformation", "list-stack-resources",
        "--stack-name", stack_name, "--region", region,
    ])
    ids: list[str] = []
    if code == 0:
        try:
            for r in json.loads(out).get("StackResourceSummaries", []):
                if r.get("ResourceStatus") == "DELETE_FAILED":
                    ids.append(r["LogicalResourceId"])
        except (json.JSONDecodeError, KeyError):
            pass
    return ids


def _delete_cfn_stack(
    stack_name: str, region: str,
    on_status: Callable[[str], None] | None = None,
    retain_resources: list[str] | None = None,
    auto_retain_on_failure: bool = False,
) -> TeardownStep:
    """Delete a CloudFormation stack and wait for completion.

    When ``auto_retain_on_failure`` is set, a stack that reaches DELETE_FAILED is
    retried with ``--retain-resources`` set to the resources that could not be
    deleted (accumulated across attempts). This lets the stack reach
    DELETE_COMPLETE even when a few resources are un-deletable by CloudFormation
    (e.g. an IAM user with an out-of-band service-specific credential, or
    resources that depend on a retained Aurora cluster). Retained resource IDs
    are reported in ``step.detail`` so the caller can surface them.
    """
    step = TeardownStep(name=f"CFN stack: {stack_name}", status="running")
    if on_status:
        on_status(f"Deleting CloudFormation stack '{stack_name}'...")

    retained: list[str] = list(retain_resources or [])
    max_attempts = 5 if auto_retain_on_failure else 1

    for attempt in range(max_attempts):
        delete_args = [
            "cloudformation", "delete-stack",
            "--stack-name", stack_name,
            "--region", region,
        ]
        if retained:
            delete_args += ["--retain-resources"] + retained

        code, _, err = _aws(delete_args)
        if code != 0:
            if "does not exist" in err:
                step.status = "skipped"
                step.message = "Stack does not exist"
                return step
            step.status = "failed"
            step.message = f"delete-stack failed: {err[:200]}"
            return step

        # Wait for deletion
        if on_status:
            on_status(f"  Waiting for stack '{stack_name}' to be deleted...")
        status = ""
        for _ in range(180):  # up to 45 min
            c, out, _ = _aws([
                "cloudformation", "describe-stacks",
                "--stack-name", stack_name, "--region", region,
            ])
            if c != 0:
                status = "DELETE_COMPLETE"  # stack no longer exists
                break
            try:
                status = json.loads(out).get("Stacks", [{}])[0].get("StackStatus", "")
            except (json.JSONDecodeError, IndexError):
                status = ""
            if status in ("DELETE_COMPLETE", "DELETE_FAILED"):
                break
            time.sleep(15)

        if status == "DELETE_COMPLETE":
            step.status = "ok"
            if retained:
                step.message = f"Deleted (retained {len(retained)} un-deletable resource(s))"
                step.detail = "Retained: " + ", ".join(sorted(set(retained)))
            else:
                step.message = "Deleted"
            return step

        if status == "DELETE_FAILED":
            failed = _stack_failed_resources(stack_name, region)
            if not auto_retain_on_failure or attempt == max_attempts - 1:
                step.status = "failed"
                step.message = "Stack deletion failed (status: DELETE_FAILED)"
                step.detail = (
                    "Resources that could not be deleted: " + ", ".join(failed)
                    if failed else
                    "Check the CloudFormation console for resources that could not be deleted."
                )
                return step
            newly = [x for x in failed if x not in retained]
            if not newly:
                step.status = "failed"
                step.message = "Stack deletion failed (no further resources to retain)"
                step.detail = "Stuck resources: " + ", ".join(failed) if failed else None
                return step
            retained += newly
            if on_status:
                on_status(f"  Retrying delete, retaining stuck resource(s): {', '.join(newly)}")
            continue

        # Timed out waiting
        step.status = "failed"
        step.message = "Timed out waiting for stack deletion"
        return step

    return step


def _empty_and_delete_bucket(
    bucket: str, region: str,
    on_status: Callable[[str], None] | None = None,
) -> TeardownStep:
    """Empty and delete an S3 bucket."""
    step = TeardownStep(name=f"S3 bucket: {bucket}", status="running")
    if on_status:
        on_status(f"Emptying S3 bucket '{bucket}'...")

    # Empty the bucket (including versioned objects)
    _run(["aws", "s3", "rm", f"s3://{bucket}", "--recursive", "--region", region], timeout=300)

    # Delete object versions if versioning was enabled
    code, out, _ = _aws(["s3api", "list-object-versions", "--bucket", bucket,
                          "--region", region, "--query",
                          "{Objects: Versions[].{Key:Key,VersionId:VersionId}}"])
    if code == 0 and out:
        try:
            data = json.loads(out)
            objects = data.get("Objects")
            if objects:
                delete_payload = json.dumps({"Objects": objects, "Quiet": True})
                _run(["aws", "s3api", "delete-objects", "--bucket", bucket,
                      "--region", region, "--delete", delete_payload], timeout=120)
        except (json.JSONDecodeError, TypeError):
            pass

    # Delete markers
    code, out, _ = _aws(["s3api", "list-object-versions", "--bucket", bucket,
                          "--region", region, "--query",
                          "{Objects: DeleteMarkers[].{Key:Key,VersionId:VersionId}}"])
    if code == 0 and out:
        try:
            data = json.loads(out)
            objects = data.get("Objects")
            if objects:
                delete_payload = json.dumps({"Objects": objects, "Quiet": True})
                _run(["aws", "s3api", "delete-objects", "--bucket", bucket,
                      "--region", region, "--delete", delete_payload], timeout=120)
        except (json.JSONDecodeError, TypeError):
            pass

    # Delete the bucket
    if on_status:
        on_status(f"Deleting S3 bucket '{bucket}'...")
    code, _, err = _run(["aws", "s3api", "delete-bucket", "--bucket", bucket, "--region", region])
    if code == 0:
        step.status = "ok"
        step.message = "Deleted"
    elif "NoSuchBucket" in err:
        step.status = "skipped"
        step.message = "Bucket does not exist"
    else:
        step.status = "failed"
        step.message = f"Could not delete bucket: {err[:200]}"
    return step


def _delete_ecr_repo(
    repo_name: str, region: str,
    on_status: Callable[[str], None] | None = None,
) -> TeardownStep:
    """Delete an ECR repository and all its images."""
    step = TeardownStep(name=f"ECR repo: {repo_name}", status="running")
    if on_status:
        on_status(f"Deleting ECR repository '{repo_name}'...")

    code, _, err = _aws([
        "ecr", "delete-repository",
        "--repository-name", repo_name,
        "--region", region,
        "--force",  # deletes all images
    ])
    if code == 0:
        step.status = "ok"
        step.message = "Deleted"
    elif "RepositoryNotFoundException" in err:
        step.status = "skipped"
        step.message = "Repository does not exist"
    else:
        step.status = "failed"
        step.message = f"delete-repository failed: {err[:200]}"
    return step


def _delete_efs(
    fs_id: str, region: str,
    on_status: Callable[[str], None] | None = None,
) -> TeardownStep:
    """Delete an EFS file system (after removing mount targets)."""
    step = TeardownStep(name=f"EFS: {fs_id}", status="running")
    if on_status:
        on_status(f"Deleting EFS mount targets for '{fs_id}'...")

    # Delete mount targets first
    code, out, _ = _aws(["efs", "describe-mount-targets",
                          "--file-system-id", fs_id, "--region", region])
    if code == 0:
        try:
            mts = json.loads(out).get("MountTargets", [])
            for mt in mts:
                mt_id = mt.get("MountTargetId", "")
                _aws(["efs", "delete-mount-target",
                       "--mount-target-id", mt_id, "--region", region])
            # Wait for mount targets to be deleted
            if mts:
                if on_status:
                    on_status(f"  Waiting for {len(mts)} mount target(s) to be deleted...")
                time.sleep(30)
                for _ in range(30):
                    c, o, _ = _aws(["efs", "describe-mount-targets",
                                     "--file-system-id", fs_id, "--region", region])
                    if c != 0:
                        break
                    remaining = json.loads(o).get("MountTargets", [])
                    if not remaining:
                        break
                    time.sleep(10)
        except (json.JSONDecodeError, KeyError):
            pass

    if on_status:
        on_status(f"Deleting EFS file system '{fs_id}'...")
    code, _, err = _aws(["efs", "delete-file-system",
                          "--file-system-id", fs_id, "--region", region])
    if code == 0:
        step.status = "ok"
        step.message = "Deleted"
    elif "FileSystemNotFound" in err:
        step.status = "skipped"
        step.message = "File system does not exist"
    else:
        step.status = "failed"
        step.message = f"delete-file-system failed: {err[:200]}"
    return step


def _delete_aurora(
    cluster_id: str, instances: list[str], region: str,
    on_status: Callable[[str], None] | None = None,
) -> TeardownStep:
    """Delete an Aurora cluster and its instances (skip-final-snapshot)."""
    step = TeardownStep(name=f"Aurora: {cluster_id}", status="running")

    # Delete instances first
    for inst in instances:
        if on_status:
            on_status(f"Deleting Aurora instance '{inst}'...")
        _aws(["rds", "delete-db-instance",
               "--db-instance-identifier", inst,
               "--skip-final-snapshot",
               "--region", region])

    # Wait for instances to be deleted
    for inst in instances:
        if on_status:
            on_status(f"  Waiting for instance '{inst}' deletion...")
        for _ in range(120):
            c, o, _ = _aws(["rds", "describe-db-instances",
                             "--db-instance-identifier", inst,
                             "--region", region])
            if c != 0:
                break
            try:
                status = json.loads(o).get("DBInstances", [{}])[0].get("DBInstanceStatus", "")
                if status == "deleting":
                    time.sleep(15)
                    continue
            except (json.JSONDecodeError, IndexError):
                break
            time.sleep(15)

    # Delete the cluster
    if on_status:
        on_status(f"Deleting Aurora cluster '{cluster_id}'...")
    code, _, err = _aws([
        "rds", "delete-db-cluster",
        "--db-cluster-identifier", cluster_id,
        "--skip-final-snapshot",
        "--region", region,
    ])
    if code == 0 or "DBClusterNotFoundFault" in err:
        step.status = "ok"
        step.message = "Deleted"
    else:
        step.status = "failed"
        step.message = f"delete-db-cluster failed: {err[:200]}"
    return step


def _delete_secrets(
    secrets: list[dict], region: str,
    on_status: Callable[[str], None] | None = None,
) -> TeardownStep:
    """Delete Secrets Manager secrets (force, no recovery window)."""
    step = TeardownStep(name=f"Secrets Manager ({len(secrets)} secret(s))", status="running")
    failed = []
    for s in secrets:
        arn = s.get("arn", "")
        name = s.get("name", arn)
        if on_status:
            on_status(f"Deleting secret '{name}'...")
        code, _, err = _aws([
            "secretsmanager", "delete-secret",
            "--secret-id", arn,
            "--force-delete-without-recovery",
            "--region", region,
        ])
        if code != 0 and "ResourceNotFoundException" not in err:
            failed.append(name)

    if not failed:
        step.status = "ok"
        step.message = f"Deleted {len(secrets)} secret(s)"
    else:
        step.status = "failed"
        step.message = f"Failed to delete: {', '.join(failed)}"
    return step


def _delete_iam_user(
    username: str,
    on_status: Callable[[str], None] | None = None,
) -> TeardownStep:
    """Delete an IAM user after removing its credentials and policies."""
    step = TeardownStep(name=f"IAM user: {username}", status="running")
    if on_status:
        on_status(f"Cleaning up IAM user '{username}'...")

    # Delete access keys
    code, out, _ = _aws(["iam", "list-access-keys", "--user-name", username])
    if code == 0:
        try:
            for key in json.loads(out).get("AccessKeyMetadata", []):
                _aws(["iam", "delete-access-key", "--user-name", username,
                       "--access-key-id", key["AccessKeyId"]])
        except (json.JSONDecodeError, KeyError):
            pass

    # Delete service-specific credentials
    code, out, _ = _aws(["iam", "list-service-specific-credentials", "--user-name", username])
    if code == 0:
        try:
            for cred in json.loads(out).get("ServiceSpecificCredentials", []):
                _aws(["iam", "delete-service-specific-credential", "--user-name", username,
                       "--service-specific-credential-id", cred["ServiceSpecificCredentialId"]])
        except (json.JSONDecodeError, KeyError):
            pass

    # Detach managed policies
    code, out, _ = _aws(["iam", "list-attached-user-policies", "--user-name", username])
    if code == 0:
        try:
            for pol in json.loads(out).get("AttachedPolicies", []):
                _aws(["iam", "detach-user-policy", "--user-name", username,
                       "--policy-arn", pol["PolicyArn"]])
        except (json.JSONDecodeError, KeyError):
            pass

    # Delete inline policies
    code, out, _ = _aws(["iam", "list-user-policies", "--user-name", username])
    if code == 0:
        try:
            for pol_name in json.loads(out).get("PolicyNames", []):
                _aws(["iam", "delete-user-policy", "--user-name", username,
                       "--policy-name", pol_name])
        except (json.JSONDecodeError, KeyError):
            pass

    # Delete the user
    code, _, err = _aws(["iam", "delete-user", "--user-name", username])
    if code == 0 or "NoSuchEntity" in err:
        step.status = "ok"
        step.message = "Deleted"
    else:
        step.status = "failed"
        step.message = f"delete-user failed: {err[:200]}"
    return step


# ---------------------------------------------------------------------------
# Main teardown orchestrator
# ---------------------------------------------------------------------------

def run_teardown(
    cluster: str,
    region: str,
    delete_data: bool = False,
    on_status: Callable[[str], None] | None = None,
) -> TeardownResult:
    """Execute the full teardown sequence.

    Args:
        cluster:     EKS cluster name (the deployment's identifier).
        region:      AWS region.
        delete_data: If True, also delete retained Aurora + EFS (data loss!).
        on_status:   Callback for progress messages.

    Returns a TeardownResult with the outcome of each step.
    """
    result = TeardownResult(success=True)

    if on_status:
        on_status("Discovering resources...")
    resources = discover_resources(cluster, region)

    # ── 1. Delete EKS cluster ──────────────────────────────────────────────
    if resources["eks_cluster"]:
        step = _delete_eks_cluster(cluster, region, on_status)
        result.steps.append(step)
        if step.status == "failed":
            result.success = False
            result.warnings.append(
                f"EKS cluster deletion failed. You may need to delete it manually:\n"
                f"  eksctl delete cluster --name {cluster} --region {region} --wait"
            )
    else:
        result.steps.append(TeardownStep(
            name=f"EKS cluster: {cluster}", status="skipped",
            message="Cluster not found (already deleted or never created)",
        ))

    # ── 2. Delete eksctl sub-stacks ────────────────────────────────────────
    # eksctl delete cluster should handle these, but clean up any stragglers.
    for stack_info in resources["eksctl_stacks"]:
        name = stack_info["name"]
        code, _, _ = _aws([
            "cloudformation", "describe-stacks",
            "--stack-name", name, "--region", region,
        ])
        if code == 0:
            step = _delete_cfn_stack(name, region, on_status)
            result.steps.append(step)
            if step.status == "failed":
                result.warnings.append(f"eksctl stack '{name}' could not be deleted.")

    # ── 3. Empty and delete S3 buckets (must be done before CFN stack) ─────
    for bucket_info in resources["s3_buckets"]:
        bucket = bucket_info["name"]
        if "wizard-templates" in bucket:
            continue  # delete last
        step = _empty_and_delete_bucket(bucket, region, on_status)
        result.steps.append(step)

    # ── 4. Clean up IAM users BEFORE the core stack ────────────────────────
    #     The Bedrock API-key user carries a service-specific credential created
    #     out of band (not tracked by CloudFormation), so CFN cannot delete the
    #     user while it exists. Strip the user's credentials/policies (and delete
    #     the user) first so the core stack delete does not fail on it. If we lack
    #     IAM permissions here, the stack delete below will retain the user and
    #     continue (see auto_retain_on_failure).
    for user in resources["iam_users"]:
        step = _delete_iam_user(user["name"], on_status)
        result.steps.append(step)
        if step.status == "failed":
            result.warnings.append(
                f"Could not fully clean up IAM user '{user['name']}'. If it blocks "
                f"stack deletion it will be retained; remove it manually with:\n"
                f"  aws iam delete-user --user-name {user['name']}"
            )

    # ── 5. Delete retained Aurora BEFORE the core stack (only --delete-data) ─
    #     Aurora cluster/instance use DeletionPolicy: Retain, so the stack delete
    #     skips them - but the (non-retained) DB subnet group and security group
    #     depend on the running instance and fail to delete while it exists.
    #     Deleting Aurora up front lets those dependents delete cleanly. Without
    #     --delete-data, Aurora and its dependent subnet group/security group are
    #     left in place (the stack delete below retains whatever it cannot remove).
    if delete_data:
        for aurora in resources["aurora_clusters"]:
            step = _delete_aurora(
                aurora["id"], aurora.get("instances", []), region, on_status,
            )
            result.steps.append(step)
    else:
        for aurora in resources["aurora_clusters"]:
            result.steps.append(TeardownStep(
                name=f"Aurora: {aurora['id']}", status="skipped",
                message="Retained (use --delete-data to remove)",
            ))

    # ── 6. Delete core Coder stack ─────────────────────────────────────────
    core_stack = f"{cluster}-coder"
    core_found = any(s["name"] == core_stack for s in resources["cfn_stacks"])
    if core_found:
        step = _delete_cfn_stack(
            core_stack, region, on_status, auto_retain_on_failure=True,
        )
        result.steps.append(step)
        if step.status == "failed":
            result.success = False
            result.warnings.append(
                f"Core stack deletion failed. Try deleting manually:\n"
                f"  aws cloudformation delete-stack --stack-name {core_stack} --region {region}"
            )
        elif step.detail and step.detail.startswith("Retained:"):
            result.warnings.append(
                f"Core stack deleted, but some resources could not be removed and "
                f"were retained ({step.detail}). Remove them manually."
            )
    else:
        result.steps.append(TeardownStep(
            name=f"CFN stack: {core_stack}", status="skipped",
            message="Stack not found",
        ))

    # ── 7. Delete ECR repos ────────────────────────────────────────────────
    for repo in resources["ecr_repos"]:
        step = _delete_ecr_repo(repo["name"], region, on_status)
        result.steps.append(step)

    # ── 8. Delete image pipeline stack ─────────────────────────────────────
    pipeline_stack = f"{cluster}-image-pipeline"
    pipeline_found = any(s["name"] == pipeline_stack for s in resources["cfn_stacks"])
    if pipeline_found:
        step = _delete_cfn_stack(pipeline_stack, region, on_status)
        result.steps.append(step)
        if step.status == "failed":
            result.warnings.append(f"Pipeline stack '{pipeline_stack}' could not be deleted.")
    else:
        result.steps.append(TeardownStep(
            name=f"CFN stack: {pipeline_stack}", status="skipped",
            message="Stack not found",
        ))

    # ── 9. Retained EFS (only with --delete-data) ──────────────────────────
    if delete_data:
        for efs in resources["efs_filesystems"]:
            step = _delete_efs(efs["id"], region, on_status)
            result.steps.append(step)
    else:
        for efs in resources["efs_filesystems"]:
            result.steps.append(TeardownStep(
                name=f"EFS: {efs['id']}", status="skipped",
                message="Retained (use --delete-data to remove)",
            ))

    # ── 10. Secrets Manager ────────────────────────────────────────────────
    if resources["secrets"]:
        step = _delete_secrets(resources["secrets"], region, on_status)
        result.steps.append(step)

    # ── 11. Wizard staging bucket (last) ───────────────────────────────────
    for bucket_info in resources["s3_buckets"]:
        bucket = bucket_info["name"]
        if "wizard-templates" in bucket:
            step = _empty_and_delete_bucket(bucket, region, on_status)
            result.steps.append(step)

    # Overall success
    result.success = all(
        s.status in ("ok", "skipped") for s in result.steps
    )
    return result
