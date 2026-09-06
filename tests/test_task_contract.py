"""The task contract is what a reviewer judges the work against.

A proof is checked against its statement, never against the prover's own
account of it. The kit's analogue: the Definition of Done says what "done"
means before the reviewer looks, and the tests command is the one check the
tool itself runs -- at `finish` on the worker's side and again, on request,
at `gate approve` on the reviewer's side -- instead of a sentence the worker
typed. These tests pin the rules:

- `finish` refuses while the Definition of Done is empty; a review task's
  recorded gate decision stands in for one.
- `--goal`, `--done`, `--tests-cmd` are accepted at creation (shared checkout
  and worktree bootstrap alike), by `agentctl contract`, and by `finish`.
- `finish` executes the tests command, refuses on a non-zero exit or a
  timeout, and records command, exit, and duration.
- `gate approve --rerun-tests` re-executes the recorded command and refuses
  approval when it fails; the gate record says what was checked.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import agentctl

KIT = Path(__file__).resolve().parents[1]
IDENTITY_ENV = (
    "CODEX_THREAD_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CURSOR_CONVERSATION_ID",
    "WHALENT_AGENT_ID",
    "WHALENT_CODEX_INSTANCE_ID",
    "WHALENT_COMPOSER_ID",
    "WHALENT_FORK_SOURCE_AGENT_ID",
    "AGENT_SESSION_ID",
    "TERM_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_ID",
    "AGENT_WORKFLOW_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_OWNER_RUNTIME",
    "AGENT_WORKFLOW_SESSION_INSTANCE_ID",
    "AGENT_WORKFLOW_PARENT_SESSION_KEY",
    "AGENT_WORKFLOW_SESSION_ISOLATION_ERROR",
)


class _ContractTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-contract-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.email", "agent@example.com")
        self.git("config", "user.name", "Agent Test")
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def git(self, *args, check=True):
        proc = subprocess.run(
            ["git", *args], cwd=str(self.root), text=True, capture_output=True, timeout=60,
        )
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def env(self, session=None, **extra):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_RESOURCE_LOCK_DIR"] = str(self.root / ".resource-locks")
        if session:
            env["AGENT_WORKFLOW_SESSION_ID"] = session
            env["CODEX_THREAD_ID"] = f"thread-{session}"
        env.update(extra)
        return env

    def agentctl(self, *args, expect=0, session=None, **extra):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(self.root), env=self.env(session, **extra), text=True,
            capture_output=True, timeout=180,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        return proc

    def open_task(self, session, title, scope, *extra):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--title", title,
            "--scope", scope, *extra, session=session,
        )
        board = json.loads((self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        return next(t for t, e in board["tasks"].items() if e.get("title") == title)

    def doc(self, task):
        return (self.root / ".agent" / "tasks" / f"{task}.md").read_text(encoding="utf-8")

    def completion(self, task):
        return agentctl._extract_section(self.doc(task), "## Completion Record")

    def status(self, task):
        board = json.loads((self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        return board["tasks"][task]["status"]

    def register_reviewer(self, session, name="reviewer"):
        self.agentctl("agents", "add", "--id", name, "--role", "review", session=session)
        return name


class DefinitionOfDoneTest(_ContractTestCase):
    def test_finish_refuses_without_a_definition_of_done(self):
        task = self.open_task("worker", "fix the loader", "src/data/")
        self.assertEqual(agentctl._task_contract(self.root, task)["done"], "")
        refused = self.agentctl(
            "finish", "--summary", "did it", "--tests", "pytest: 3 passed",
            expect=1, session="worker",
        )
        self.assertIn("has no Definition of Done", refused.stderr)
        self.assertIn("agentctl contract --done", refused.stderr)
        self.assertEqual(self.status(task), "in_progress")

        # Supplying it at finish is the last chance, and it lands in the document.
        finished = self.agentctl(
            "finish", "--summary", "did it", "--tests", "pytest: 3 passed",
            "--done", "shard split is exact for every n", session="worker",
        )
        self.assertIn("-> review", finished.stdout)
        self.assertEqual(agentctl._task_contract(self.root, task)["done"], "shard split is exact for every n")
        self.assertEqual(self.status(task), "review")

    def test_contract_fields_land_at_creation_and_can_be_changed(self):
        task = self.open_task(
            "worker", "fix the loader", "src/data/",
            "--goal", "loader must not drop the last shard",
            "--done", "pytest tests/data passes; n=7 case covered",
            "--tests-cmd", "python3 -c 'print(42)'",
        )
        contract = agentctl._task_contract(self.root, task)
        self.assertEqual(contract["goal"], "loader must not drop the last shard")
        self.assertEqual(contract["done"], "pytest tests/data passes; n=7 case covered")
        self.assertEqual(contract["tests_cmd"], "python3 -c 'print(42)'")
        body = self.doc(task)
        self.assertIn("- Goal: loader must not drop the last shard", body)
        self.assertIn("- Tests command: python3 -c 'print(42)'\n", body)

        shown = json.loads(self.agentctl("contract", "--json", session="worker").stdout)
        self.assertEqual(shown["task"], task)
        self.assertEqual(shown["done"], "pytest tests/data passes; n=7 case covered")

        self.agentctl("contract", "--done", "also handles n=1", session="worker")
        self.assertEqual(agentctl._task_contract(self.root, task)["done"], "also handles n=1")
        # The tool wrote the document on the agent's behalf: no receipt refresh needed.
        self.agentctl("note", "still working", session="worker")

    def test_a_review_task_is_done_by_its_recorded_decision(self):
        worker_task = self.open_task("worker", "fix the loader", "src/data/")
        self.agentctl(
            "finish", "--summary", "done", "--tests", "unit", "--done", "loader fixed",
            session="worker",
        )
        reviewer = self.register_reviewer("reviewer")
        self.agentctl(
            "work", "--agent", reviewer, "--auto-create", "--type", "review",
            "--title", f"review {worker_task}", "--scope", ".agent/", session="reviewer",
        )
        self.agentctl("refresh", session="reviewer")
        self.agentctl(
            "gate", "approve", "--task", worker_task, "--by", reviewer, "--note", "ok",
            session="reviewer",
        )
        # No --done anywhere: the decision on record is what a review delivers.
        finished = self.agentctl("finish", "--summary", "approved", session="reviewer")
        self.assertIn("-> done", finished.stdout)

    def test_a_review_task_without_a_decision_still_needs_a_definition(self):
        self.register_reviewer("reviewer")
        task = self.agentctl(
            "work", "--agent", "reviewer", "--auto-create", "--type", "review",
            "--title", "review nothing yet", "--scope", ".agent/", session="reviewer",
        )
        refused = self.agentctl("finish", "--summary", "nothing decided", expect=1, session="reviewer")
        self.assertIn("has no Definition of Done", refused.stderr)


class TestsCommandAtFinishTest(_ContractTestCase):
    def test_finish_runs_the_command_and_records_the_result(self):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        finished = self.agentctl(
            "finish", "--summary", "did it",
            "--tests-cmd", "python3 -c \"print('11 passed in 0.2s')\"",
            session="worker",
        )
        self.assertIn("running tests command", finished.stdout)
        self.assertIn("exited 0", finished.stdout)
        record = self.completion(task)
        self.assertIn("- Tests-command: python3 -c \"print('11 passed in 0.2s')\"", record)
        self.assertIn("- Tests-exit: 0", record)
        self.assertRegex(record, r"- Tests-duration: \d+\.\d+s")
        # No prose given: the record carries the command's last line, not "not recorded".
        self.assertIn("- Tests: 11 passed in 0.2s", record)
        # The command given at finish is now part of the contract too.
        self.assertEqual(
            agentctl._task_contract(self.root, task)["tests_cmd"],
            "python3 -c \"print('11 passed in 0.2s')\"",
        )

    def test_finish_uses_the_contracts_command_when_none_is_given(self):
        marker = self.root / "ran.txt"
        task = self.open_task(
            "worker", "fix the loader", "src/data/", "--done", "loader fixed",
            "--tests-cmd", f"python3 -c \"open({str(marker)!r}, 'w').write('yes')\"",
        )
        self.agentctl("finish", "--summary", "did it", session="worker")
        self.assertEqual(marker.read_text(encoding="utf-8"), "yes")
        self.assertIn("- Tests-exit: 0", self.completion(task))

    def test_a_failing_command_refuses_finish_and_shows_the_tail(self):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        refused = self.agentctl(
            "finish", "--summary", "did it",
            "--tests-cmd", "python3 -c \"print('FAILED tests/x.py::t - boom'); raise SystemExit(1)\"",
            expect=1, session="worker",
        )
        self.assertIn("FAILED tests/x.py::t - boom", refused.stderr)
        self.assertIn("exited 1", refused.stderr)
        self.assertIn("finish refused; the tests command must exit 0", refused.stderr)
        self.assertEqual(self.status(task), "in_progress")
        self.assertNotIn("Completed-at:", self.completion(task))

    def test_a_hanging_command_is_a_refusal_not_a_hang(self):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        refused = self.agentctl(
            "finish", "--summary", "did it",
            "--tests-cmd", "python3 -c \"import time; print('starting'); time.sleep(30)\"",
            expect=1, session="worker", AGENT_WORKFLOW_TESTS_TIMEOUT="2",
        )
        self.assertIn("timed out after 2s", refused.stderr)
        self.assertIn(agentctl.TESTS_TIMEOUT_ENV, refused.stderr)
        self.assertEqual(self.status(task), "in_progress")

    def test_without_a_command_the_record_says_so(self):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        self.agentctl("finish", "--summary", "did it", session="worker")
        record = self.completion(task)
        self.assertIn("- Tests: not recorded", record)
        self.assertNotIn("Tests-command", record)

    def test_executed_command_counts_as_verification_evidence(self):
        body = (
            "# T-1 - x\n\nStatus: review\n\n## Completion Record\n\n- Summary: s\n"
            "- Tests: not recorded\n- Tests-command: pytest -q\n- Tests-exit: 0\n"
            "- Completed-at: 2026-09-06 00:00:00\n- Completed-at-ns: 5\n"
        )
        problems = agentctl._completion_evidence_problems(
            body, task="T-1", status="review", not_before_ns=1,
        )
        self.assertNotIn("task verification evidence is missing", problems)
        failed = body.replace("- Tests-exit: 0", "- Tests-exit: 1")
        problems = agentctl._completion_evidence_problems(
            failed, task="T-1", status="review", not_before_ns=1,
        )
        self.assertIn("task verification evidence is missing", problems)


class RerunAtGateTest(_ContractTestCase):
    def finished_task(self, command):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        self.agentctl("finish", "--summary", "did it", "--tests-cmd", command, session="worker")
        reviewer = self.register_reviewer("reviewer")
        self.agentctl(
            "work", "--agent", reviewer, "--auto-create", "--type", "review",
            "--title", f"review {task}", "--scope", ".agent/", session="reviewer",
        )
        self.agentctl("refresh", session="reviewer")
        return task, reviewer

    def test_rerun_passes_and_is_recorded(self):
        task, reviewer = self.finished_task("python3 -c \"print('ok')\"")
        approved = self.agentctl(
            "gate", "approve", "--task", task, "--by", reviewer, "--rerun-tests",
            "--note", "lgtm", session="reviewer",
        )
        self.assertIn("rerunning the tests command here", approved.stdout)
        self.assertIn("approved -> done", approved.stdout)
        gate = (self.root / ".agent" / "gates" / f"{task}.md").read_text(encoding="utf-8")
        self.assertIn("- Definition of Done: loader fixed", gate)
        self.assertIn("- Tests command: python3 -c \"print('ok')\"", gate)
        self.assertRegex(gate, r"- Tests rerun: passed \(exit 0 in \d+\.\d+s, runtime host-runtime:")

    def test_rerun_failure_refuses_approval(self):
        # Passes for the worker, fails for the reviewer: the command depends on
        # a file that exists only while the worker finishes.
        flag = self.root / "worker-only.txt"
        flag.write_text("here\n", encoding="utf-8")
        command = f"python3 -c \"import sys, os; sys.exit(0 if os.path.exists({str(flag)!r}) else 3)\""
        task, reviewer = self.finished_task(command)
        flag.unlink()
        refused = self.agentctl(
            "gate", "approve", "--task", task, "--by", reviewer, "--rerun-tests",
            "--note", "lgtm", expect=1, session="reviewer",
        )
        self.assertIn("exited 3", refused.stderr)
        self.assertIn("approval refused", refused.stderr)
        self.assertEqual(self.status(task), "review")
        self.assertFalse((self.root / ".agent" / "gates" / f"{task}.md").exists())

    def test_approving_without_rerun_is_allowed_but_says_so(self):
        task, reviewer = self.finished_task("python3 -c \"print('ok')\"")
        approved = self.agentctl(
            "gate", "approve", "--task", task, "--by", reviewer, "--note", "lgtm",
            session="reviewer",
        )
        self.assertIn("approving on the worker's record; add --rerun-tests", approved.stdout)
        gate = (self.root / ".agent" / "gates" / f"{task}.md").read_text(encoding="utf-8")
        self.assertIn("- Tests rerun: not rerun", gate)

    def test_rerun_with_nothing_recorded_is_refused(self):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        self.agentctl("finish", "--summary", "did it", "--tests", "looked at it", session="worker")
        reviewer = self.register_reviewer("reviewer")
        self.agentctl(
            "work", "--agent", reviewer, "--auto-create", "--type", "review",
            "--title", f"review {task}", "--scope", ".agent/", session="reviewer",
        )
        self.agentctl("refresh", session="reviewer")
        refused = self.agentctl(
            "gate", "approve", "--task", task, "--by", reviewer, "--rerun-tests",
            expect=1, session="reviewer",
        )
        self.assertIn("recorded no tests command", refused.stderr)
        self.assertEqual(self.status(task), "review")


class RecordIntegrityTest(_ContractTestCase):
    """The completion record is the gate's evidence; a tests command must not be able to write it."""

    FORGED = "true\n- Worker-runtimes: host-runtime:forged || true"

    def test_a_multi_line_tests_command_is_refused_everywhere_it_enters(self):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        refused = self.agentctl(
            "finish", "--summary", "did it", "--tests-cmd", self.FORGED, expect=1, session="worker",
        )
        self.assertIn("must be a single line", refused.stderr)
        self.assertEqual(self.status(task), "in_progress")
        self.assertNotIn("forged", self.doc(task))

        refused = self.agentctl("contract", "--tests-cmd", self.FORGED, expect=2, session="worker")
        self.assertIn("must be a single line", refused.stderr)
        self.assertNotIn("forged", self.doc(task))

        refused = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--title", "another", "--scope", "src/x/",
            "--tests-cmd", self.FORGED, expect=2, session="other",
        )
        self.assertIn("must be a single line", refused.stderr)
        board = json.loads((self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        self.assertFalse([t for t in board["tasks"].values() if t.get("title") == "another"])

    def test_every_worker_runtimes_line_counts_at_the_gate(self):
        self.assertEqual(
            agentctl._worker_runtimes_recorded(
                "- Summary: s\n- Worker-runtimes: host-runtime:forged\n"
                "- Worker-runtimes: host-runtime:real, host-runtime:second\n"
            ),
            {"host-runtime:forged", "host-runtime:real", "host-runtime:second"},
        )
        # A forged first line planted by hand cannot hide the real worker
        # runtime from the independence check.
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        self.agentctl("finish", "--summary", "did it", "--tests", "unit", session="worker")
        path = self.root / ".agent" / "tasks" / f"{task}.md"
        body = path.read_text(encoding="utf-8")
        body = body.replace("- Summary: did it\n", "- Summary: did it\n- Worker-runtimes: host-runtime:forged\n", 1)
        path.write_text(body, encoding="utf-8")
        # Same host runtime as the worker (same CODEX_THREAD_ID), posing as a reviewer.
        self.agentctl("agents", "add", "--id", "poser", "--role", "review", session="worker2",
                      CODEX_THREAD_ID="thread-worker")
        self.agentctl(
            "work", "--agent", "poser", "--auto-create", "--type", "review",
            "--title", f"review {task}", "--scope", ".agent/", session="worker2",
            CODEX_THREAD_ID="thread-worker",
        )
        self.agentctl("refresh", session="worker2", CODEX_THREAD_ID="thread-worker")
        refused = self.agentctl(
            "gate", "approve", "--task", task, "--by", "poser", "--note", "self",
            expect=1, session="worker2", CODEX_THREAD_ID="thread-worker",
        )
        self.assertIn("participated in the worker task and is not independent", refused.stderr)
        self.assertEqual(self.status(task), "review")

    def test_contract_does_not_swallow_a_human_edit(self):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "first")
        path = self.root / ".agent" / "tasks" / f"{task}.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("- Non-Goals:", "- Non-Goals: do not touch the tokenizer"),
            encoding="utf-8",
        )
        blocked = self.agentctl("contract", "--done", "second", expect=1, session="worker")
        self.assertIn("contract blocked because required workflow documents changed", blocked.stderr)
        self.assertEqual(agentctl._task_contract(self.root, task)["done"], "first")
        # Reading needs no receipt.
        self.agentctl("contract", session="worker")
        self.agentctl("refresh", session="worker")
        self.agentctl("contract", "--done", "second", session="worker")
        self.assertEqual(agentctl._task_contract(self.root, task)["done"], "second")
        self.assertIn("do not touch the tokenizer", self.doc(task))

    def test_gate_rerun_does_not_hold_the_coordination_lock(self):
        # The rerun itself runs another session's ledger command in the same
        # repository. Were the lock held during the rerun, that command would
        # time out on it, exit non-zero, and the approval would be refused.
        # (Registering the reviewer changes agents.json, which every session
        # must re-read; do it before the probe session opens.)
        reviewer = self.register_reviewer("reviewer")
        self.open_task("other", "unrelated work", "src/other/", "--done", "whatever")
        probe = (
            f"env -u CURSOR_CONVERSATION_ID AGENT_WORKFLOW_SESSION_ID=other CODEX_THREAD_ID=thread-other "
            f"{sys.executable} tools/agentctl.py note 'written while the reviewer reruns'"
        )
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        self.agentctl("finish", "--summary", "did it", "--tests-cmd", probe, session="worker")
        self.agentctl(
            "work", "--agent", reviewer, "--auto-create", "--type", "review",
            "--title", f"review {task}", "--scope", ".agent/", session="reviewer",
        )
        self.agentctl("refresh", session="reviewer")
        approved = self.agentctl(
            "gate", "approve", "--task", task, "--by", reviewer, "--rerun-tests", "--note", "ok",
            session="reviewer",
        )
        self.assertIn("approved -> done", approved.stdout)
        self.assertIn("written while the reviewer reruns", (self.root / ".agent" / "logs" / "progress.md").read_text(encoding="utf-8"))

    def test_backticks_and_quotes_survive_the_round_trip(self):
        command = "test \"`echo 3`\" = \"3\" && test '$(echo x)' = '$(echo x)'"
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed", "--tests-cmd", command)
        self.assertEqual(agentctl._task_contract(self.root, task)["tests_cmd"], command)
        self.assertIn(f"- Tests command: {command}\n", self.doc(task))
        self.agentctl("finish", "--summary", "did it", session="worker")
        self.assertIn(f"- Tests-command: {command}\n", self.completion(task))
        reviewer = self.register_reviewer("reviewer")
        self.agentctl(
            "work", "--agent", reviewer, "--auto-create", "--type", "review",
            "--title", f"review {task}", "--scope", ".agent/", session="reviewer",
        )
        self.agentctl("refresh", session="reviewer")
        approved = self.agentctl(
            "gate", "approve", "--task", task, "--by", reviewer, "--rerun-tests", session="reviewer",
        )
        self.assertIn(f"rerunning the tests command here: {command}", approved.stdout)

    def test_timeout_kills_the_whole_command_not_just_the_shell(self):
        task = self.open_task("worker", "fix the loader", "src/data/", "--done", "loader fixed")
        marker = f"awk-contract-{os.getpid()}"
        refused = self.agentctl(
            "finish", "--summary", "did it",
            "--tests-cmd", f"sleep 45; echo {marker}",
            expect=1, session="worker", AGENT_WORKFLOW_TESTS_TIMEOUT="2",
        )
        self.assertIn("timed out after 2s", refused.stderr)
        alive = subprocess.run(["pgrep", "-f", "sleep 45"], text=True, capture_output=True).stdout.split()
        self.assertEqual(alive, [], f"grandchild survived the timeout: {alive}")
        self.assertEqual(self.status(task), "in_progress")

    def test_an_indented_definition_of_done_is_not_empty(self):
        body = (
            "# T\n\n## Task Contract\n\n- Goal: g\n- Definition of Done:\n"
            "  - shard split exact for every n\n  - pytest tests/data passes\n"
            "- Expected Deliverables: x\n\n## Verification\n\n- Tests command:\n"
        )
        self.assertEqual(
            agentctl._task_doc_field(body, "## Task Contract", "Definition of Done"),
            "shard split exact for every n pytest tests/data passes",
        )
        self.assertEqual(agentctl._task_doc_field(body, "## Task Contract", "Expected Deliverables"), "x")
        self.assertEqual(agentctl._task_doc_field(body, "## Verification", "Tests command"), "")


class WorktreeBootstrapCarriesTheContractTest(_ContractTestCase):
    def test_code_task_in_a_worktree_gets_the_fields(self):
        # Worktree tasks need a baseline commit; the adoption commit is it.
        human = self.env(TERM_SESSION_ID="w0t0p0")
        subprocess.run(
            ["git", "add", *agentctl.ADOPTION_COMMIT_ADD_PATHS], cwd=str(self.root),
            env=human, check=True, capture_output=True,
        )
        committed = subprocess.run(
            ["git", "commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE], cwd=str(self.root),
            env=human, text=True, capture_output=True,
        )
        self.assertEqual(committed.returncode, 0, committed.stdout + committed.stderr)
        created = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--type", "code",
            "--title", "speed up the tokenizer", "--scope", "src/tok/",
            "--goal", "tokenizer twice as fast", "--done", "bench < 2s; pytest tests/tok passes",
            "--tests-cmd", "python3 -c 'print(1)'", session="worker",
        )
        path_line = next(ln for ln in created.stdout.splitlines() if ln.strip().startswith("path="))
        worktree = Path(path_line.split("=", 1)[1].strip())
        # The kit puts worktrees in a `<repo>-worktrees` sibling; remove the whole sibling.
        self.addCleanup(shutil.rmtree, worktree.parent, ignore_errors=True)
        docs = list((worktree / ".agent" / "tasks").glob("T*.md"))
        docs = [d for d in docs if d.name != "_template.md" and "tokenizer" in d.read_text(encoding="utf-8")]
        self.assertEqual(len(docs), 1, docs)
        body = docs[0].read_text(encoding="utf-8")
        self.assertIn("- Goal: tokenizer twice as fast", body)
        self.assertIn("- Definition of Done: bench < 2s; pytest tests/tok passes", body)
        self.assertIn("- Tests command: python3 -c 'print(1)'\n", body)


class FieldEditingTest(unittest.TestCase):
    def test_set_field_replaces_adds_or_creates_the_section(self):
        body = "# T\n\n## Task Contract\n\n- Goal:\n- Definition of Done:\n\n## Verification\n\n- Commands to run:\n  - `x`\n"
        out = agentctl._set_task_doc_field(body, "## Task Contract", "Definition of Done", "all  green\n now")
        self.assertIn("- Definition of Done: all  green now\n", out)
        self.assertEqual(out.count("Definition of Done"), 1)
        # Missing bullet: appended at the end of its section, before the next header.
        out = agentctl._set_task_doc_field(out, "## Verification", "Tests command", "`pytest`")
        verification = agentctl._extract_section(out, "## Verification")
        self.assertTrue(verification.endswith("- Tests command: `pytest`"), verification)
        self.assertEqual(agentctl._task_doc_field(out, "## Verification", "Tests command"), "`pytest`")
        # Missing section: created at the end.
        out = agentctl._set_task_doc_field("# T\n\nStatus: todo\n", "## Task Contract", "Goal", "g")
        self.assertIn("## Task Contract\n\n- Goal: g\n", out)
        # Case-insensitive label match, and an empty label reads back as "".
        self.assertEqual(agentctl._task_doc_field(body, "## Task Contract", "definition of done"), "")


if __name__ == "__main__":
    unittest.main()
