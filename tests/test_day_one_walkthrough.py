"""Regression coverage for the README day-one walkthrough gaps.

A verbatim replay of the README on a blank project found places where the
prose and the tool disagreed. These tests pin the fixed behavior:

- `doctor` is a read-only diagnostic and must run from a plain terminal
  with no agent conversation identity, while mutating commands stay refused.
- `init` gitignores the default artifact root and a re-run appends only the
  entries an older install is missing.
- The `finish` hint and the gate refusal name the reviewer registration
  command instead of leaving the reviewer to guess.
- The commit that installs the kit passes the kit's own hooks without a
  task, and only when it contains nothing but what the installer wrote.
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
from unittest import mock

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


class _KitRepoTestCase(unittest.TestCase):
    """Shared scaffolding: a temp repo with the kit installed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-day-one-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git("init", "-q")
        self.git("config", "user.email", "agent@example.com")
        self.git("config", "user.name", "Agent Test")
        self.init()

    def init(self):
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(self.root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        return install

    def git(self, *args, check=True):
        proc = subprocess.run(
            ["git", *args], cwd=str(self.root), text=True,
            capture_output=True, timeout=60,
        )
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def env(self, session=None, **extra):
        env = os.environ.copy()
        for name in IDENTITY_ENV:
            env.pop(name, None)
        # Resource locks are machine-wide by design (a GPU is a GPU no matter
        # which checkout claims it); keep the suite hermetic.
        env["AGENT_WORKFLOW_RESOURCE_LOCK_DIR"] = str(self.root / ".resource-locks")
        if session:
            env["AGENT_WORKFLOW_SESSION_ID"] = session
            # A real Codex/Claude/Cursor conversation also carries a host
            # thread id; the review gate fingerprints it as the runtime.
            env["CODEX_THREAD_ID"] = f"thread-{session}"
        env.update(extra)
        return env

    def agentctl(self, *args, expect=0, session=None, **extra):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(self.root), env=self.env(session, **extra), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        return proc


class DoctorWithoutIdentityTest(_KitRepoTestCase):
    def test_doctor_runs_from_a_plain_terminal(self):
        # README step 5: the human checks in from any terminal. Terminal.app
        # exports TERM_SESSION_ID, which the controller classifies as a
        # non-unique identity and refuses for anything that touches sessions.
        proc = self.agentctl("doctor", TERM_SESSION_ID="w0t0p0")
        self.assertIn("agentctl doctor:", proc.stdout)
        self.assertIn("agent sessions", proc.stdout)
        self.assertIn("resource interlocks", proc.stdout)
        self.assertNotIn("conversation identity is unavailable", proc.stderr)

        report = json.loads(
            self.agentctl("doctor", "--json", TERM_SESSION_ID="w0t0p0").stdout
        )
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["problems"], [])

        # No identity at all (cron, CI shell) works too.
        self.agentctl("doctor")

        # The exemption is narrow: claiming work still needs a real identity.
        refused = self.agentctl(
            "work", "--agent", "human", "--auto-create",
            "--title", "no identity", "--scope", "src/",
            expect=2, TERM_SESSION_ID="w0t0p0",
        )
        self.assertIn("conversation identity is unavailable", refused.stderr)

    def test_doctor_still_reports_interlocks_without_identity(self):
        # A conversation leases gpu:0 and then dies; the human should see
        # the stuck card and the recovery command from a plain terminal.
        self.agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--title", "train", "--scope", "src/train/", session="conv-a",
        )
        self.agentctl("resource", "acquire", "gpu:0", session="conv-a")
        aged = 0
        for path in glob.glob(str(self.root / ".git" / "agent-workflow" / "sessions" / "*.json")):
            state = json.loads(Path(path).read_text(encoding="utf-8"))
            state["heartbeat_ns"] = time.time_ns() - 2 * 3600 * 10**9
            Path(path).write_text(json.dumps(state), encoding="utf-8")
            aged += 1
        self.assertEqual(aged, 1)

        proc = self.agentctl("doctor", TERM_SESSION_ID="w0t0p0")
        self.assertIn("stuck without a live holder", proc.stdout)
        self.assertIn("gpu:0", proc.stdout)
        self.assertIn("resource release", proc.stdout)
        self.assertIn("--force-stale", proc.stdout)
        self.assertIn("stale agent session claim", proc.stdout)

    def test_identity_free_set_is_shared_with_the_hook(self):
        from tools import agent_workflow_hook as workflow_hook

        self.assertIn(("doctor",), agentctl.IDENTITY_FREE_COMMAND_PATHS)
        self.assertIn(("doctor",), workflow_hook.IDENTITY_FREE_COMMAND_PATHS)
        self.assertFalse(
            workflow_hook.command_requires_agentctl_identity("python3 tools/agentctl.py doctor")
        )
        template = (KIT / "templates" / "project" / "tools" / "agent_workflow_hook.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            template, (KIT / "tools" / "agent_workflow_hook.py").read_text(encoding="utf-8"),
            "the distributed hook template must match tools/agent_workflow_hook.py",
        )


class GitignoreManagedEntriesTest(_KitRepoTestCase):
    def test_init_ignores_the_default_artifact_root(self):
        text = (self.root / ".gitignore").read_text(encoding="utf-8")
        for entry in agentctl.GITIGNORE_MANAGED_ENTRIES:
            self.assertIn(entry, text.splitlines())
        self.assertIn(".agent-artifacts/", agentctl.GITIGNORE_MANAGED_ENTRIES)
        # `run start --output .agent-artifacts/<task>/` is where checkpoints
        # land; Git must not offer them for staging.
        probe = self.git("check-ignore", "-q", ".agent-artifacts/T-001/ckpt.bin", check=False)
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)

    def test_rerun_appends_only_missing_entries_inside_the_managed_block(self):
        gitignore = self.root / ".gitignore"
        gitignore.write_text(
            "build/\n\n# Agent Workflow Kit local state\n.agent/state/\n.agent/tmp/\n\n*.pyc\n",
            encoding="utf-8",
        )
        self.init()
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            ["build/", "", "# Agent Workflow Kit local state", ".agent/state/",
             ".agent/tmp/", ".agent-artifacts/", "", "*.pyc"],
        )
        self.init()
        self.assertEqual(lines, gitignore.read_text(encoding="utf-8").splitlines())

    def test_user_spelling_of_an_entry_is_respected(self):
        gitignore = self.root / ".gitignore"
        gitignore.write_text("/.agent-artifacts/\n", encoding="utf-8")
        self.init()
        text = gitignore.read_text(encoding="utf-8")
        self.assertEqual(text.count("agent-artifacts"), 1, text)
        self.assertIn(".agent/state/", text)


class ReviewerRegistrationHintTest(_KitRepoTestCase):
    def test_finish_hint_and_gate_refusal_name_the_registration_command(self):
        self.agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--title", "fix the data loader", "--scope", "src/data/", session="worker",
        )
        board = json.loads((self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        task = next(t for t, e in board["tasks"].items() if e.get("title") == "fix the data loader")
        finished = self.agentctl(
            "finish", "--summary", "done", "--tests", "pytest: 1 passed", session="worker",
        )
        self.assertIn("agentctl agents add --id <reviewer> --role review", finished.stdout)
        self.assertIn(f"gate approve --task {task} --by <reviewer>", finished.stdout)

        # An unregistered reviewer follows the README but skips registration.
        self.agentctl(
            "work", "--agent", "reviewer-x", "--auto-create", "--type", "review",
            "--title", f"review {task}", "--scope", ".agent/", session="reviewer",
        )
        refused = self.agentctl(
            "gate", "approve", "--task", task, "--by", "reviewer-x", "--note", "lgtm",
            expect=1, session="reviewer",
        )
        self.assertIn("independent gate decision rejected", refused.stderr)
        self.assertIn("agentctl agents add --id reviewer-x --role review", refused.stderr)

        # Registering, refreshing the read receipt, and deciding then works.
        self.agentctl("agents", "add", "--id", "reviewer-x", "--role", "review", session="reviewer")
        self.agentctl("refresh", session="reviewer")
        approved = self.agentctl(
            "gate", "approve", "--task", task, "--by", "reviewer-x", "--note", "lgtm",
            session="reviewer",
        )
        self.assertIn("approved -> done", approved.stdout)


class _AdoptedRepoTestCase(_KitRepoTestCase):
    """A project with history, a bare origin, and the kit freshly installed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="awk-adopt-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.email", "human@example.com")
        self.git("config", "user.name", "Human")
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")
        self.origin = self.bare_origin()
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-q", "-u", "origin", "main")
        self.install = self.init()

    def bare_origin(self):
        origin = Path(tempfile.mkdtemp(prefix="awk-adopt-origin-"))
        self.addCleanup(shutil.rmtree, origin, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        return origin

    def human_git(self, *args, check=True):
        # A person in Terminal.app: no agent conversation identity at all.
        proc = subprocess.run(
            ["git", *args], cwd=str(self.root), text=True, capture_output=True,
            timeout=120, env=self.env(TERM_SESSION_ID="w0t0p0"),
        )
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def stage_installation(self):
        self.human_git("add", *agentctl.ADOPTION_COMMIT_ADD_PATHS)

    def adopt_and_push(self):
        self.stage_installation()
        self.human_git("commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE)
        self.human_git("push", "-q", "origin", "main")


class AdoptionCommitTest(_AdoptedRepoTestCase):
    """The commit that installs the kit must pass the kit's own hooks.

    Before this, `./install.sh` left a human with staged files that
    pre-commit refused ("no active task"), a claim they could not make from
    a plain terminal, and a push that pre-push refused ("missing task ID").
    The only way through was `--no-verify` twice.
    """

    def test_install_commit_passes_every_hook_from_a_plain_terminal(self):
        hint = self.install.stdout
        self.assertIn("commit the installation on its own", hint)
        self.assertIn("git add " + " ".join(agentctl.ADOPTION_COMMIT_ADD_PATHS), hint)
        self.assertIn(f'git commit -m "{agentctl.ADOPTION_COMMIT_MESSAGE}"', hint)

        self.stage_installation()
        self.assertEqual(self.human_git("status", "--porcelain", "--untracked-files=all").stdout.count("??"), 0)
        committed = self.human_git("commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE)
        self.assertIn("agentctl check (pre-commit): OK", committed.stdout + committed.stderr)
        self.assertIn("agentctl check (commit-msg): OK", committed.stdout + committed.stderr)

        pushed = self.human_git("push", "origin", "main")
        self.assertIn("agentctl check (pre-push): OK", pushed.stdout + pushed.stderr)
        self.assertEqual(
            self.git("rev-parse", "HEAD").stdout.strip(),
            self.git("rev-parse", "origin/main").stdout.strip(),
        )

        # The rules start with the very next commit.
        (self.root / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")
        self.human_git("add", "src/app.py")
        refused = self.human_git("commit", "-q", "-m", "feat(app): change", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("staged changes but no active task", refused.stdout + refused.stderr)

    def test_a_reinstall_is_not_an_adoption_commit(self):
        self.stage_installation()
        self.human_git("commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE)
        # Upgrading the kit later rewrites managed files; that commit is
        # ordinary work and must be claimed like any other.
        manifest = self.root / ".agent" / "install-manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["updated_at"] = "later"
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.human_git("add", ".agent/install-manifest.json")
        refused = self.human_git("commit", "-q", "-m", "chore(agent): upgrade kit", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("no active task", refused.stdout + refused.stderr)

    def test_other_files_bundled_into_the_install_commit_are_refused(self):
        (self.root / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
        self.stage_installation()
        self.human_git("add", "src/new.py")
        refused = self.human_git("commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("may contain only files agentctl init wrote", refused.stdout + refused.stderr)
        self.assertIn("src/new.py", refused.stdout + refused.stderr)
        self.assertIn("commit those separately under a task", refused.stdout + refused.stderr)
        self.assertNotIn("no active task", refused.stdout + refused.stderr)

        self.human_git("reset", "-q", "src/new.py")
        self.human_git("commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE)

    def test_an_edited_controller_is_not_an_install_commit(self):
        controller = self.root / "tools" / "agentctl.py"
        controller.write_text(
            controller.read_text(encoding="utf-8") + "\n# local tweak\n", encoding="utf-8",
        )
        self.stage_installation()
        refused = self.human_git("commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE, check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("tools/agentctl.py differs from the version agentctl init installed", refused.stdout + refused.stderr)

    def test_pre_push_judges_the_install_commit_by_content(self):
        # Hooks skipped locally (--no-verify) cannot smuggle extra files past
        # the push: the adoption exemption is decided from the commit itself.
        (self.root / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
        self.stage_installation()
        self.human_git("add", "src/new.py")
        self.human_git("commit", "-q", "--no-verify", "-m", agentctl.ADOPTION_COMMIT_MESSAGE)
        refused = self.human_git("push", "origin", "main", check=False)
        self.assertNotEqual(refused.returncode, 0)
        output = refused.stdout + refused.stderr
        self.assertIn("src/new.py", output)
        self.assertNotIn("commit missing task ID", output)

        # The subject is still held to Conventional Commits.
        self.human_git("reset", "-q", "--soft", "HEAD~1")
        self.human_git("reset", "-q", "src/new.py")
        self.human_git("commit", "-q", "--no-verify", "-m", "install the kit")
        refused = self.human_git("push", "origin", "main", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("commit not Conventional", refused.stdout + refused.stderr)

    def test_the_kit_source_tree_has_no_adoption_exemption(self):
        # The kit's own repository has no manifest. Staging a forged one there
        # must not buy a task-free commit; the same change set in an installed
        # project is at least judged.
        self.assertTrue(agentctl._is_kit_source_checkout(KIT))
        self.assertFalse(agentctl._is_kit_source_checkout(self.root))
        forged = {".agent/install-manifest.json": "A", "tools/agentctl.py": "M"}
        with mock.patch.object(agentctl, "_commit_changes", return_value=forged):
            self.assertIsNone(agentctl._adoption_commit_problems(KIT, "HEAD"))
            self.assertIsNone(agentctl._adoption_commit_problems(KIT))
            judged = agentctl._adoption_commit_problems(self.root, "HEAD")
        self.assertIsInstance(judged, list)
        self.assertTrue(judged, "a manifest that is not in the commit is a problem, not a pass")

    def test_the_kit_as_the_very_first_commit(self):
        # `git init` then install: no prior HEAD, so no adoption baseline file
        # either, and the install commit is the root commit.
        root = Path(tempfile.mkdtemp(prefix="awk-adopt-root-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "h@example.com"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Human"], check=True)
        install = subprocess.run(
            [sys.executable, str(KIT / "tools" / "agentctl.py"), "init", str(root)],
            cwd=KIT, text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        self.assertFalse((root / ".agent" / "adoption.json").exists())
        env = self.env(TERM_SESSION_ID="w0t0p0")
        add = subprocess.run(
            ["git", "add", *agentctl.ADOPTION_COMMIT_ADD_PATHS], cwd=str(root),
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
        commit = subprocess.run(
            ["git", "commit", "-q", "-m", agentctl.ADOPTION_COMMIT_MESSAGE], cwd=str(root),
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(commit.returncode, 0, commit.stdout + commit.stderr)
        # A brand-new remote: pre-push sees a zero remote sha and judges the
        # whole (one-commit) history.
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(self.bare_origin())], check=True)
        pushed = subprocess.run(
            ["git", "push", "origin", "main"], cwd=str(root),
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(pushed.returncode, 0, pushed.stdout + pushed.stderr)
        self.assertIn("agentctl check (pre-push): OK", pushed.stdout + pushed.stderr)


class WorktreeRefusalHintTest(_AdoptedRepoTestCase):
    def test_dirty_planning_checkout_names_the_way_out(self):
        # Before the adoption commit the install itself is the dirt: named.
        refused = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--type", "code",
            "--title", "tokenizer", "--scope", "src/tok/", expect=1, session="conv-a",
        )
        self.assertIn("requires a clean planning checkout", refused.stderr)
        self.assertIn("preserve or commit these first: .agent/", refused.stderr)
        self.assertIn(".agent/install-manifest.json", refused.stderr)
        self.assertNotIn("agentctl sync", refused.stderr)

        # After it, the usual dirt is ledger data another conversation has
        # not published yet; `sync` is the answer and the hint says so.
        self.adopt_and_push()
        progress = self.root / ".agent" / "logs" / "progress.md"
        progress.write_text(progress.read_text(encoding="utf-8") + "- note\n", encoding="utf-8")
        refused = self.agentctl(
            "work", "--agent", "codex", "--auto-create", "--type", "code",
            "--title", "tokenizer", "--scope", "src/tok/", expect=1, session="conv-a",
        )
        self.assertIn("only ledger data is uncommitted; run 'agentctl sync'", refused.stderr)
        board = json.loads((self.root / ".agent" / "board.json").read_text(encoding="utf-8"))
        self.assertFalse([t for t in board["tasks"].values() if t.get("title") == "tokenizer"])


class SecondCloneTest(_AdoptedRepoTestCase):
    """A fresh clone of an adopted project must not work unguarded.

    `core.hooksPath` and the merge driver are clone-local Git config that
    `git clone` does not carry. Until now a second machine started work with
    the hooks checked out but not active, and nothing said so.
    """

    def setUp(self):
        super().setUp()
        self.adopt_and_push()
        self.clone = Path(tempfile.mkdtemp(prefix="awk-adopt-clone-"))
        self.addCleanup(shutil.rmtree, self.clone, ignore_errors=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True)
        for key, value in (("user.email", "m2@example.com"), ("user.name", "Machine Two")):
            subprocess.run(["git", "-C", str(self.clone), "config", key, value], check=True)

    def clone_git(self, *args, env=None, check=True):
        proc = subprocess.run(
            ["git", *args], cwd=str(self.clone), text=True, capture_output=True,
            timeout=120, env=env or self.env(TERM_SESSION_ID="w0t0p0"),
        )
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def clone_agentctl(self, *args, expect=0, session=None, **extra):
        proc = subprocess.run(
            [sys.executable, "tools/agentctl.py", *args],
            cwd=str(self.clone), env=self.env(session, **extra), text=True,
            capture_output=True, timeout=120,
        )
        self.assertEqual(
            proc.returncode, expect,
            f"args={args}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        return proc

    def test_first_work_wires_the_hooks_and_merge_driver(self):
        self.assertEqual(self.clone_git("config", "--get", "core.hooksPath", check=False).stdout, "")
        self.assertEqual(
            self.clone_git("config", "--get", "merge.agent-ledger.driver", check=False).stdout, "",
        )
        doctor = json.loads(self.clone_agentctl("doctor", "--json", expect=1).stdout)
        self.assertFalse(doctor["ok"])

        started = self.clone_agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--title", "collect on machine two", "--scope", "src/collect/", session="conv-m2",
        )
        self.assertIn("wired this clone's git hooks", started.stdout)
        self.assertIn("registered the 'agent-ledger' merge driver", started.stdout)
        self.assertEqual(self.clone_git("config", "--get", "core.hooksPath").stdout.strip(), ".githooks")
        self.assertIn("merge-driver", self.clone_git("config", "--get", "merge.agent-ledger.driver").stdout)

        # The hooks are live: a human's unclaimed commit is refused here too.
        (self.clone / "stray.txt").write_text("x\n", encoding="utf-8")
        self.clone_git("add", "stray.txt")
        refused = self.clone_git("commit", "-q", "-m", "chore: stray", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("no active task", refused.stdout + refused.stderr)

        # Wiring happens once; a second session says nothing about it.
        again = self.clone_agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--title", "more", "--scope", "src/more/", session="conv-m3",
        )
        self.assertNotIn("wired this clone", again.stdout)

    def test_a_foreign_hooks_path_is_refused_not_overridden(self):
        self.clone_git("config", "core.hooksPath", ".husky")
        refused = self.clone_agentctl(
            "work", "--agent", "codex", "--auto-create",
            "--title", "collect", "--scope", "src/collect/", expect=2, session="conv-m2",
        )
        self.assertIn("core.hooksPath is '.husky'", refused.stderr)
        self.assertIn("agentctl init .", refused.stderr)
        self.assertEqual(self.clone_git("config", "--get", "core.hooksPath").stdout.strip(), ".husky")
        board = json.loads((self.clone / ".agent" / "board.json").read_text(encoding="utf-8"))
        self.assertFalse(
            [t for t in board["tasks"].values() if t.get("title") == "collect"],
            "refusal must happen before any task state is written",
        )


if __name__ == "__main__":
    unittest.main()
