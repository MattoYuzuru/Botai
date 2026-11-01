"""Reuse a successful internal PR run only for an identical repository tree.

The immutable artifact name is the receipt; no PR-supplied archive is downloaded
or executed. GitHub supplies its run provenance and the individual job results.
Only ci-ok publishes the receipt, after all full checks have passed.
"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

FULL_JOBS = ("backend", "web", "web-e2e", "generated", "guards")
REQUIRED_JOBS = ("plan", *FULL_JOBS, "ci-ok")
RECEIPT_PREFIX = "ci-tree-v1"
TRUSTED_FILES = (".github/workflows/ci.yml", "scripts/ci_gate.sh")


def trusted_blobs():
    blobs = {}
    for name in TRUSTED_FILES:
        content = (Path(__file__).parents[1] / name).read_bytes()
        blobs[name] = hashlib.sha1(
            b"blob " + str(len(content)).encode() + b"\0" + content
        ).hexdigest()
    return blobs


def api(endpoint):
    result = subprocess.run(
        ["gh", "api", endpoint],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    return json.loads(result.stdout)


def receipt_name(tree, attempt):
    return f"{RECEIPT_PREFIX}-{tree}-{attempt}"


def successful_pr(run, repository):
    return (
        run.get("event") == "pull_request"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and (run.get("repository") or {}).get("full_name") == repository
        and (run.get("head_repository") or {}).get("full_name") == repository
    )


def find_reusable_run(repository, tree, request=api):
    prefix = f"repos/{repository}"
    deadline = time.monotonic() + 90
    # Bounded search: missing/older evidence simply causes a full CI run.
    runs = request(
        f"{prefix}/actions/workflows/ci.yml/runs?event=pull_request&status=success&per_page=30"
    )["workflow_runs"]
    for run in runs:
        if time.monotonic() >= deadline:
            break
        if not successful_pr(run, repository):
            continue
        run_id = run["id"]
        attempt = run["run_attempt"]
        response = request(f"{prefix}/actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = response["artifacts"]
        # Test code can upload artifacts too. The trusted gate must have
        # published exactly one receipt; reject extra/fabricated receipts.
        if response["total_count"] != len(artifacts):
            continue
        if (
            sum(
                artifact.get("name", "").startswith(RECEIPT_PREFIX)
                for artifact in artifacts
            )
            != 1
        ):
            continue
        receipts = [
            artifact
            for artifact in artifacts
            if artifact.get("name") == receipt_name(tree, attempt)
            and artifact.get("expired") is False
            and artifact.get("workflow_run", {}).get("id") == run_id
            and artifact.get("workflow_run", {}).get("head_sha") == run["head_sha"]
        ]
        if len(receipts) != 1:
            continue
        # A different PR workflow could fabricate a receipt for any tree.
        # Require the same publisher as main; PR plan executes inline git only.
        if any(
            request(f"{prefix}/contents/{name}?ref={run['head_sha']}").get("sha")
            != blob
            for name, blob in trusted_blobs().items()
        ):
            continue
        # A green workflow alone can contain skipped jobs. Require every check
        # from this pipeline, including the gate that published the receipt.
        jobs = request(
            f"{prefix}/actions/runs/{run_id}/jobs?filter=latest&per_page=100"
        )["jobs"]
        if not all(
            len(matching := [job for job in jobs if job.get("name") == name]) == 1
            and matching[0].get("status") == "completed"
            and matching[0].get("conclusion") == "success"
            for name in REQUIRED_JOBS
        ):
            continue
        # A rerun can start while the receipt/jobs are being read.
        current = request(f"{prefix}/actions/runs/{run_id}")
        if (
            successful_pr(current, repository)
            and current["run_attempt"] == attempt
            and current["head_sha"] == run["head_sha"]
        ):
            return run_id
    return None


def decide(event, ref, repository, tree, request=api):
    if event != "push" or ref != "refs/heads/main":
        return None, "Full CI: only a push to main can reuse PR checks."
    try:
        run_id = find_reusable_run(repository, tree, request)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None, "Full CI: GitHub evidence unavailable or invalid."
    if run_id is None:
        return None, "Full CI: no successful PR receipt for this exact tree."
    return run_id, f"Reusing successful PR CI run {run_id} for tree {tree}."


def main():
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], text=True
    ).strip()
    run_id, reason = decide(
        os.environ["GITHUB_EVENT_NAME"],
        os.environ["GITHUB_REF"],
        os.environ["GITHUB_REPOSITORY"],
        tree,
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"reuse={'true' if run_id else 'false'}\n")
        output.write(f"source_run={run_id or ''}\n")
    print(reason)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(f"{reason}\n\nRepository tree: `{tree}`.\n")
        if run_id:
            summary.write(
                f"\n[Source PR CI](https://github.com/{os.environ['GITHUB_REPOSITORY']}"
                f"/actions/runs/{run_id}). Full check jobs are intentionally skipped.\n"
            )


if __name__ == "__main__":
    main()
