"""Cross-checkout recovery of machine-wide resource locks.

A GPU lock lives under one directory per host, shared by every project on
that machine, while lease registries and session records are per checkout.
Before this coverage existed, a project whose conversation died holding
`gpu:0` blocked every other project on the host: the newcomer's `doctor`
saw a clean registry, `resource release --force-stale` could not find the
lease, and the acquire refusal named no way out.

These tests run two independently installed checkouts against one lock
directory and pin the evidence rules: live holders are refused, holders the
holder's own registry proves dead self-heal on the next acquire, stale
sessions and vanished checkouts need an explicit `--lock ... --force-stale`,
legacy locks without a recorded checkout are explained and force-releasable,
and `doctor` in the second checkout names all of it.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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


class TwoCheckoutLockTest(unittest.TestCase):
    """Two kit-installed repos on one host sharing a resource lock directory."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="awk-lock-recovery-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.locks = self.base / "locks"
        self.a = self.checkout("a")
        self.b = self.checkout("b")
        self.start(self.a, "conv-a", "T-A", "train/")
        self.start(self.b, "conv-b", "T-B", "eval/")

    def checkout(self, name):
        root = self.base / name
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, timeout=60)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(root)],
            cwd=KIT, text=True, capture_output=True, timeout=600,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        return root

    def env(self, session, **extra):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        env["AGENT_WORKFLOW_SESSION_ID"] = session
        env["CODEX_THREAD_ID"] = f"thread-{session}"
        env["AGENT_WORKFLOW_RESOURCE_LOCK_DIR"] = str(self.locks)
        env.update(extra)
        return env

    def agentctl(self, root, session, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=root, env=self.env(session), text=True,
            capture_output=True, timeout=600,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"[{root.name}] agentctl {' '.join(args)}\nrc={proc.returncode}\n"
            f"{proc.stdout}{proc.stderr}",
        )
        return proc

    def start(self, root, session, task, scope):
        self.agentctl(
            root, session, "work", "--agent", "codex", "--auto-create",
            "--new-id", task, "--title", f"work in {root.name}", "--scope", scope,
        )

    # --- state surgery helpers -------------------------------------------------

    def registry(self, root):
        path, _lock = agentctl._runtime_lease_paths(root)
        return path

    def edit_registry(self, root, mutate):
        path = self.registry(root)
        data = json.loads(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def age_sessions(self, root, hours=2):
        pattern = str(root / ".git" / "agent-workflow" / "sessions" / "*.json")
        aged = 0
        for file in glob.glob(pattern):
            state = json.loads(Path(file).read_text(encoding="utf-8"))
            state["heartbeat_ns"] = time.time_ns() - hours * 3600 * 10**9
            Path(file).write_text(json.dumps(state), encoding="utf-8")
            aged += 1
        self.assertGreaterEqual(aged, 1, pattern)

    def owner_path(self, resource):
        # Same layout as agentctl._resource_lock_location, rooted at the
        # directory the subprocesses see through AGENT_WORKFLOW_RESOURCE_LOCK_DIR.
        digest = agentctl.hashlib.sha256(resource.encode("utf-8")).hexdigest()
        return self.locks / digest / "owner.json"

    def acquire(self, root, session, resource="gpu:0", expect=0):
        return self.agentctl(root, session, "resource", "acquire", resource, expect=expect)

    # --- tests ---------------------------------------------------------------------

    def test_owner_record_names_the_holder_checkout(self):
        self.acquire(self.a, "conv-a")
        owner = json.loads(self.owner_path("gpu:0").read_text(encoding="utf-8"))
        self.assertEqual(Path(owner["checkout"]).resolve(), self.a.resolve())
        self.assertEqual(owner["resource"], "gpu:0")
        self.assertEqual(owner["holder_type"], "conversation")

    def test_live_holder_in_another_checkout_is_refused_and_not_force_releasable(self):
        self.acquire(self.a, "conv-a")
        refused = self.acquire(self.b, "conv-b", expect=1)
        self.assertIn("in another checkout", refused.stderr)
        self.assertIn("holder is live", refused.stderr)
        self.assertIn(str(self.a.resolve()), refused.stderr)
        self.assertNotIn("--force-stale", refused.stderr)

        forced = self.agentctl(
            self.b, "conv-b", "resource", "release", "--lock", "gpu:0",
            "--force-stale", "--reason", "trying to steal", expect=1,
        )
        self.assertIn("holder is live", forced.stderr)
        self.assertTrue(self.owner_path("gpu:0").exists())

        # A clean doctor in B: nothing here is stuck.
        report = json.loads(self.agentctl(self.b, "conv-b", "doctor", "--json").stdout)
        self.assertFalse(any("machine-wide lock" in w for w in report["warnings"]), report)

    def test_released_holder_lease_frees_the_lock_on_the_next_acquire(self):
        self.acquire(self.a, "conv-a")

        # The holder's registry says released but the lock survived (the
        # shape a crash between the two writes leaves behind).
        def mark_released(data):
            for item in data["leases"]:
                if item.get("kind") == "resource":
                    item["status"] = "released"
        self.edit_registry(self.a, mark_released)

        healed = self.acquire(self.b, "conv-b")
        self.assertIn("released orphaned machine-wide lock for gpu:0", healed.stderr)
        self.assertIn("resource lease", healed.stdout)
        owner = json.loads(self.owner_path("gpu:0").read_text(encoding="utf-8"))
        self.assertEqual(Path(owner["checkout"]).resolve(), self.b.resolve())

    def test_missing_holder_lease_heals_only_after_the_grace_window(self):
        self.acquire(self.a, "conv-a")

        def drop_resource_lease(data):
            data["leases"] = [i for i in data["leases"] if i.get("kind") != "resource"]
        self.edit_registry(self.a, drop_resource_lease)

        fresh = self.acquire(self.b, "conv-b", expect=1)
        self.assertIn("registering", fresh.stderr)
        self.assertIn("grace window", fresh.stderr)

        owner_path = self.owner_path("gpu:0")
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        owner["created_at"] = "2020-01-01 00:00:00"
        owner_path.write_text(json.dumps(owner), encoding="utf-8")
        healed = self.acquire(self.b, "conv-b")
        self.assertIn("released orphaned machine-wide lock", healed.stderr)

    def test_stale_holder_needs_an_explicit_force_and_doctor_names_it(self):
        self.acquire(self.a, "conv-a")
        self.age_sessions(self.a)

        refused = self.acquire(self.b, "conv-b", expect=1)
        self.assertIn("holder is stale", refused.stderr)
        self.assertIn("resource release --lock gpu:0 --force-stale", refused.stderr)

        report = json.loads(self.agentctl(self.b, "conv-b", "doctor", "--json").stdout)
        hits = [w for w in report["warnings"] if "machine-wide lock for gpu:0" in w]
        self.assertEqual(len(hits), 1, report["warnings"])
        self.assertIn("stale", hits[0])
        self.assertIn("--lock gpu:0 --force-stale", hits[0])
        interlock = next(c for c in report["checks"] if c["name"] == "resource interlocks")
        self.assertEqual(interlock["status"], "warn")

        no_force = self.agentctl(
            self.b, "conv-b", "resource", "release", "--lock", "gpu:0",
            "--reason", "conversation a is gone", expect=1,
        )
        self.assertIn("requires --force-stale", no_force.stderr)
        self.assertTrue(self.owner_path("gpu:0").exists())

        forced = self.agentctl(
            self.b, "conv-b", "resource", "release", "--lock", "gpu:0",
            "--force-stale", "--reason", "conversation a is gone; inspected its checkout",
        )
        self.assertIn("released machine-wide lock for gpu:0", forced.stdout)
        self.assertFalse(self.owner_path("gpu:0").exists())

        audit = json.loads(
            self.agentctl(self.b, "conv-b", "resource", "status", "--json").stdout
        )["resources"]
        row = next(r for r in audit if r.get("release_mode") == "force-stale-foreign")
        self.assertEqual(row["status"], "released")
        self.assertEqual(row["resources"], ["gpu:0"])
        self.assertEqual(row["foreign_holder_state"], "stale")
        self.assertIn("inspected its checkout", row["release_reason"])

        self.acquire(self.b, "conv-b")
        report = json.loads(self.agentctl(self.b, "conv-b", "doctor", "--json").stdout)
        self.assertFalse(any("machine-wide lock" in w for w in report["warnings"]), report)

    def test_vanished_holder_checkout_is_reported_but_not_auto_released(self):
        self.acquire(self.a, "conv-a")
        shutil.rmtree(self.a)

        refused = self.acquire(self.b, "conv-b", expect=1)
        self.assertIn("checkout_gone", refused.stderr)
        self.assertIn("no longer exists", refused.stderr)
        self.assertIn("--lock gpu:0 --force-stale", refused.stderr)
        self.assertTrue(self.owner_path("gpu:0").exists())

        report = json.loads(self.agentctl(self.b, "conv-b", "doctor", "--json").stdout)
        self.assertTrue(any(
            "machine-wide lock for gpu:0" in w and "no longer exists" in w
            for w in report["warnings"]
        ), report["warnings"])

        self.agentctl(
            self.b, "conv-b", "resource", "release", "--lock", "gpu:0",
            "--force-stale", "--reason", "project a was deleted",
        )
        self.acquire(self.b, "conv-b")

    def test_legacy_lock_without_checkout_is_explained_and_force_releasable(self):
        owner_path = self.owner_path("gpu:0")
        owner_path.parent.mkdir(parents=True)
        owner_path.write_text(json.dumps({
            "lease_id": "resource-0123456789abcdef",
            "resource": "gpu:0",
            "task": "T-OLD",
            "holder_type": "conversation",
            "holder_id": "session-000000000000000000000000",
            "host": agentctl.platform.node(),
            "pid": 1,
            "created_at": "2026-01-01 00:00:00",
        }), encoding="utf-8")

        refused = self.acquire(self.b, "conv-b", expect=1)
        self.assertIn("holder is unknown", refused.stderr)
        self.assertIn("cannot be verified", refused.stderr)
        self.assertIn("--lock gpu:0 --force-stale", refused.stderr)

        forced = self.agentctl(
            self.b, "conv-b", "resource", "release", "--lock", "gpu:0",
            "--force-stale", "--reason", "nvidia-smi shows the card idle",
        )
        self.assertIn("operator judgment", forced.stdout)
        self.acquire(self.b, "conv-b")

    def test_lock_held_by_this_checkout_is_not_treated_as_foreign(self):
        self.acquire(self.b, "conv-b")
        refused = self.agentctl(
            self.b, "conv-b", "resource", "release", "--lock", "gpu:0",
            "--force-stale", "--reason", "wrong tool", expect=1,
        )
        self.assertIn("held by lease", refused.stderr)
        self.assertIn("of this checkout", refused.stderr)
        report = json.loads(self.agentctl(self.b, "conv-b", "doctor", "--json").stdout)
        self.assertFalse(any("machine-wide lock" in w for w in report["warnings"]), report)

        nothing = self.agentctl(
            self.b, "conv-b", "resource", "release", "--lock", "gpu:7",
            "--force-stale", "--reason", "typo", expect=1,
        )
        self.assertIn("no machine-wide lock exists for gpu:7", nothing.stderr)

    def test_release_arguments_are_mutually_exclusive(self):
        both = self.agentctl(
            self.b, "conv-b", "resource", "release", "resource-0123456789abcdef",
            "--lock", "gpu:0", "--force-stale", "--reason", "x", expect=2,
        )
        self.assertIn("not both", both.stderr)
        neither = self.agentctl(
            self.b, "conv-b", "resource", "release", "--reason", "x", expect=2,
        )
        self.assertIn("needs a lease id or --lock", neither.stderr)


if __name__ == "__main__":
    unittest.main()
