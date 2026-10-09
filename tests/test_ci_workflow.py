"""Commit-policy CI checks contributor PR history, not Actions' merge commit.

PR runs select the event's head SHA; push runs fall back to github.sha.
The full history is required for the existing base..HEAD validation range.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1]
WORKFLOW = Path(".github/workflows/agent-workflow-check.yml")
TEMPLATE = KIT / "templates" / "project" / WORKFLOW
HEAD_REF = "${{ github.event.pull_request.head.sha || github.sha }}"


class CIWorkflowCheckoutTest(unittest.TestCase):
    def validation_job(self, text):
        # Bound the text contract to the validation job, excluding Windows'
        # separate checkout so an option added to the wrong job cannot pass.
        job = re.search(
            r"(?ms)^  agent-workflow-check:\n(?P<body>.*?)"
            r"(?=^  [A-Za-z][A-Za-z0-9_-]*:[ \t]*$|\Z)",
            text,
        )
        self.assertIsNotNone(job, "missing agent-workflow-check job")
        return job.group("body")

    def assert_head_checkout(self, text):
        checkout = re.search(
            r"(?m)^      - uses: actions/checkout@[^\n]+\n"
            r"(?P<body>(?:^        [^\n]*\n|^\n)*)",
            self.validation_job(text),
        )
        self.assertIsNotNone(checkout, "missing validation checkout step")
        body = checkout.group("body")
        self.assertRegex(body, r"(?m)^        with:[ \t]*$")
        self.assertEqual(
            re.findall(r"(?m)^          ref:[ \t]*(.*?)[ \t]*$", body),
            [HEAD_REF],
            "PR commit policy must use contributor head, with a push SHA fallback",
        )
        self.assertEqual(
            re.findall(r"(?m)^          fetch-depth:[ \t]*(.*?)[ \t]*$", body),
            ["0"],
            "range validation requires the complete base and head history",
        )
        self.assertIn("DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}", self.validation_job(text))

    def test_repository_validation_checks_out_pr_head_or_push_sha(self):
        self.assert_head_checkout((KIT / WORKFLOW).read_text(encoding="utf-8"))

    def test_distributed_template_checks_out_pr_head_or_push_sha(self):
        self.assert_head_checkout(TEMPLATE.read_text(encoding="utf-8"))

    def test_fresh_install_preserves_template_head_checkout_contract(self):
        with tempfile.TemporaryDirectory(prefix="awk-ci-workflow-") as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
            installed = subprocess.run(
                [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(root)],
                cwd=KIT, text=True, capture_output=True, timeout=120,
            )
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            workflow = root / WORKFLOW
            self.assertTrue(workflow.is_file(), "installer must distribute the CI workflow")
            self.assertEqual(workflow.read_bytes(), TEMPLATE.read_bytes())
            self.assert_head_checkout(workflow.read_text(encoding="utf-8"))

    def history(self, default_at_head=False, with_default=True, default_diverged=False):
        temporary = tempfile.TemporaryDirectory(prefix="awk-ci-history-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.git(root, "init", "-q")
        self.git(root, "config", "user.name", "CI Fixture")
        self.git(root, "config", "user.email", "ci-fixture@example.com")
        self.git(root, "commit", "-q", "--allow-empty", "-m", "chore(fixture): baseline")
        base = self.git(root, "rev-parse", "HEAD")
        self.git(root, "commit", "-q", "--allow-empty", "-m", "feat(fixture): branch change")
        head = self.git(root, "rev-parse", "HEAD")
        if with_default:
            default = head if default_at_head else base
            if default_diverged:
                tree = self.git(root, "rev-parse", f"{base}^{{tree}}")
                default = self.git(root, "commit-tree", tree, "-p", base, "-m", "feat(fixture): default branch advanced")
            self.git(root, "update-ref", "refs/remotes/origin/main", default)
        (root / "tools").mkdir()
        (root / "tools" / "agentctl.py").write_text(
            "import json, os, sys\n"
            "print(json.dumps(sys.argv[1:]))\n"
            "sys.exit(int(os.environ.get('CI_VALIDATOR_EXIT', '0')))\n",
            encoding="utf-8",
        )
        return root, base, head

    def git(self, root, *args):
        proc = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def run_validation(self, workflow, root, *, event="push", before="", pr_base="", exit_code=0):
        text = workflow.read_text(encoding="utf-8")
        job = self.validation_job(text)
        run = re.search(r"(?m)^        run: \|\n(?P<body>(?:^          [^\n]*\n|^\n)*)", job)
        self.assertIsNotNone(run, "missing commit validation script")
        script = textwrap.dedent(run.group("body"))
        for expression, value in (
            ("${{ github.event_name }}", event),
            ("${{ github.event.before }}", before),
            ("${{ github.event.pull_request.base.sha }}", pr_base),
        ):
            script = script.replace(expression, value)
        env = os.environ.copy()
        env.update(DEFAULT_BRANCH="main", CI_VALIDATOR_EXIT=str(exit_code))
        return subprocess.run(["bash", "-c", script], cwd=root, env=env, text=True, capture_output=True, timeout=60)

    def assert_range(self, proc, expected, exit_code=0):
        self.assertEqual(proc.returncode, exit_code, proc.stdout + proc.stderr)
        calls = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(calls, [["check", "--mode", "ci", "--commit-range", expected]])

    def test_valid_pr_and_existing_push_validate_base_to_checked_out_head(self):
        root, base, _ = self.history()
        for workflow in (KIT / WORKFLOW, TEMPLATE):
            for event in ("pull_request", "push"):
                with self.subTest(workflow=workflow, event=event):
                    proc = self.run_validation(workflow, root, event=event, before=base, pr_base=base)
                    self.assert_range(proc, f"{base}..HEAD")

    def test_new_branch_push_uses_available_default_branch_merge_base(self):
        root, base, _ = self.history(default_diverged=True)
        for workflow in (KIT / WORKFLOW, TEMPLATE):
            for before in ("0" * 40, "", "f" * 40):
                with self.subTest(workflow=workflow, before=before):
                    self.assert_range(self.run_validation(workflow, root, before=before), f"{base}..HEAD")

    def test_missing_or_head_default_branch_still_validates_reachable_head_commits(self):
        for default_at_head, with_default in ((True, True), (False, False)):
            root, _, _ = self.history(default_at_head=default_at_head, with_default=with_default)
            for workflow in (KIT / WORKFLOW, TEMPLATE):
                with self.subTest(workflow=workflow, default_at_head=default_at_head, with_default=with_default):
                    self.assert_range(self.run_validation(workflow, root, before="0" * 40), "HEAD")

    def test_commit_validator_failure_is_not_replaced_by_document_only_check(self):
        root, _, _ = self.history(with_default=False)
        for workflow in (KIT / WORKFLOW, TEMPLATE):
            with self.subTest(workflow=workflow):
                self.assert_range(self.run_validation(workflow, root, exit_code=2), "HEAD", exit_code=2)


if __name__ == "__main__":
    unittest.main()
