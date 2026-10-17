#!/usr/bin/env python3
"""Request a Railway redeploy through the connected GitHub branch.

The live container has no Railway control credential. This script creates a
metadata-only commit through GitHub's Git Data API. Railway's existing GitHub
integration receives the push and owns the container replacement after this
process disappears.

Fail-closed invariants:
- dry-run unless --execute is supplied;
- a full Railway-provided live deployment SHA is mandatory;
- the connected branch must still equal that live SHA;
- the ref update is non-forced;
- success requires a new GitHub deployment record for the created SHA;
- no working tree or persistent state is modified.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

Run = Callable[..., subprocess.CompletedProcess[str]]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class DeploymentReceiptTimeout(RuntimeError):
    def __init__(self, commit_sha: str, timeout: float):
        self.commit_sha = commit_sha
        super().__init__(
            f"commit {commit_sha[:12]} was pushed but no Railway/GitHub deployment "
            f"record appeared within {timeout:g}s; Watch Paths or autodeploy may block it"
        )


def _gh_value(
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    run: Run = subprocess.run,
) -> Any:
    command = ["gh", "api", path, "--method", method]
    kwargs: dict = {"text": True, "capture_output": True, "check": False}
    if payload is not None:
        command.extend(["--input", "-"])
        kwargs["input"] = json.dumps(payload)
    result = run(command, **kwargs)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "unknown gh error"
        raise RuntimeError(f"GitHub API request failed ({method} {path}): {message}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GitHub API returned invalid JSON for {path}") from exc


def _gh_object(*args, **kwargs) -> dict:
    value = _gh_value(*args, **kwargs)
    if not isinstance(value, dict):
        raise RuntimeError("GitHub API returned an unexpected non-object payload")
    return value


def _require_sha(value: str, label: str) -> str:
    value = value.strip()
    if not SHA_RE.fullmatch(value):
        raise ValueError(f"{label} must be a full 40-character lowercase hexadecimal SHA")
    return value


def _pid1_environment(path: str = "/proc/1/environ") -> dict[str, str]:
    """Read platform variables before gateway/.env overlays can alter them."""
    try:
        with open(path, "rb") as stream:
            raw = stream.read()
    except OSError as exc:
        raise RuntimeError("cannot read PID 1 environment for Railway provenance") from exc
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        result[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return result


def request_redeploy(
    *,
    repo: str,
    branch: str,
    live_sha: str,
    railway_environment_id: str,
    railway_project_name: str,
    railway_environment_name: str,
    reason: str,
    execute: bool,
    receipt_timeout: float = 120.0,
    poll_interval: float = 2.0,
    run: Run = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if not repo or "/" not in repo:
        raise ValueError("repo must be OWNER/NAME")
    if not branch or branch.startswith("-") or ".." in branch:
        raise ValueError("invalid branch")
    live_sha = _require_sha(live_sha, "Railway live deployment SHA")
    railway_environment_id = railway_environment_id.strip()
    railway_project_name = railway_project_name.strip()
    railway_environment_name = railway_environment_name.strip()
    if not railway_environment_id:
        raise ValueError("Railway environment ID is required")
    if not railway_project_name or not railway_environment_name:
        raise ValueError("Railway project and environment names are required")

    reason = " ".join(reason.split()).strip()
    if not reason:
        raise ValueError("reason is required")
    if len(reason) > 160:
        raise ValueError("reason must be at most 160 characters")
    if receipt_timeout <= 0 or poll_interval <= 0:
        raise ValueError("receipt timeout and poll interval must be positive")

    ref_path = f"repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"
    ref = _gh_object(ref_path, run=run)
    head_sha = _require_sha(str(ref.get("object", {}).get("sha", "")), "GitHub branch SHA")
    if head_sha != live_sha:
        raise RuntimeError(
            "refusing redeploy: live deployment SHA does not match the GitHub branch "
            f"(live={live_sha[:12]}, branch={head_sha[:12]})"
        )

    parent = _gh_object(f"repos/{repo}/git/commits/{head_sha}", run=run)
    tree_sha = _require_sha(str(parent.get("tree", {}).get("sha", "")), "parent tree SHA")

    plan = {
        "status": "planned",
        "repo": repo,
        "branch": branch,
        "parent_sha": head_sha,
        "tree_sha": tree_sha,
        "reason": reason,
        "mutation": "metadata-only commit followed by non-forced ref update",
        "acceptance": (
            "Railway project/environment deployment record; exact-service activation "
            "still requires /health/ready at the known service domain"
        ),
    }
    if not execute:
        return plan

    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    created = _gh_object(
        f"repos/{repo}/git/commits",
        method="POST",
        payload={
            "message": f"chore: request Railway redeploy\n\nReason: {reason}\nRequested-at: {stamp}",
            "tree": tree_sha,
            "parents": [head_sha],
        },
        run=run,
    )
    new_sha = _require_sha(str(created.get("sha", "")), "created commit SHA")

    updated = _gh_object(
        f"repos/{repo}/git/refs/heads/{quote(branch, safe='/')}",
        method="PATCH",
        payload={"sha": new_sha, "force": False},
        run=run,
    )
    confirmed_sha = str(updated.get("object", {}).get("sha", ""))
    if confirmed_sha != new_sha:
        raise RuntimeError("GitHub ref update response did not confirm the new commit")

    deadline = monotonic() + receipt_timeout
    deployment = None
    query = f"repos/{repo}/deployments?sha={new_sha}&per_page=10"
    while monotonic() < deadline:
        records = _gh_value(query, run=run)
        if not isinstance(records, list):
            raise RuntimeError("GitHub deployments API returned an unexpected payload")
        expected_environment = f"{railway_project_name} / {railway_environment_name}"
        candidates = [
            item
            for item in records
            if isinstance(item, dict)
            and item.get("sha") == new_sha
            and (item.get("creator") or {}).get("login") == "railway-app[bot]"
            and isinstance(item.get("payload"), dict)
            and item["payload"].get("environmentId") == railway_environment_id
            and item.get("environment") == expected_environment
        ]
        deployment = None
        for candidate in candidates:
            statuses = _gh_value(
                f"repos/{repo}/deployments/{candidate.get('id')}/statuses?per_page=10",
                run=run,
            )
            if not isinstance(statuses, list):
                raise RuntimeError("GitHub deployment statuses API returned an unexpected payload")
            railway_status_present = any(
                isinstance(status, dict)
                and (status.get("creator") or {}).get("login") == "railway-app[bot]"
                for status in statuses
            )
            if railway_status_present:
                deployment = candidate
                break
        if deployment is not None:
            break
        sleep(poll_interval)
    if deployment is None:
        raise DeploymentReceiptTimeout(new_sha, receipt_timeout)

    return {
        **plan,
        "status": "project_deployment_created_pending_target_verification",
        "commit_sha": new_sha,
        "deployment_id": deployment.get("id"),
        "environment": deployment.get("environment"),
        "requested_at": stamp,
    }


def _defaults() -> tuple[str, str, str, str, str, str]:
    platform = _pid1_environment()
    owner = platform.get("RAILWAY_GIT_REPO_OWNER", "")
    name = platform.get("RAILWAY_GIT_REPO_NAME", "")
    repo = f"{owner}/{name}" if owner and name else ""
    return (
        repo,
        platform.get("RAILWAY_GIT_BRANCH", "main"),
        platform.get("RAILWAY_GIT_COMMIT_SHA", ""),
        platform.get("RAILWAY_ENVIRONMENT_ID", ""),
        platform.get("RAILWAY_PROJECT_NAME", ""),
        platform.get("RAILWAY_ENVIRONMENT_NAME", ""),
    )


def main() -> int:
    (
        default_repo,
        default_branch,
        live_sha,
        railway_environment_id,
        railway_project_name,
        railway_environment_name,
    ) = _defaults()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=default_repo, help="GitHub OWNER/NAME")
    parser.add_argument("--branch", default=default_branch)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--receipt-timeout", type=float, default=120.0)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create the redeploy commit and require a deployment receipt (default: dry-run)",
    )
    args = parser.parse_args()
    try:
        result = request_redeploy(
            repo=args.repo,
            branch=args.branch,
            live_sha=live_sha,
            railway_environment_id=railway_environment_id,
            railway_project_name=railway_project_name,
            railway_environment_name=railway_environment_name,
            reason=args.reason,
            execute=args.execute,
            receipt_timeout=args.receipt_timeout,
        )
    except DeploymentReceiptTimeout as exc:
        print(
            json.dumps(
                {
                    "status": "commit_created_no_deployment",
                    "commit_sha": exc.commit_sha,
                    "error": str(exc),
                }
            )
        )
        return 3
    except (RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
