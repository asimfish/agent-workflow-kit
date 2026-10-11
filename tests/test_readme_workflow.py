"""Replay the published bilingual entry commands in fresh installed projects."""

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests.test_task_contract import KIT, _ContractTestCase
from tools import agentctl


def readme_commands(name):
    commands = []
    text = (KIT / name).read_text(encoding="utf-8")
    for block in re.findall(r"(?ms)^```bash\n(.*?)^```", text):
        for line in block.replace("\\\n", " ").splitlines():
            words = shlex.split(line, comments=True)
            if words and words[0] == "agentctl":
                commands.append(words[1:])
    return commands


class ReadmeWorkflowTest(_ContractTestCase):
    def command(self, commands, *prefix, flag=None):
        matches = [args for args in commands if args[:len(prefix)] == list(prefix)
                   and (flag is None or flag in args)]
        self.assertEqual(len(matches), 1, (prefix, flag, matches))
        return matches[0]

    def replay(self, name):
        commands = readme_commands(name)
        work = self.command(commands, "work", "--agent", "codex", flag="--auto-create")
        scopes = work[work.index("--scope") + 1].split(",")
        self.assertEqual(scopes, ["src/data/", "tests/data/"],
                         "the example must authorize its regression test files")
        tests_cmd = work[work.index("--tests-cmd") + 1]
        self.assertEqual(tests_cmd, "python3 -m unittest discover -s tests/data -v")

        # A clean adoption commit supplies the baseline required by code worktrees.
        human = self.env(TERM_SESSION_ID="readme-adoption")
        for args in (
            ["add", *agentctl.ADOPTION_COMMIT_ADD_PATHS],
            ["commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE],
        ):
            proc = subprocess.run(["git", *args], cwd=self.root, env=human,
                                  text=True, capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.addCleanup(shutil.rmtree, Path(str(self.root) + "-worktrees"), ignore_errors=True)
        planning = self.root
        created = self.agentctl(*work, session="worker")
        path = next(line.strip().split("=", 1)[1] for line in created.stdout.splitlines()
                    if line.strip().startswith("path="))
        self.root = Path(path)
        self.assertNotEqual(self.root, planning)
        board = json.loads((self.root / ".agent/board.json").read_text(encoding="utf-8"))
        task = next(task for task, entry in board["tasks"].items()
                    if entry.get("title") == work[work.index("--title") + 1])
        self.assertNotEqual(task, "T-001", "README IDs are placeholders, not assigned IDs")
        self.agentctl("work", "--agent", "codex", "--task", task, session="worker")

        doc_before = self.doc(task)
        denied = self.agentctl("work", "--agent", "codex", "--task", task,
                               session="copied-history", expect=1)
        self.assertRegex(denied.stdout + denied.stderr, r"conflict|claimed|locked|takeover")
        self.assertEqual(self.doc(task), doc_before)

        source = self.root / "src/data/loader.py"
        source.parent.mkdir(parents=True)
        source.write_text("def split(n):\n    return list(range(n - 1))\n", encoding="utf-8")
        test = self.root / "tests/data/test_loader.py"
        test.parent.mkdir(parents=True)
        test.write_text(
            "import unittest\nfrom src.data.loader import split\n\n"
            "class SplitTest(unittest.TestCase):\n"
            "    def test_exact_records(self):\n"
            "        for n in (1, 7):\n"
            "            with self.subTest(n=n):\n"
            "                self.assertEqual(split(n), list(range(n)))\n",
            encoding="utf-8",
        )
        return commands, task, source

    def exercise(self, name):
        commands, task, source = self.replay(name)
        finishes = [args for args in commands if args[0] == "finish"]
        self.assertEqual(len(finishes), 2)
        worker_finish, reviewer_finish = finishes
        failed = self.agentctl(*worker_finish, session="worker", expect=1)
        self.assertIn("exited", failed.stderr)
        self.assertEqual(self.status(task), "in_progress")
        source.write_text("def split(n):\n    return list(range(n))\n", encoding="utf-8")
        self.agentctl(*self.command(commands, "note"), session="worker")
        self.agentctl(*worker_finish, session="worker")
        self.assertEqual(self.status(task), "review")
        self.assertIn("- Tests-exit: 0", self.completion(task))

        # Review the committed candidate in a separate clone, not the worker lease.
        for args in (
            ["add", ".agent", "src/data/loader.py", "tests/data/test_loader.py"],
            ["commit", "-q", "-m", f"fix(data): correct shard split\n\nRefs: {task}"],
        ):
            proc = subprocess.run(["git", *args], cwd=self.root, env=self.env("worker"),
                                  text=True, capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        candidate = self.git("rev-parse", "HEAD").stdout.strip()
        review = tempfile.TemporaryDirectory(prefix="awk-readme-review-")
        self.addCleanup(review.cleanup)
        cloned = subprocess.run(["git", "clone", "-q", "--no-local", str(self.root), review.name],
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(cloned.returncode, 0, cloned.stdout + cloned.stderr)
        self.root = Path(review.name)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), candidate)
        self.agentctl(*self.command(commands, "agents", "add"), session="reviewer")
        reviewer_work = self.command(commands, "work", "--agent", "reviewer")
        reviewer_work = [task if arg == "T-001" else arg.replace("T-001", task)
                         for arg in reviewer_work]
        self.assertEqual(reviewer_work[reviewer_work.index("--scope") + 1], ".agent/gates/")
        self.agentctl(*reviewer_work, session="reviewer")
        board = json.loads((self.root / ".agent/board.json").read_text(encoding="utf-8"))
        review_task = next(task_id for task_id, entry in board["tasks"].items()
                           if entry.get("title") == f"review {task}")
        approve = [arg.replace("T-001", task)
                   for arg in self.command(commands, "gate", "approve")]
        self.agentctl(*approve, session="reviewer")
        self.assertEqual(self.status(task), "done")
        gate = (self.root / ".agent/gates" / f"{task}.md").read_text(encoding="utf-8")
        self.assertIn("- Tests rerun: passed", gate)
        self.agentctl(*[arg.replace("T-001", task) for arg in reviewer_finish], session="reviewer")
        self.assertEqual(self.status(review_task), "done", "the review must close without another review")
        self.agentctl("check", "--mode", "manual", session="reviewer")

    def test_english_walkthrough(self):
        self.exercise("README.md")

    def test_chinese_walkthrough(self):
        self.exercise("README_CN.md")

    def test_both_languages_publish_the_same_commands_and_windows_runs_them(self):
        self.assertEqual(readme_commands("README.md"), readme_commands("README_CN.md"))
        workflow = (KIT / ".github/workflows/agent-workflow-check.yml").read_text(encoding="utf-8")
        windows = workflow.split("  windows-dispatch-check:\n", 1)[1]
        self.assertIn("python -m unittest tests.test_readme_workflow -v", windows)
