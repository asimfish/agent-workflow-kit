"""Unified conversation, run, and resource lease regressions."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


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


class ExecutionLeaseWorkflowRegressionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-execution-leases-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True, timeout=60)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)

    def env(self, session, **extra):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        env["AGENT_WORKFLOW_RESOURCE_LOCK_DIR"] = str(self.root / ".resource-locks")
        env.update(extra)
        return env

    def agentctl(self, *args, session="one", expect=0, env=None, timeout=120):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=self.root, env=env or self.env(session), text=True,
            capture_output=True, timeout=timeout,
        )
        self.assertEqual(proc.returncode, expect, proc.stdout + proc.stderr)
        return proc

    def start(self, session, task, scope):
        return self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", task,
            "--title", f"work for {session}", "--scope", scope, session=session,
        )

    @staticmethod
    def lease_id(output, prefix):
        match = re.search(rf"\b({re.escape(prefix)}-[0-9a-f]{{16}})\b", output)
        if not match:
            raise AssertionError(f"missing {prefix} lease id in: {output}")
        return match.group(1)

    def test_unified_view_keeps_conversations_distinct_and_never_inherits_authority(self):
        self.start("parent", "T-301", "src/parent/")
        child_env = self.env(
            "child",
            AGENT_WORKFLOW_PARENT_SESSION_KEY="parent",
            AGENT_WORKFLOW_SESSION_INSTANCE_ID="child-instance",
        )
        self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--new-id", "T-302",
            "--title", "derived work", "--scope", "src/child/", env=child_env,
        )

        rows = json.loads(
            self.agentctl("lease", "list", "--json", session="parent").stdout
        )["leases"]
        conversations = [row for row in rows if row["kind"] == "conversation"]
        self.assertEqual({row["task"] for row in conversations}, {"T-301", "T-302"})
        self.assertEqual(len({row["holder"]["id"] for row in conversations}), 2)
        child = next(row for row in conversations if row["task"] == "T-302")
        self.assertFalse(child["lineage"]["authority_inherited"])
        self.assertTrue(child["lineage"]["parent_session_key"])

    def test_supervised_run_holds_resource_then_releases_it_after_success(self):
        self.start("one", "T-311", "outputs/T-311/")
        self.start("two", "T-312", "outputs/T-312/")
        command = (
            "from pathlib import Path; import time; time.sleep(0.4); "
            "p=Path('outputs/T-311/result.txt'); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text('ok', encoding='utf-8')"
        )
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-311/",
            "--resource", "host:test:gpu:1", "--",
            sys.executable, "-c", command, session="one",
        )
        run_id = self.lease_id(started.stdout, "run")

        blocked = self.agentctl(
            "resource", "acquire", "host:test:gpu:1",
            session="two", expect=1,
        )
        self.assertIn("already locked", blocked.stderr)
        self.agentctl("run", "wait", run_id, "--timeout", "20", session="one")
        self.assertEqual(
            (self.root / "outputs" / "T-311" / "result.txt").read_text(encoding="utf-8"),
            "ok",
        )

        acquired = self.agentctl(
            "resource", "acquire", "host:test:gpu:1", session="two",
        )
        resource_id = self.lease_id(acquired.stdout, "resource")
        self.agentctl(
            "resource", "release", resource_id, "--reason", "test complete",
            session="two",
        )

    def test_peer_cannot_force_release_another_conversations_resource(self):
        self.start("owner", "T-313", "outputs/T-313/")
        self.start("peer", "T-314", "outputs/T-314/")
        acquired = self.agentctl(
            "resource", "acquire", "host:test:gpu:2", session="owner",
        )
        resource_id = self.lease_id(acquired.stdout, "resource")

        unsupported = self.agentctl(
            "resource", "release", resource_id, "--reason", "peer takeover",
            "--force", session="peer", expect=2,
        )
        self.assertIn("unrecognized arguments: --force", unsupported.stderr)
        refused = self.agentctl(
            "resource", "release", resource_id, "--reason", "peer takeover",
            session="peer", expect=1,
        )
        self.assertIn("belongs to conversation:", refused.stderr)
        rows = json.loads(
            self.agentctl("resource", "status", "--json", session="owner").stdout
        )["resources"]
        lease = next(row for row in rows if row["id"] == resource_id)
        self.assertEqual(lease["status"], "active")

        self.agentctl(
            "resource", "release", resource_id, "--reason", "owner complete",
            session="owner",
        )

    def test_run_rejects_undeclared_write_ownership_outside_task_scope(self):
        self.start("one", "T-321", "outputs/T-321/")
        refused = self.agentctl(
            "run", "start", "--output", "outputs/other/", "--",
            sys.executable, "-c", "print('never launched')",
            session="one", expect=1,
        )
        self.assertIn("outside task scope", refused.stderr)
        rows = json.loads(
            self.agentctl("run", "list", "--json", session="one").stdout
        )["runs"]
        self.assertEqual(rows, [])

    def test_adopted_process_requires_explicit_result_reconciliation(self):
        self.start("one", "T-331", "outputs/T-331/")
        self.start("two", "T-332", "outputs/T-332/")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            cwd=self.root,
        )
        self.addCleanup(lambda: process.poll() is None and process.terminate())
        adopted = self.agentctl(
            "run", "adopt", "--pid", str(process.pid), "--cwd", str(self.root),
            "--output", "outputs/T-331/", session="one",
        )
        run_id = self.lease_id(adopted.stdout, "run")
        process.wait(timeout=10)

        unknown = self.agentctl(
            "run", "wait", run_id, "--timeout", "2", session="one", expect=1,
        )
        self.assertIn("exited_unknown", unknown.stdout)
        refused = self.agentctl(
            "run", "finish", run_id, "--status", "succeeded",
            "--reason", "not my run", session="two", expect=1,
        )
        self.assertIn("belongs to conversation", refused.stderr)
        self.agentctl(
            "run", "finish", run_id, "--status", "succeeded",
            "--reason", "external result inspected", session="one",
        )
        shown = json.loads(
            self.agentctl("run", "show", run_id, "--json", session="one").stdout
        )["runs"][0]
        self.assertEqual(shown["status"], "succeeded")

    def test_supervised_wait_rechecks_transient_unknown_before_timeout(self):
        from tools import agentctl as workflow

        transient = {
            "id": "run-transient",
            "kind": "run",
            "mode": "supervised",
            "status": "running",
            "processes": [],
            "supervisor_process": {},
        }
        succeeded = dict(transient, status="succeeded")
        args = type("Args", (), {"lease": "run-transient", "timeout": 1.0})()
        with (
            mock.patch.object(
                workflow, "_runtime_lease", side_effect=[transient, succeeded]
            ) as load_lease,
            mock.patch.object(workflow.time, "sleep"),
        ):
            self.assertEqual(workflow._run_wait(self.root, args), 0)
        self.assertEqual(load_lease.call_count, 2)

    def test_only_the_owner_conversation_can_stop_a_run(self):
        self.start("owner", "T-341", "outputs/T-341/")
        self.start("peer", "T-342", "outputs/T-342/")
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-341/result.txt", "--",
            sys.executable, "-c", "import time; time.sleep(5)",
            session="owner",
        )
        run_id = self.lease_id(started.stdout, "run")
        time.sleep(0.2)

        refused = self.agentctl(
            "run", "stop", run_id, "--reason", "not my run",
            session="peer", expect=1,
        )
        self.assertIn("belongs to conversation", refused.stderr)
        self.agentctl(
            "run", "stop", run_id, "--reason", "owner requested stop",
            session="owner",
        )
        self.agentctl(
            "run", "wait", run_id, "--timeout", "10",
            session="owner", expect=1,
        )

    def test_supervisor_entrypoint_is_single_use(self):
        self.start("one", "T-351", "outputs/T-351/")
        command = (
            "from pathlib import Path; import os, time; "
            "p=Path('outputs/T-351/pids.txt'); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_text(str(os.getpid())+'\\n', encoding='utf-8'); time.sleep(0.5)"
        )
        started = self.agentctl(
            "run", "start", "--output", "outputs/T-351/pids.txt", "--",
            sys.executable, "-c", command, session="one",
        )
        run_id = self.lease_id(started.stdout, "run")
        replay = self.agentctl(
            "_run-supervise", "--root", str(self.root), "--lease", run_id,
            "--token", "invalid-replay-token", session="one", expect=1,
        )
        self.assertIn("claim rejected", replay.stderr)
        self.agentctl("run", "wait", run_id, "--timeout", "20", session="one")
        lines = (
            self.root / "outputs" / "T-351" / "pids.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)

    def test_loop_lease_keeps_creator_attribution_when_queried_by_peer(self):
        self.start("creator", "T-361", "outputs/T-361/")
        self.start("peer", "T-362", "outputs/T-362/")
        script = (
            "import importlib.util, pathlib; "
            "p=pathlib.Path('tools/agentctl.py').resolve(); "
            "s=importlib.util.spec_from_file_location('agentctl_under_test', p); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "token,error=m._loop_execution_claim(pathlib.Path.cwd(), 'ownership-test'); "
            "assert token and not error, error"
        )
        claimed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root, env=self.env("creator"), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)
        rows = json.loads(
            self.agentctl("lease", "list", "--json", session="peer").stdout
        )["leases"]
        loop = next(row for row in rows if str(row["id"]).startswith("loop:"))
        creator = next(
            row for row in rows
            if row["kind"] == "conversation" and row["task"] == "T-361"
        )
        self.assertEqual(loop["task"], "T-361")
        self.assertEqual(loop["scope"], ["outputs/T-361/"])
        self.assertEqual(loop["holder"]["id"], creator["holder"]["id"])


if __name__ == "__main__":
    unittest.main()
