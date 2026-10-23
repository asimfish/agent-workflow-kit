"""Installed synthetic-project journeys; identities are simulated, not live LLMs."""

import concurrent.futures
import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from tests.test_task_contract import _ContractTestCase
from tools import agentctl


class SimulatedCollaborationTest(_ContractTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(shutil.rmtree, Path(str(self.root) + "-worktrees"), ignore_errors=True)

    def ctl(self, root, session, *args, expect=0):
        result = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args], cwd=root,
            env=self.env(session), text=True, capture_output=True, timeout=120,
        )
        if expect is not None:
            self.assertEqual(result.returncode, expect, f"{args}\n{result.stdout}\n{result.stderr}")
        return result

    def git_at(self, root, session, *args, expect=0):
        result = subprocess.run(
            ["git", *args], cwd=root, env=self.env(session),
            text=True, capture_output=True, timeout=60,
        )
        if expect is not None:
            self.assertEqual(result.returncode, expect, f"{args}\n{result.stdout}\n{result.stderr}")
        return result

    def claim(self, session, label, kind="docs"):
        task = f"T-SIM-{label.upper()}-001"
        created = self.ctl(
            self.root, session, "work", "--agent", "codex", "--auto-create",
            "--new-id", task, "--type", kind, "--title", f"synthetic {label}",
            "--scope", f"jobs/{label}/", "--done", f"{label} output and evidence are isolated",
        )
        path = re.search(r"^\s*path=(.+)$", created.stdout, re.MULTILINE)
        root = Path(path.group(1)) if path else self.root
        if path:
            self.ctl(root, session, "work", "--agent", "codex", "--task", task)
        return root, task

    def write_guard(self, root, session, relative, expect=0):
        result = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "pre-tool-use"],
            cwd=root, env=self.env(session), text=True, capture_output=True, timeout=60,
            input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": relative}}),
        )
        # Codex consumes a structured denial even when the hook exits zero.
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout) if result.stdout else {}
        denied = output.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
        self.assertEqual(denied, expect == 2, result.stdout + result.stderr)
        return result

    def write(self, root, session, relative, text):
        self.write_guard(root, session, relative)
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def wait_for(self, probe, message, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = probe()
            if value:
                return value
            time.sleep(0.05)
        self.fail(message)

    def cleanup_run(self, root, session, run):
        def status():
            return json.loads(self.ctl(root, session, "run", "show", run, "--json").stdout)["runs"][0]["status"]

        terminal = {"succeeded", "failed", "cancelled"}
        if status() not in terminal:
            self.ctl(root, session, "run", "stop", run, "--reason", "synthetic test cleanup")
            self.ctl(root, session, "run", "wait", run, "--timeout", "30", expect=None)
        self.assertIn(status(), terminal, "cleanup left a live or unreconciled collector")

    def test_same_relative_code_scopes_progress_in_separate_worktrees(self):
        self.git_at(self.root, None, "add", *agentctl.ADOPTION_COMMIT_ADD_PATHS)
        self.git_at(self.root, None, "commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE)
        barrier = threading.Barrier(2)

        def allocate(session):
            task = f"T-SIM-{session.upper()}-001"
            barrier.wait(timeout=30)
            created = self.ctl(
                self.root, session, "work", "--agent", "codex", "--auto-create",
                "--new-id", task, "--type", "code", "--title", session,
                "--scope", "src/", "--done", "own code and task evidence remain isolated",
            )
            root = Path(re.search(r"^\s*path=(.+)$", created.stdout, re.MULTILINE).group(1))
            self.ctl(root, session, "work", "--agent", "codex", "--task", task)
            return session, (root, task)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            workers = dict(pool.map(allocate, ("code-a", "code-b")))
        a, task_a = workers["code-a"]
        b, task_b = workers["code-b"]
        self.assertNotEqual(a, b)
        self.assertEqual(self.git_at(self.root, None, "status", "--porcelain").stdout, "")

        barrier = threading.Barrier(2)

        def develop(session):
            root, task = workers[session]
            self.write(root, session, "src/result.txt", session + "\n")
            barrier.wait(timeout=30)
            self.ctl(root, session, "note", f"SIM[{session}] same path, independent output")
            command = shlex.join([
                sys.executable, "-c",
                f"from pathlib import Path; assert Path('src/result.txt').read_text() == {session + chr(10)!r}",
            ])
            self.ctl(root, session, "contract", "--tests-cmd", command)
            self.ctl(root, session, "finish", "--summary", f"{session} verified locally")
            board = json.loads((root / ".agent/board.json").read_text())
            self.assertEqual(board["tasks"][task]["status"], "review")
            return root

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(develop, workers))
        for session, (root, task) in workers.items():
            peer = "code-b" if session == "code-a" else "code-a"
            self.assertEqual((root / "src/result.txt").read_text(), session + "\n")
            doc = (root / f".agent/tasks/{task}.md").read_text()
            self.assertIn(f"SIM[{session}]", doc)
            self.assertNotIn(f"SIM[{peer}]", doc)
            self.assertIn("- Tests-exit: 0", doc)
        self.write_guard(a, "code-a", str(b / "src/result.txt"), expect=2)
        self.write_guard(b, "code-b", str(a / f".agent/tasks/{task_a}.md"), expect=2)
        denied = self.ctl(b, "code-b", "run", "start", "--output", str(a / "src/result.txt"),
                           "--", sys.executable, "-c", "raise AssertionError('must not run')", expect=1)
        self.assertIn("output", denied.stderr)
        self.assertEqual((a / "src/result.txt").read_text(), "code-a\n")
        resource = self.ctl(a, "code-a", "resource", "acquire", "synthetic:same-scope-slot:0")
        lease = re.search(r"\bresource-[0-9a-f]{16}\b", resource.stdout).group()
        try:
            locked = self.ctl(b, "code-b", "resource", "acquire", "synthetic:same-scope-slot:0", expect=1)
            self.assertIn("already locked", locked.stderr)
        finally:
            self.ctl(a, "code-a", "resource", "release", lease, "--reason", "synthetic check complete")
        heads = []
        for session, (root, task) in workers.items():
            self.git_at(root, session, "add", ".agent", "src/")
            self.git_at(root, session, "commit", "-q", "-m", f"feat(simulation): deliver {session}\n\nRefs: {task}")
            heads.append(self.git_at(root, session, "rev-parse", "HEAD").stdout.strip())
        merged = self.git_at(self.root, None, "merge-tree", "--write-tree", *heads, expect=1)
        self.assertIn("CONFLICT (add/add)", merged.stdout)
        self.assertIn("src/result.txt", merged.stdout)
        self.assertEqual((a / "src/result.txt").read_text(), "code-a\n")
        self.assertEqual((b / "src/result.txt").read_text(), "code-b\n")
        self.assertEqual(self.git_at(self.root, None, "status", "--porcelain").stdout, "")

    def test_four_shared_document_sessions_preserve_all_updates_without_retries(self):
        labels = ("catalog", "pricing", "validation", "replay")
        barrier = threading.Barrier(len(labels))

        def start(label):
            barrier.wait(timeout=30)
            return label, self.claim(label, label)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            tasks = dict(pool.map(start, labels))
        rows = json.loads(self.ctl(self.root, "catalog", "sessions", "list", "--json").stdout)["sessions"]
        own_rows = [row for row in rows if row.get("task") in {task for _, task in tasks.values()}]
        self.assertEqual(len(own_rows), 4)
        self.assertEqual(len({row["workflow_session_key"] for row in own_rows}), 4)

        def record(label):
            try:
                for step in range(3):
                    barrier.wait(timeout=30)
                    self.ctl(self.root, label, "note", f"SIM[{label}][{step}]")
            except Exception:
                barrier.abort()
                raise

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(record, label): label for label in labels}
            failures = []
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    failures.append(f"{futures[future]}: {error}")
        self.assertEqual(failures, [], "\n".join(failures))
        progress = (self.root / ".agent/logs/progress.md").read_text(encoding="utf-8")
        for label, (_, task) in tasks.items():
            doc = self.doc(task)
            self.assertEqual(self.status(task), "in_progress")
            for step in range(3):
                marker = f"SIM[{label}][{step}]"
                self.assertEqual(doc.count(marker), 1)
                self.assertEqual(progress.count(marker), 1)
            for peer in set(labels) - {label}:
                self.assertNotIn(f"SIM[{peer}]", doc)
            self.write(self.root, label, f"jobs/{label}/result.txt", label)

        board_before = (self.root / ".agent/board.json").read_bytes()
        catalog_doc = self.doc(tasks["catalog"][1])
        denied = self.ctl(
            self.root, "copied-history", "work", "--agent", "codex",
            "--task", tasks["catalog"][1], expect=1,
        )
        self.assertRegex(denied.stdout + denied.stderr, r"conflict|claimed|locked|takeover")
        self.assertEqual(self.doc(tasks["catalog"][1]), catalog_doc)
        self.write_guard(self.root, "pricing", "jobs/catalog/result.txt", expect=2)
        self.write_guard(self.root, "pricing", f".agent/tasks/{tasks['catalog'][1]}.md", expect=2)
        self.assertEqual((self.root / "jobs/catalog/result.txt").read_text(), "catalog")
        self.assertEqual(self.doc(tasks["catalog"][1]), catalog_doc)
        self.assertEqual((self.root / ".agent/board.json").read_bytes(), board_before)
        self.ctl(
            self.root, "overlap", "work", "--agent", "codex", "--auto-create",
            "--new-id", "T-SIM-OVERLAP-001", "--type", "docs", "--title", "overlap",
            "--scope", "jobs/", "--done", "must not start", expect=1,
        )
        self.assertEqual((self.root / ".agent/board.json").read_bytes(), board_before)
        self.assertFalse((self.root / ".agent/tasks/T-SIM-OVERLAP-001.md").exists())

        self.ctl(self.root, "validation", "finish", "--summary", "validation notes delivered")
        self.assertEqual(self.status(tasks["validation"][1]), "review")
        for label in ("catalog", "pricing", "replay"):
            self.ctl(self.root, label, "note", f"SIM[{label}] continues without refresh")
            self.assertEqual(self.status(tasks[label][1]), "in_progress")
        self.ctl(self.root, "catalog", "reconcile", "check")

        # A trusted human's direction change must invalidate the existing receipt.
        plan = self.root / ".agent/PROJECT_PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "\nHuman direction: audit all results.\n", encoding="utf-8")
        stale = self.ctl(self.root, "catalog", "note", "must not be recorded", expect=None)
        self.assertNotEqual(stale.returncode, 0, stale.stdout + stale.stderr)
        self.assertIn("refresh", stale.stdout + stale.stderr)
        self.assertNotIn("must not be recorded", self.doc(tasks["catalog"][1]))
        self.ctl(self.root, "catalog", "refresh")
        self.ctl(self.root, "catalog", "note", "human direction read; continue")

    def test_copied_history_child_and_released_task_handoff_remain_isolated(self):
        _, task = self.claim("parent", "handoff")
        original = self.write(self.root, "parent", "jobs/handoff/result.txt", "parent partial\n")
        self.ctl(self.root, "parent", "note", "SIM[parent] partial output ready for a verified handoff")
        parent = json.loads(self.ctl(self.root, "parent", "status", "--json").stdout)

        # Simulate a fork carrying the parent's workflow ID, but not its runtime.
        child_env = self.env("parent", CODEX_THREAD_ID="thread-history-child")
        started = subprocess.run(
            [sys.executable, "tools/agent_workflow_hook.py", "session-start"],
            cwd=self.root, env=child_env, text=True, capture_output=True, timeout=60,
            input=json.dumps({"session_id": "parent", "parent_session_id": "parent", "source": "startup"}),
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        exported = json.loads(started.stdout)["env"]
        self.assertEqual(exported["AGENT_WORKFLOW_SESSION_ID"], "")
        child_env.update(exported)

        def child(*args, expect=0):
            result = subprocess.run(
                [sys.executable, "tools/agentctl.py", *args], cwd=self.root,
                env=child_env, text=True, capture_output=True, timeout=120,
            )
            self.assertEqual(result.returncode, expect, f"{args}\n{result.stdout}\n{result.stderr}")
            return result

        before = self.doc(task)
        child("work", "--agent", "codex", "--task", task, expect=1)
        self.assertEqual(self.doc(task), before)
        peer_task = "T-SIM-HISTORY-CHILD-001"
        child("work", "--agent", "codex", "--auto-create", "--new-id", peer_task,
              "--type", "docs", "--title", "independent child task", "--scope", "jobs/child/",
              "--done", "child notes belong only to the child task")
        peer = json.loads(child("status", "--json").stdout)
        self.assertEqual(peer["task"], peer_task)
        self.assertNotEqual(peer["workflow_session_key"], parent["workflow_session_key"])
        self.assertRegex(peer["parent_session_key"], r"^lineage-[0-9a-f]{24}$")
        focus = child("focus").stdout
        self.assertIn(peer_task, focus)
        self.assertNotIn(f"FOCUS [{task}]", focus)
        child("note", "SIM[child] independent work after copied history")
        self.ctl(self.root, "parent", "note", "SIM[parent] still owns its task without refreshing for the child")

        # A legal handoff retains the partial files and rotates claim authority.
        peer_doc = self.doc(peer_task)
        reason = "parent stopped; task document and partial output inspected; no background process"
        self.ctl(self.root, "parent", "sessions", "release", "--reason", reason)
        self.assertEqual(original.read_text(), "parent partial\n")
        self.ctl(self.root, "successor", "work", "--agent", "codex", "--task", task)
        successor = json.loads(self.ctl(self.root, "successor", "status", "--json").stdout)
        self.assertEqual(successor["task"], task)
        self.assertNotEqual(successor["claim_id"], parent["claim_id"])
        self.assertNotEqual(successor["workflow_session_key"], parent["workflow_session_key"])
        self.assertIn("SIM[parent] partial output", self.doc(task))
        self.ctl(self.root, "successor", "note", f"SIM[successor] verified handoff: {reason}")
        self.write(self.root, "successor", "jobs/handoff/result.txt", "parent partial\nsuccessor completed\n")
        self.assertEqual(self.doc(peer_task), peer_doc)

        board = (self.root / ".agent/board.json").read_bytes()
        task_doc = self.doc(task)
        for args in (
            ("note", "SIM[revoked] must not be recorded"),
            ("finish", "--summary", "SIM[revoked] must not finish"),
            ("refresh",), ("upgrade", "rebind"),
            ("work", "--agent", "codex", "--task", task),
        ):
            with self.subTest(old_owner=args):
                denied = self.ctl(self.root, "parent", *args, expect=None)
                self.assertNotEqual(denied.returncode, 0, denied.stdout + denied.stderr)
                self.assertRegex(denied.stdout + denied.stderr, r"released.*claim|claim authority|session conflict|already claimed")
                self.assertEqual(self.doc(task), task_doc)
                self.assertEqual(self.doc(peer_task), peer_doc)
                self.assertEqual((self.root / ".agent/board.json").read_bytes(), board)
        self.write_guard(self.root, "parent", "jobs/handoff/result.txt", expect=2)
        self.write_guard(self.root, "parent", f".agent/tasks/{task}.md", expect=2)
        self.assertEqual(original.read_text(), "parent partial\nsuccessor completed\n")
        self.assertEqual(self.doc(task), task_doc)
        self.assertEqual(self.doc(peer_task), peer_doc)
        self.assertEqual((self.root / ".agent/board.json").read_bytes(), board)

        self.ctl(self.root, "successor", "finish", "--summary", "verified partial output preserved and completed")
        child("note", "SIM[child] continues after successor finish without refreshing")
        self.assertEqual(self.status(task), "review")
        self.assertEqual(self.status(peer_task), "in_progress")
        self.assertEqual(json.loads(child("status", "--json").stdout)["claim_id"], peer["claim_id"])
        self.assertNotIn("SIM[revoked]", self.doc(task))
        self.assertNotIn("SIM[child]", self.doc(task))
        self.assertNotIn("SIM[parent]", self.doc(peer_task))
        self.assertNotIn("SIM[successor]", self.doc(peer_task))
        self.ctl(self.root, "successor", "reconcile", "check")

    def test_isolated_collectors_own_outputs_resources_and_normal_git_delivery(self):
        for args in (
            ("add", *agentctl.ADOPTION_COMMIT_ADD_PATHS),
            ("commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE),
        ):
            self.git_at(self.root, None, *args)
        a, task_a = self.claim("worker-a", "batch-a", "experiment")
        b, task_b = self.claim("worker-b", "batch-b", "experiment")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, self.root)
        common_a = self.git_at(a, "worker-a", "rev-parse", "--git-common-dir").stdout.strip()
        common_b = self.git_at(b, "worker-b", "rev-parse", "--git-common-dir").stdout.strip()
        self.assertEqual((a / common_a).resolve(), (b / common_b).resolve())
        self.assertEqual(self.git_at(self.root, None, "status", "--porcelain").stdout, "")

        def collector(root, session, label, first):
            self.write(root, session, f"jobs/{label}/collect.py",
                       "import json, time\nfrom pathlib import Path\n"
                       "p = Path(__file__).parent\n(p / 'ready').touch()\n"
                       "deadline = time.monotonic() + 90\n"
                       "while not (p / 'release').exists():\n"
                       "    if time.monotonic() >= deadline:\n        raise TimeoutError('release missing')\n"
                       "    time.sleep(0.05)\n"
                       f"(p / 'result.json').write_text(json.dumps(list(range({first}, {first + 20}))))\n")

        collector(a, "worker-a", "batch-a", 1)
        collector(b, "worker-b", "batch-b", 21)
        self.assertFalse((a / "jobs/batch-b/collect.py").exists())
        self.assertFalse((b / "jobs/batch-a/collect.py").exists())
        self.write_guard(a, "worker-a", str(b / "jobs/batch-b/collect.py"), expect=2)

        started = self.ctl(
            a, "worker-a", "run", "start", "--output", "jobs/batch-a/",
            "--resource", "synthetic:collector-slot:0", "--", sys.executable, "jobs/batch-a/collect.py",
        )
        run_a = re.search(r"\brun-[0-9a-f]{16}\b", started.stdout).group()
        try:
            self.wait_for(lambda: (a / "jobs/batch-a/ready").exists(), "collector A never became ready")
            locked = self.ctl(b, "worker-b", "resource", "acquire", "synthetic:collector-slot:0", expect=1)
            self.assertIn("already locked", locked.stderr)
            refused = self.ctl(b, "worker-b", "run", "stop", run_a, "--reason", "not the holder", expect=1)
            self.assertIn("belongs to conversation", refused.stderr)
            self.assertFalse((a / "jobs/batch-a/result.json").exists())
            self.ctl(b, "worker-b", "note", "B remains productive while A holds the slot")
            self.write(a, "worker-a", "jobs/batch-a/release", "continue")
            self.ctl(a, "worker-a", "run", "wait", run_a, "--timeout", "30")
        finally:
            self.cleanup_run(a, "worker-a", run_a)
        self.assertEqual(json.loads((a / "jobs/batch-a/result.json").read_text()), list(range(1, 21)))
        released = self.ctl(b, "worker-b", "resource", "acquire", "synthetic:collector-slot:0")
        resource = re.search(r"\bresource-[0-9a-f]{16}\b", released.stdout).group()
        self.ctl(b, "worker-b", "resource", "release", resource, "--reason", "handover inspected")
        self.write(b, "worker-b", "jobs/batch-b/release", "continue")
        started_b = self.ctl(
            b, "worker-b", "run", "start", "--output", "jobs/batch-b/",
            "--resource", "synthetic:collector-slot:0", "--", sys.executable, "jobs/batch-b/collect.py",
        )
        run_b = re.search(r"\brun-[0-9a-f]{16}\b", started_b.stdout).group()
        try:
            self.ctl(b, "worker-b", "run", "wait", run_b, "--timeout", "30")
        finally:
            self.cleanup_run(b, "worker-b", run_b)
        data_a = json.loads((a / "jobs/batch-a/result.json").read_text())
        data_b = json.loads((b / "jobs/batch-b/result.json").read_text())
        self.assertEqual(data_a + data_b, list(range(1, 41)))
        self.assertEqual(set(data_a) & set(data_b), set())
        for root, session, label, task, first in (
            (a, "worker-a", "batch-a", task_a, 1), (b, "worker-b", "batch-b", task_b, 21),
        ):
            code = (f"import json; from pathlib import Path; "
                    f"assert json.loads(Path('jobs/{label}/result.json').read_text()) == list(range({first}, {first + 20}))")
            command = shlex.join([sys.executable, "-c", code])
            self.ctl(root, session, "contract", "--tests-cmd", command)
            self.ctl(root, session, "finish", "--summary", f"{label}: exact 20-record output")
            board = json.loads((root / ".agent/board.json").read_text())
            self.assertEqual(board["tasks"][task]["status"], "review")
            self.assertIn("- Tests-exit: 0", (root / f".agent/tasks/{task}.md").read_text())

        self.git_at(a, "worker-a", "add", ".agent", "jobs/batch-a/")
        before = self.git_at(a, "worker-a", "rev-parse", "HEAD").stdout
        bad = self.git_at(a, "worker-a", "commit", "-m", "unstructured message", expect=None)
        self.assertNotEqual(bad.returncode, 0, bad.stdout + bad.stderr)
        self.assertEqual(self.git_at(a, "worker-a", "rev-parse", "HEAD").stdout, before)
        self.git_at(a, "worker-a", "commit", "-m", f"feat(simulation): collect first batch\n\nRefs: {task_a}")
        remote = self.root.parent / f"{self.root.name}-origin.git"
        self.addCleanup(shutil.rmtree, remote, ignore_errors=True)
        self.git_at(self.root, None, "init", "--bare", "-q", str(remote))
        self.git_at(a, "worker-a", "remote", "add", "origin", str(remote))
        self.git_at(a, "worker-a", "push", "origin", "HEAD")
        branch = self.git_at(a, "worker-a", "branch", "--show-current").stdout.strip()
        remote_head = self.git_at(a, "worker-a", "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()[0]
        self.assertEqual(remote_head, self.git_at(a, "worker-a", "rev-parse", "HEAD").stdout.strip())

    def test_interrupted_loop_is_resumable_only_by_its_owner(self):
        _, task = self.claim("loop-owner", "loop-owner")
        _, peer_task = self.claim("loop-peer", "loop-peer")
        state_path = self.root / ".agent/loops/state.json"
        process = subprocess.Popen(
            [sys.executable, "tools/agentctl.py", "loop", "cycle", "--checkpoint", "pre-finish",
             "--cycles", "2", "--interval", "60", "--force"],
            cwd=self.root, env=self.env("loop-owner"), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            def first_cycle():
                self.assertIsNone(process.poll(), "loop exited before its first checkpoint")
                runtime = json.loads(state_path.read_text()).get("cycle_runtime") or {}
                return runtime if runtime.get("completed_cycles") == 1 else None

            self.wait_for(first_cycle, "loop never reached the interval after its first checkpoint")
            process.terminate()
            process.communicate(timeout=20)
            observed = json.loads(self.ctl(self.root, "loop-owner", "loop", "status", "--json").stdout)
            self.assertEqual(observed["status"], "interrupted")
            self.assertTrue(observed["runtime"]["resume_safe"])
            before = json.loads(state_path.read_text())["cycle_runtime"]
            reports = set((self.root / ".agent/loops/runs").glob("*.md"))
            self.ctl(self.root, "loop-peer", "loop", "resume", expect=2)
            self.ctl(self.root, "loop-peer", "loop", "stop", "--reason", "not my cycle", expect=2)
            self.assertEqual(json.loads(state_path.read_text())["cycle_runtime"], before)
            self.assertEqual(set((self.root / ".agent/loops/runs").glob("*.md")), reports)
            self.ctl(self.root, "loop-peer", "note", "peer continues while owner is interrupted")
            peer_doc = self.doc(peer_task)
            self.ctl(self.root, "loop-owner", "loop", "resume")
            runtime = json.loads(state_path.read_text())["cycle_runtime"]
            self.assertEqual((runtime["status"], runtime["completed_cycles"]), ("completed", 2))
            self.assertIsNone(runtime["inflight_cycle"])
            for report in runtime["last_reports"]:
                self.assertIn(f"- Task: {task}", (self.root / report).read_text())
            self.assertEqual(self.status(peer_task), "in_progress")
            self.assertEqual(peer_doc, self.doc(peer_task))
            self.ctl(self.root, "loop-owner", "reconcile", "check")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=20)
