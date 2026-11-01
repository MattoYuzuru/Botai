"""Behavioral checks for the main/PR CI handoff; no GitHub writes or secrets."""

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "ci_reuse.py"
SPEC = importlib.util.spec_from_file_location("ci_reuse", SCRIPT)
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)

REPOSITORY = "owner/repo"
TREE = "a" * 40


class GitHub:
    def __init__(self):
        self.run = {
            "id": 42,
            "run_attempt": 1,
            "head_sha": "b" * 40,
            "event": "pull_request",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": REPOSITORY},
            "head_repository": {"full_name": REPOSITORY},
        }
        self.artifact = {
            "name": ci.receipt_name(TREE, 1),
            "expired": False,
            "workflow_run": {"id": 42, "head_sha": "b" * 40},
        }
        self.jobs = [
            {"name": name, "status": "completed", "conclusion": "success"}
            for name in ci.REQUIRED_JOBS
        ]
        self.current = None
        self.blobs = ci.trusted_blobs()
        self.extra_artifacts = []
        self.calls = []

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if "/workflows/ci.yml/runs?" in endpoint:
            return {"workflow_runs": [self.run]}
        if "/artifacts?" in endpoint:
            artifacts = [self.artifact, *self.extra_artifacts]
            return {"artifacts": artifacts, "total_count": len(artifacts)}
        if "/contents/" in endpoint:
            name = endpoint.split("/contents/", 1)[1].split("?", 1)[0]
            return {"sha": self.blobs[name]}
        if "/jobs?" in endpoint:
            return {"jobs": self.jobs}
        if endpoint.endswith("/runs/42"):
            return self.current if self.current is not None else self.run
        raise AssertionError(endpoint)


class ReuseTests(unittest.TestCase):
    def decide(self, github):
        return ci.decide("push", "refs/heads/main", REPOSITORY, TREE, github)[0]

    def test_same_tree_can_reuse_despite_different_commit_sha(self):
        github = GitHub()
        self.assertNotEqual(github.run["head_sha"], TREE)
        self.assertEqual(self.decide(github), 42)
        self.assertEqual(len(github.calls), 6)

    def test_fabricated_receipts_and_modified_publishers_are_rejected(self):
        github = GitHub()
        github.extra_artifacts = [{"name": ci.receipt_name("c" * 40, 1)}]
        self.assertIsNone(self.decide(github))
        for name in ci.TRUSTED_FILES:
            with self.subTest(name=name):
                github = GitHub()
                github.blobs[name] = "c" * 40
                self.assertIsNone(self.decide(github))

    def test_pr_and_other_branches_always_run_full_ci_without_api(self):
        for event, ref in [
            ("pull_request", "refs/pull/1/merge"),
            ("push", "refs/heads/dev"),
        ]:
            with self.subTest(event=event):
                github = GitHub()
                self.assertIsNone(ci.decide(event, ref, REPOSITORY, TREE, github)[0])
                self.assertEqual(github.calls, [])

    def test_changed_tree_missing_expired_or_wrong_attempt_receipt_runs_full_ci(self):
        for change in [
            {"name": ci.receipt_name("c" * 40, 1)},
            {"name": "legacy-receipt"},
            {"expired": True},
            {"name": ci.receipt_name(TREE, 2)},
            {"workflow_run": {"id": 999, "head_sha": "b" * 40}},
            {"workflow_run": {"id": 42, "head_sha": "c" * 40}},
        ]:
            with self.subTest(change=change):
                github = GitHub()
                github.artifact.update(change)
                self.assertIsNone(self.decide(github))

    def test_ineligible_runs_cannot_supply_evidence(self):
        for change in [
            {"event": "push"},
            {"status": "in_progress"},
            {"conclusion": "failure"},
            {"conclusion": "cancelled"},
            {"repository": {"full_name": "elsewhere/repo"}},
            {"head_repository": {"full_name": "fork/repo"}},
            {"head_repository": None},
        ]:
            with self.subTest(change=change):
                github = GitHub()
                github.run.update(change)
                self.assertIsNone(self.decide(github))
                self.assertEqual(len(github.calls), 1)

    def test_green_run_with_skipped_failed_cancelled_or_missing_check_is_rejected(self):
        for name in ci.REQUIRED_JOBS:
            for conclusion in [
                "skipped",
                "failure",
                "cancelled",
                "timed_out",
                None,
                "missing",
            ]:
                with self.subTest(name=name, conclusion=conclusion):
                    github = GitHub()
                    if conclusion == "missing":
                        github.jobs = [
                            job for job in github.jobs if job["name"] != name
                        ]
                    else:
                        next(job for job in github.jobs if job["name"] == name)[
                            "conclusion"
                        ] = conclusion
                    self.assertIsNone(self.decide(github))

    def test_duplicate_or_in_progress_job_is_rejected(self):
        github = GitHub()
        github.jobs.append(copy.deepcopy(github.jobs[0]))
        self.assertIsNone(self.decide(github))
        github = GitHub()
        github.jobs[0]["status"] = "in_progress"
        self.assertIsNone(self.decide(github))

    def test_rerun_race_is_rejected(self):
        for change in [
            {"run_attempt": 2},
            {"status": "in_progress"},
            {"conclusion": "failure"},
        ]:
            with self.subTest(change=change):
                github = GitHub()
                github.current = {**github.run, **change}
                self.assertIsNone(self.decide(github))

    def test_api_error_or_malformed_evidence_falls_back_to_full_ci(self):
        for error in [
            subprocess.CalledProcessError(1, "gh"),
            subprocess.TimeoutExpired("gh", 15),
            OSError("unavailable"),
            json.JSONDecodeError("bad JSON", "", 0),
            KeyError("workflow_runs"),
            TypeError("bad metadata"),
        ]:
            with self.subTest(error=type(error).__name__):

                def unavailable(_, error=error):
                    raise error

                self.assertIsNone(self.decide(unavailable))

    def test_no_runs_and_time_budget_fall_back(self):
        self.assertIsNone(self.decide(lambda _: {"workflow_runs": []}))
        github = GitHub()
        with patch.object(ci.time, "monotonic", side_effect=[0, 100]):
            self.assertIsNone(self.decide(github))
        self.assertEqual(len(github.calls), 1)

    def test_git_commits_with_identical_files_reuse_but_changed_files_do_not(self):
        with tempfile.TemporaryDirectory() as temporary:

            def git(*arguments):
                return subprocess.check_output(
                    ["git", "-C", temporary, *arguments], text=True
                ).strip()

            git("init", "-q")
            git("config", "user.name", "CI test")
            git("config", "user.email", "ci@example.invalid")
            source = Path(temporary) / "source.txt"
            source.write_text("tested\n")
            git("add", "source.txt")
            git("commit", "-qm", "Tested snapshot")
            tested_sha = git("rev-parse", "HEAD")
            tested_tree = git("rev-parse", "HEAD^{tree}")
            git("commit", "--allow-empty", "-qm", "Different commit metadata")
            self.assertNotEqual(git("rev-parse", "HEAD"), tested_sha)
            self.assertEqual(git("rev-parse", "HEAD^{tree}"), tested_tree)
            github = GitHub()
            github.artifact["name"] = ci.receipt_name(tested_tree, 1)
            self.assertEqual(
                ci.decide("push", "refs/heads/main", REPOSITORY, tested_tree, github)[
                    0
                ],
                42,
            )
            source.write_text("changed\n")
            git("add", "source.txt")
            git("commit", "-qm", "Untested change")
            changed_tree = git("rev-parse", "HEAD^{tree}")
            self.assertNotEqual(changed_tree, tested_tree)
            self.assertIsNone(
                ci.decide("push", "refs/heads/main", REPOSITORY, changed_tree, github)[
                    0
                ]
            )

    def test_cli_outputs_and_summary_use_actual_checked_out_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF": "refs/heads/main",
                "GITHUB_REPOSITORY": REPOSITORY,
                "GITHUB_OUTPUT": str(root / "output"),
                "GITHUB_STEP_SUMMARY": str(root / "summary"),
            }
            for run_id in [None, 42]:
                with (
                    self.subTest(run_id=run_id),
                    patch.dict(os.environ, environment),
                    patch.object(
                        ci.subprocess, "check_output", return_value=TREE + "\n"
                    ),
                    patch.object(
                        ci, "decide", return_value=(run_id, "Decision")
                    ) as decide,
                ):
                    (root / "output").write_text("")
                    ci.main()
                    decide.assert_called_once_with(
                        "push", "refs/heads/main", REPOSITORY, TREE
                    )
                    self.assertIn(
                        f"reuse={'true' if run_id else 'false'}\n",
                        (root / "output").read_text(),
                    )
                    self.assertIn(TREE, (root / "summary").read_text())


class GateTests(unittest.TestCase):
    def gate(self, reuse, results, plan="success"):
        return subprocess.run(
            ["bash", str(SCRIPT.with_name("ci_gate.sh"))],
            check=False,
            env={
                **os.environ,
                "PLAN_RESULT": plan,
                "REUSE": reuse,
                "RESULTS": " ".join(results),
            },
            capture_output=True,
        ).returncode

    def test_full_success_and_verified_skip_are_accepted(self):
        self.assertEqual(self.gate("false", ["success"] * 5), 0)
        self.assertEqual(self.gate("true", ["skipped"] * 5), 0)

    def test_every_incomplete_required_job_blocks_the_full_gate(self):
        for index in range(5):
            for result in ["failure", "skipped", "cancelled", ""]:
                with self.subTest(index=index, result=result):
                    results = ["success"] * 5
                    results[index] = result
                    self.assertNotEqual(self.gate("false", results), 0)

    def test_reuse_requires_skips_and_successful_plan(self):
        self.assertNotEqual(self.gate("true", ["success"] * 5), 0)
        for plan in ["failure", "skipped", "cancelled", ""]:
            with self.subTest(plan=plan):
                self.assertNotEqual(self.gate("true", ["skipped"] * 5, plan), 0)
                self.assertNotEqual(self.gate("false", ["success"] * 5, plan), 0)

    def test_missing_decision_or_results_cannot_pass(self):
        self.assertNotEqual(self.gate("", ["success"] * 5), 0)
        self.assertNotEqual(self.gate("true", []), 0)
        self.assertNotEqual(self.gate("false", ["success"] * 4), 0)


if __name__ == "__main__":
    unittest.main()
