"""Tests for the replicable plugin: project detection, MCP server, hooks (with graphify)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "harness"))
import project  # noqa: E402

HAS_GRAPHIFY = shutil.which("graphify") is not None
needs_graphify = unittest.skipUnless(HAS_GRAPHIFY, "graphify is not installed (uv tool install graphifyy)")

FIXTURE = {
    "pkg/__init__.py": "",
    "pkg/util.py": '''
        def clean(value):
            return value.strip()
    ''',
    "pkg/service.py": '''
        from pkg.util import clean

        def run(x):
            return clean(x)
    ''',
    "tests/test_service.py": '''
        from pkg.service import run

        def test_run():
            assert run(" x ") == "x"
    ''',
}


def make_repo(files=FIXTURE, commit=False) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip())
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    if commit:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init"],
                       cwd=root, check=True)
    return tmp


def env_for(root: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in {"LAB_TEST_CMD", "LAB_TEST_GATE", "GRAPHIFY_BIN", "REPLICABLE_TRACE"}}
    env.update(HARNESS_ROOT=str(root), HARNESS_DATA=str(root / ".state"))
    env.update(extra)
    return env


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict(os.environ, env_for(self.root), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, rel, text=""):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_detects_common_test_commands(self):
        self.assertIsNone(project.test_command())
        self.write("go.mod", "module x")
        self.assertEqual(project.test_command(), "go test ./...")
        self.write("package.json", '{"scripts": {"test": "vitest"}}')
        self.write("pnpm-lock.yaml")
        self.assertEqual(project.test_command(), "pnpm test")
        self.write("Makefile", "build:\n\ttrue\ntest:\n\ttrue\n")
        self.assertEqual(project.test_command(), "make test")

    def test_python_prefers_pytest_when_configured(self):
        self.write("tests/test_x.py")
        self.assertTrue(project.test_command().endswith("-m unittest discover -s tests"))
        self.write("pyproject.toml", "[tool.pytest.ini_options]\n")
        self.assertTrue(project.test_command().endswith("-m pytest -q"))

    def test_config_overrides_detection(self):
        self.write("go.mod")
        self.write(".claude/replicable.json", '{"test": "just check"}')
        self.assertEqual(project.test_command(), "just check")

    def test_pre_0_6_config_name_is_still_read_until_renamed(self):
        self.write(".claude/replicable-harness.json", '{"test": "old name"}')
        self.assertEqual(project.test_command(), "old name")
        self.assertTrue(project.info()["config_file"].endswith("replicable-harness.json"))
        self.write(".claude/replicable.json", '{"test": "new name"}')
        self.assertEqual(project.test_command(), "new name")

    def test_state_lives_outside_the_project(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            os.environ.pop("HARNESS_DATA", None)
            data = project.data_dir()
        self.assertTrue(str(data).startswith(str(self.root / "cache" / "replicable")))
        self.assertNotEqual(data, project.data_dir(self.root / "other"))

    def test_detects_graphify_claude_integration(self):
        self.assertFalse(project.graphify_claude_integration())
        self.write("CLAUDE.md", "# x\n\n## graphify\nrules\n")
        self.write(".claude/settings.json", '{"hooks": {"PreToolUse": [{"command": "graphify hook-guard search"}]}}')
        self.assertTrue(project.graphify_claude_integration())


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_repo()
        self.addCleanup(self.tmp.cleanup)
        self.env = env_for(Path(self.tmp.name))

    def rpc(self, *messages):
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run([sys.executable, str(PLUGIN / "harness" / "mcp_server.py")], input=stdin,
                             capture_output=True, text=True, env=self.env, timeout=30, check=True).stdout
        return [json.loads(line) for line in out.splitlines()]

    def call(self, tool, **arguments):
        return {"jsonrpc": "2.0", "id": tool, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}

    def test_handshake_lists_only_harness_tools(self):
        init, tools = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        self.assertEqual(init["result"]["serverInfo"]["name"], "replicable")
        self.assertEqual({t["name"] for t in tools["result"]["tools"]}, {"info", "test_gate"})

    def test_mcp_json_registers_the_server_under_its_name(self):
        servers = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
        self.assertEqual(list(servers), [project.MCP_SERVER])
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], project.PLUGIN)

    def test_gate_toggle_is_reflected_in_info(self):
        on, info, off = self.rpc(self.call("test_gate", action="on"), self.call("info"),
                                 self.call("test_gate", action="off"))
        self.assertIn("test-gate is ON", on["result"]["content"][0]["text"])
        data = json.loads(info["result"]["content"][0]["text"])
        self.assertTrue(data["test_gate"])
        self.assertEqual(data["project_root"], str(Path(self.tmp.name).resolve()))
        self.assertIn("graphify", data)
        self.assertIn("test-gate is OFF", off["result"]["content"][0]["text"])

    def test_errors_do_not_crash_the_server(self):
        bad_action, unknown = self.rpc(self.call("test_gate", action="maybe"), self.call("nope"))
        self.assertTrue(bad_action["result"]["isError"])
        self.assertEqual(unknown["error"]["code"], -32602)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_repo()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = env_for(self.root, LAB_TEST_GATE="1")

    def hook(self, name, event, **env):
        res = subprocess.run([sys.executable, str(PLUGIN / "hooks" / name)], input=json.dumps(event),
                             capture_output=True, text=True, env=dict(self.env, **env), timeout=180)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout) if res.stdout.strip() else None

    def without_graphify(self):
        path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                               if not (Path(p) / "graphify").exists())
        return {"PATH": path}

    def test_session_start_explains_how_to_install_graphify(self):
        out = self.hook("session_start.py", {}, **self.without_graphify())
        self.assertIn("graphify is not installed", out["hookSpecificOutput"]["additionalContext"])

    def test_post_edit_is_silent_without_graphify_and_for_docs(self):
        self.assertIsNone(self.hook("post_edit.py", {"tool_input": {"file_path": str(self.root / "pkg" / "util.py")}},
                                    **self.without_graphify()))
        self.assertIsNone(self.hook("post_edit.py", {"tool_input": {"file_path": str(self.root / "README.md")}}))

    @needs_graphify
    def test_session_start_builds_the_graph_and_suggests_setup(self):
        out = self.hook("session_start.py", {})
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertTrue((self.root / "graphify-out" / "graph.json").exists())
        self.assertIn("built graphify-out/graph.json", context)
        self.assertIn("/replicable:setup", context)

    @needs_graphify
    def test_post_edit_reports_blast_radius_and_tests_from_graphify(self):
        (self.root / "pkg" / "util.py").write_text("def clean(value):\n    return value.strip().lower()\n")
        out = self.hook("post_edit.py", {"tool_name": "Edit", "tool_input": {"file_path": str(self.root / "pkg" / "util.py")}})
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("graphify: editing pkg/util.py", context)
        self.assertIn("Tests to run: tests/test_service.py", context)

    def test_test_gate_blocks_on_failure_then_gives_up_at_the_cap(self):
        decisions = [self.hook("test_gate.py", {}, LAB_TEST_CMD="false", LAB_TEST_GATE_MAX="2") for _ in range(3)]
        self.assertEqual([d.get("decision") for d in decisions], ["block", "block", None])
        self.assertIn("attempt 2/2", decisions[1]["reason"])
        self.assertIn("handing back", decisions[2]["systemMessage"])

    def test_test_gate_passes_and_is_off_without_opt_in(self):
        self.assertIn("pass", self.hook("test_gate.py", {}, LAB_TEST_CMD="true")["systemMessage"])
        self.assertIsNone(self.hook("test_gate.py", {}, LAB_TEST_CMD="false", LAB_TEST_GATE=""))


class TestGateChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_repo(commit=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        state = tempfile.TemporaryDirectory()  # state outside the repo, as in real use
        self.addCleanup(state.cleanup)
        self.env = env_for(self.root, LAB_TEST_GATE="1", LAB_TEST_CMD="false", HARNESS_DATA=state.name)

    def gate(self):
        res = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "test_gate.py")], input="{}",
                             capture_output=True, text=True, env=self.env, timeout=60)
        return json.loads(res.stdout) if res.stdout.strip() else None

    def test_ignores_docs_and_graphify_output(self):
        (self.root / "README.md").write_text("docs")
        (self.root / "graphify-out").mkdir()
        (self.root / "graphify-out" / "graph.json").write_text("{}")
        self.assertIsNone(self.gate())

    def test_runs_on_code_changes(self):
        (self.root / "pkg" / "util.py").write_text("def clean(v):\n    return v\n")
        self.assertEqual(self.gate()["decision"], "block")


FAKE_GRAPHIFY = '''#!/usr/bin/env python3
import os, sys
mode = os.environ.get("FAKE_GRAPHIFY", "hits")
if sys.argv[1] == "update":
    sys.exit(1 if mode == "fail_update" else 0)
if mode == "not_modeled":
    sys.exit(3)
if mode == "hits":
    print("- clean pkg/util.py:L1")
    print("- run pkg/service.py:L3")
    print("- test_run tests/test_service.py:L3")
'''


class HookTraceTests(unittest.TestCase):
    """REPLICABLE_TRACE records every hook decision with a reason code; without it nothing is written."""

    def setUp(self):
        self.tmp = make_repo(commit=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.state = Path(state.name)
        self.trace_file = self.state / "out" / "trace.jsonl"
        fake = self.state / "graphify"
        fake.write_text(FAKE_GRAPHIFY)
        fake.chmod(0o755)
        self.fake_graphify = str(fake)
        self.env = env_for(self.root, HARNESS_DATA=str(self.state / "data"), REPLICABLE_TRACE=str(self.trace_file))

    def run_hook(self, name, event=None, **env):
        return subprocess.run([sys.executable, str(PLUGIN / "hooks" / name)],
                              input=json.dumps(event if event is not None else {}),
                              capture_output=True, text=True, env=dict(self.env, **env), timeout=60)

    def traced(self, name, event=None, **env):
        """Run a hook and return its single trace row."""
        before = self.rows()
        res = self.run_hook(name, event, **env)
        self.assertEqual(res.returncode, 0, res.stderr)
        new = self.rows()[len(before):]
        self.assertEqual(len(new), 1, new)
        return new[0]

    def rows(self):
        if not self.trace_file.exists():
            return []
        return [json.loads(line) for line in self.trace_file.read_text().splitlines()]

    def no_graphify(self):
        path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                               if not (Path(p) / "graphify").exists())
        return {"PATH": path}

    def edit_event(self, rel):
        return {"session_id": "s1", "tool_use_id": "toolu_1", "hook_event_name": "PostToolUse",
                "tool_name": "Edit", "tool_input": {"file_path": str(self.root / rel)}}

    def test_rows_carry_identity_and_timing(self):
        row = self.traced("post_edit.py", self.edit_event("README.md"))
        self.assertEqual((row["hook"], row["outcome"], row["reason"]), ("post_edit", "skip", "docs"))
        self.assertEqual((row["session_id"], row["tool_use_id"], row["event"]), ("s1", "toolu_1", "PostToolUse"))
        self.assertEqual(row["plugin_version"], project.plugin_version())
        self.assertIsInstance(row["duration_ms"], int)

    def test_session_start_paths(self):
        self.assertEqual(self.traced("session_start.py", **self.no_graphify())["reason"], "no_graphify")
        row = self.traced("session_start.py", GRAPHIFY_BIN=self.fake_graphify)
        self.assertEqual((row["outcome"], row["reason"]), ("context", "built,no_integration"))
        self.assertGreater(row["chars"], 0)
        row = self.traced("session_start.py", GRAPHIFY_BIN=self.fake_graphify, FAKE_GRAPHIFY="fail_update")
        self.assertEqual(row["reason"], "build_failed,no_integration")

    def test_post_edit_paths(self):
        fake = {"GRAPHIFY_BIN": self.fake_graphify}
        self.assertEqual(self.traced("post_edit.py", {})["reason"], "no_path")
        self.assertEqual(self.traced("post_edit.py", self.edit_event("pkg/util.py"), **self.no_graphify())["reason"],
                         "no_graphify")
        outside = {"tool_input": {"file_path": "/elsewhere/x.py"}}
        self.assertEqual(self.traced("post_edit.py", outside, **fake)["reason"], "outside_root")
        for mode, outcome, reason in (("fail_update", "context", "update_failed"), ("not_modeled", "skip", "not_modeled"),
                                      ("no_hits", "skip", "no_hits"), ("hits", "context", "reported")):
            row = self.traced("post_edit.py", self.edit_event("pkg/util.py"), FAKE_GRAPHIFY=mode, **fake)
            self.assertEqual((row["outcome"], row["reason"]), (outcome, reason), mode)
        self.assertEqual((row["symbols"], row["files"], row["tests"]), (3, 3, 1))

    def test_test_gate_paths(self):
        self.assertEqual(self.traced("test_gate.py")["reason"], "off")
        gate = {"LAB_TEST_GATE": "1"}
        self.assertEqual(self.traced("test_gate.py", **gate)["reason"], "no_code_changes")
        (self.root / "pkg" / "util.py").write_text("def clean(v):\n    return v\n")
        row = self.traced("test_gate.py", LAB_TEST_CMD="true", **gate)
        self.assertEqual((row["outcome"], row["reason"], row["after_blocks"]), ("pass", "tests_pass", 0))
        failing = dict(gate, LAB_TEST_CMD="false", LAB_TEST_GATE_MAX="1")
        row = self.traced("test_gate.py", **failing)
        self.assertEqual((row["outcome"], row["attempt"], row["limit"]), ("block", 1, 1))
        row = self.traced("test_gate.py", **failing)
        self.assertEqual((row["outcome"], row["reason"]), ("gave_up", "cap_hit"))

    def test_test_gate_without_a_command(self):
        tmp = make_repo({"main.c": "int main(){}\n"}, commit=True)
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "main.c").write_text("int main(){return 1;}\n")
        row = self.traced("test_gate.py", HARNESS_ROOT=tmp.name, LAB_TEST_GATE="1")
        self.assertEqual((row["outcome"], row["reason"]), ("skip", "no_command"))

    def test_wiki_hook_paths(self):
        self.assertEqual(self.traced("wiki_flag.py", self.edit_event("pkg/util.py"))["reason"], "no_wiki")
        self.assertEqual(self.traced("wiki_reminder.py")["reason"], "no_wiki")
        for rel, body in WIKI_FIXTURE.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(body).lstrip())
        self.assertEqual(self.traced("wiki_reminder.py")["reason"], "nothing_pending")
        row = self.traced("wiki_flag.py", self.edit_event("src/billing.py"))
        self.assertEqual((row["outcome"], row["reason"], row["pages"]), ("context", "flagged", 1))
        self.assertEqual(self.traced("wiki_flag.py", self.edit_event("src/billing.py"))["reason"],
                         "not_covered_or_already_flagged")
        self.assertEqual(self.traced("wiki_reminder.py")["reason"], "pending")
        self.assertEqual(self.traced("wiki_flag.py", self.edit_event("wiki/domain/invoices.md"))["reason"], "page_edited")

    def test_crashes_are_recorded_by_type_only(self):
        (self.root / "pkg" / "util.py").write_text("x = 1\n")
        res = self.run_hook("test_gate.py", LAB_TEST_GATE="1", LAB_TEST_GATE_MAX="not-a-number")
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.rows()[-1]["outcome"], "error")
        self.assertEqual(self.rows()[-1]["reason"], "exception:ValueError")

    def test_off_by_default_and_1_writes_to_the_data_dir(self):
        env = dict(self.env)
        env.pop("REPLICABLE_TRACE")
        res = subprocess.run([sys.executable, str(PLUGIN / "hooks" / "test_gate.py")], input="{}",
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.trace_file.exists())
        self.assertFalse((self.state / "data" / "trace.jsonl").exists())
        self.run_hook("test_gate.py", REPLICABLE_TRACE="1")
        rows = (self.state / "data" / "trace.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(rows[0])["reason"], "off")

    def test_an_unwritable_trace_changes_nothing(self):
        (self.root / "pkg" / "util.py").write_text("x = 1\n")
        env = {"LAB_TEST_GATE": "1", "LAB_TEST_CMD": "false"}
        res = self.run_hook("test_gate.py", REPLICABLE_TRACE="/dev/null/not/a/dir/trace.jsonl", **env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["decision"], "block")
        self.assertEqual(res.stderr, "")


import wiki  # noqa: E402

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]

WIKI_FIXTURE = {
    "src/billing.py": "def issue():\n    pass\n",
    "wiki/overview.md": '''
        ---
        type: overview
        summary: What the system is for.
        updated: 2026-01-10
        ---
        # Overview

        Billing rules live in [Invoices](domain/invoices.md); see also
        [the credit-note decision](decisions/0001-credit-notes.md) and [traps](traps.md).
    ''',
    "wiki/domain/invoices.md": '''
        ---
        type: domain
        summary: "Invoices are immutable once issued: corrections are credit notes."
        code:
          - src/billing.py
        sources: [raw/2026-01-03-billing-call.md, "https://github.com/org/repo/pull/1"]
        updated: 2026-01-10
        tags: [billing]
        ---
        # Invoices

        Back to the [overview](../overview.md). Code: `src/billing.py`.

        ```
        [[not a wikilink]] and [not a link](nowhere.md) inside a code block
        ```
    ''',
    "wiki/decisions/0001-credit-notes.md": '''
        ---
        type: decision
        summary: Corrections are credit notes, not edits.
        status: accepted
        updated: 2026-01-10
        ---
        # Credit notes

        Applies to [invoices](../domain/invoices.md).
    ''',
    "wiki/traps.md": '''
        ---
        type: trap
        summary: Known pitfalls.
        updated: 2026-01-10
        ---
        # Traps

        ## Rounding

        See [invoices](domain/invoices.md#rules).
    ''',
    "wiki/raw/2026-01-03-billing-call.md": "Notes from the billing call.\n",
    "wiki/log.md": "# Log\n\n## [2026-01-10] ingest | billing call\n- added domain/invoices.md\n",
}


class WikiTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        for rel, body in WIKI_FIXTURE.items():
            self.write(rel, textwrap.dedent(body).lstrip())
        wiki.write_index(wiki.load(self.root))
        self.git("init", "-q", "-b", "main")
        self.commit("init", date="2026-01-10T12:00:00")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def git(self, *args, **env):
        return subprocess.run(GIT + list(args), cwd=self.root, check=True, capture_output=True, text=True,
                              env=dict(os.environ, **env)).stdout

    def commit(self, message, date="2026-01-10T12:00:00"):
        self.git("add", "-A")
        self.git("commit", "-qm", message, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)

    def cli(self, *args, root=None):
        res = subprocess.run([sys.executable, str(PLUGIN / "harness" / "wiki.py"), "--root", str(root or self.root),
                              *args], capture_output=True, text=True, timeout=60)
        return res.returncode, res.stdout + res.stderr

    def assertCheck(self, *expected, code=1, args=()):
        rc, out = self.cli("check", *args)
        self.assertEqual(rc, code, out)
        for text in expected:
            self.assertIn(text, out)
        return out

    def test_a_valid_wiki_passes_strict_check(self):
        out = self.assertCheck("0 error(s), 0 warning(s) in 4 page(s)", code=0, args=["--strict"])
        self.assertNotIn("nowhere.md", out)

    def test_index_is_generated_grouped_by_type(self):
        index = (self.root / "wiki" / "index.md").read_text()
        self.assertIn("## Domain\n\n- [Invoices](domain/invoices.md): Invoices are immutable once issued: "
                      "corrections are credit notes.", index)
        self.assertLess(index.index("## Overview"), index.index("## Decisions"))
        self.write("wiki/index.md", index + "- [Hand-written](x.md): nope\n")
        self.assertCheck("index.md: out of date")
        self.assertEqual(self.cli("index")[0], 0)
        self.assertEqual(self.cli("check")[0], 0)

    def test_frontmatter_errors(self):
        page = self.root / "wiki" / "domain" / "invoices.md"
        good = page.read_text()
        cases = {
            "# no frontmatter\n": "missing frontmatter",
            good.replace("summary: \"Invoices", "summary: Invoices").replace('notes."', "notes."): "must be quoted",
            good.replace("type: domain", "type: rule"): "type must be one of",
            good.replace("type: domain", "type: decision"): "belongs in wiki/decisions/",
            good.replace("updated: 2026-01-10", "updated: soon"): "updated must be a date",
            good.replace("Invoices are immutable", "x" * 121): "at most 120",
            good.replace("  - src/billing.py", "  - src/gone.py"): "code path does not exist",
            good.replace("raw/2026-01-03", "raw/2026-01-04"): "source does not exist",
        }
        for text, expected in cases.items():
            with self.subTest(expected=expected):
                page.write_text(text)
                self.cli("index")
                self.assertCheck("wiki/domain/invoices.md", expected)

    def test_decision_status_and_supersession(self):
        page = self.root / "wiki" / "decisions" / "0001-credit-notes.md"
        good = page.read_text()
        page.write_text(good.replace("status: accepted", "status: maybe"))
        self.assertCheck("decision status must be one of")
        page.write_text(good.replace("status: accepted", "status: superseded"))
        self.assertCheck("superseded decision without superseded_by", code=0)
        page.write_text(good.replace("status: accepted", "status: superseded\nsuperseded_by: 0002-x.md"))
        self.assertCheck("superseded_by does not exist")

    def test_links_must_be_relative_markdown_links_that_resolve(self):
        page = self.root / "wiki" / "decisions" / "0001-credit-notes.md"
        good = page.read_text()
        for extra, expected in [("[gone](../domain/gone.md)", "broken link: ../domain/gone.md"),
                                ("[[invoices]]", "[[wikilinks]] don't render on GitHub"),
                                ("[code](../../src/billing.py)", "link leaves the wiki"),
                                ("[abs](/wiki/overview.md)", "link must be relative")]:
            with self.subTest(expected=expected):
                page.write_text(good + "\n" + extra + "\n")
                self.assertIn(":11: ", self.assertCheck(expected))
        page.write_text(good + "\nInline `[[fine]]`, [web](https://example.com), [top](#credit-notes).\n")
        self.assertCheck(code=0, args=["--strict"])

    def test_file_names_follow_the_convention(self):
        for rel, expected in [("wiki/domain/Bad Name.md", "kebab-case"),
                              ("wiki/decisions/credit-notes.md", "numbered kebab-case"),
                              ("wiki/raw/billing-call.md", "raw/YYYY-MM-DD-short-title.md")]:
            with self.subTest(rel=rel):
                self.write(rel, "x\n")
                self.assertCheck(expected)
                (self.root / rel).unlink()

    def test_raw_sources_are_immutable(self):
        raw = self.root / "wiki" / "raw" / "2026-01-03-billing-call.md"
        raw.write_text("Edited notes.\n")
        self.assertCheck("wiki/raw/2026-01-03-billing-call.md: raw sources are immutable")
        self.commit("edit raw")
        self.assertCheck("raw sources are immutable")

    def test_likely_secrets_in_raw_warn_without_printing_them(self):
        secret = "ghp_" + "a1B2" * 9
        self.write("wiki/raw/2026-01-05-pasted-thread.md", f"ok\ntoken: {secret}\n")
        out = self.assertCheck("2026-01-05-pasted-thread.md:2: likely secret", code=0)
        self.assertNotIn(secret, out)
        self.assertEqual(self.cli("check", "--strict")[0], 1)

    def test_long_and_orphan_pages_warn(self):
        self.assertEqual(self.cli("new", "domain", "Refunds", "--summary", "How refunds work.")[0], 0)
        self.assertCheck("wiki/domain/refunds.md: orphan", code=0)
        page = self.root / "wiki" / "domain" / "invoices.md"
        page.write_text(page.read_text() + "line\n" * 150)
        self.assertCheck("wiki/domain/invoices.md: page has", code=0)

    def test_stale_pages_are_listed_and_warned(self):
        self.write("src/billing.py", "def issue():\n    return 1\n")
        self.commit("change billing", date="2026-02-01T12:00:00")
        rc, out = self.cli("stale")
        self.assertEqual(rc, 0)
        self.assertIn("wiki/domain/invoices.md (updated 2026-01-10): src/billing.py changed 2026-02-01", out)
        self.assertCheck("stale: code changed after updated 2026-01-10", code=0)
        self.assertCheck("stale", args=["--strict"])

    def test_base_fails_when_code_changes_without_its_page(self):
        self.git("checkout", "-qb", "feature")
        self.write("src/billing.py", "def issue():\n    return 2\n")
        self.commit("change billing")
        self.assertCheck("its code changed since main but the page did not: src/billing.py", args=["--base", "main"])
        self.assertEqual(self.cli("check")[0], 0)  # without --base it is not an error
        page = self.root / "wiki" / "domain" / "invoices.md"
        page.write_text(page.read_text().replace("Back to", "Checked again. Back to"))
        self.assertCheck(code=0, args=["--base", "main"])  # uncommitted page changes count
        self.assertCheck("no merge base", args=["--base", "nope"])

    def test_shallow_clones_warn_instead_of_guessing(self):
        clone = self.root.parent / (self.root.name + "-clone")
        self.addCleanup(shutil.rmtree, clone, True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", self.root.as_uri(), str(clone)], check=True)
        rc, out = self.cli("check", root=clone)
        self.assertEqual(rc, 0, out)
        self.assertIn("shallow git clone", out)

    def test_new_creates_valid_pages_and_updates_the_index(self):
        self.assertEqual(self.cli("new", "decision", "Use Postgres!", "--code", "src/billing.py")[0], 0)
        self.assertEqual(self.cli("new", "decision", "Second one")[0], 0)
        first = self.root / "wiki" / "decisions" / "0002-use-postgres.md"
        self.assertTrue(first.exists())
        self.assertTrue((self.root / "wiki" / "decisions" / "0003-second-one.md").exists())
        meta, _ = wiki.parse_frontmatter(first.read_text())
        self.assertEqual((meta["type"], meta["status"], meta["code"]), ("decision", "proposed", ["src/billing.py"]))
        self.assertIn("decisions/0002-use-postgres.md", (self.root / "wiki" / "index.md").read_text())
        self.assertIn("0 error(s)", self.cli("check")[1])
        rc, out = self.cli("new", "domain", "Invoices")
        self.assertEqual(rc, 1)
        self.assertIn("already exists", out)

    def test_new_trap_appends_a_section(self):
        self.assertEqual(self.cli("new", "trap", "Timezones", "--code", "src/billing.py")[0], 0)
        text = (self.root / "wiki" / "traps.md").read_text()
        self.assertIn("## Rounding", text)
        self.assertIn("## Timezones\n\n- **Symptom:** TODO", text)
        self.assertEqual(wiki.parse_frontmatter(text)[0]["code"], ["src/billing.py"])
        self.assertCheck(code=0, args=["--strict"])

    def test_new_in_an_empty_project_creates_the_wiki(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.assertIn("nothing to do", self.cli("check", root=empty.name)[1])
        self.assertEqual(self.cli("new", "overview", "My system", root=empty.name)[0], 0)
        self.assertIn("- [My system](overview.md): TODO", (Path(empty.name) / "wiki" / "index.md").read_text())

    def test_covers_lists_pages_for_files_and_directories(self):
        traps = self.root / "wiki" / "traps.md"
        traps.write_text(traps.read_text().replace("updated:", "code: [src]\nupdated:"))
        rc, out = self.cli("covers", "src/billing.py", str(self.root / "src" / "billing.py"), "/elsewhere/x.py",
                           "README.md")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.splitlines(), ["wiki/domain/invoices.md: src/billing.py",
                                            "wiki/traps.md: src/billing.py"])
        self.assertEqual(self.cli("covers", "docs/x.md"), (0, ""))

    def test_log_appends_a_parseable_entry(self):
        rc, _ = self.cli("log", "ingest", "PR #7 refunds", "--line", "updated domain/invoices.md", "--line", "added x")
        self.assertEqual(rc, 0)
        text = (self.root / "wiki" / "log.md").read_text()
        today = wiki.dt.date.today().isoformat()
        self.assertTrue(text.endswith(f"- added domain/invoices.md\n\n## [{today}] ingest | PR #7 refunds\n"
                                      "- updated domain/invoices.md\n- added x\n"), text)
        self.assertNotEqual(self.cli("log", "ingest", "two\nlines")[0], 0)
        self.assertNotEqual(self.cli("log", "rewrite", "x")[0], 0)
        self.assertEqual(self.cli("check")[0], 0)

    def test_frontmatter_subset_round_trips(self):
        meta = {"type": "domain", "summary": "Has: a colon, and 'quotes'", "code": ["a/b.py", "c d"],
                "sources": [], "updated": "2026-01-01", "superseded_by": None}
        parsed, start = wiki.parse_frontmatter(wiki.dump_frontmatter(meta) + "body\n")
        self.assertEqual(parsed, meta)
        block, _ = wiki.parse_frontmatter("---\n# comment\ntags:\n  - a\n  - 'b c'\nx: plain # note\n---\n")
        self.assertEqual(block, {"tags": ["a", "b c"], "x": "plain"})
        for bad in ("---\nx: {a: 1}\n---\n", "---\n  - orphan\n---\n", "---\nx: 1\n", "---\nx: 1\nx: 2\n---\n"):
            with self.subTest(bad=bad), self.assertRaises(wiki.FrontmatterError):
                wiki.parse_frontmatter(bad)


class WikiHookTests(unittest.TestCase):
    def setUp(self):
        tmp, state = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(state.cleanup)
        self.root = Path(tmp.name).resolve()
        for rel, body in WIKI_FIXTURE.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(body).lstrip())
        self.state = Path(state.name)
        self.env = env_for(self.root, HARNESS_DATA=state.name)

    def hook(self, name, event):
        res = subprocess.run([sys.executable, str(PLUGIN / "hooks" / name)],
                             input=event if isinstance(event, str) else json.dumps(event),
                             capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"] if res.stdout.strip() else None

    def edit(self, rel):
        return self.hook("wiki_flag.py", {"tool_name": "Edit", "tool_input": {"file_path": str(self.root / rel)}})

    def test_editing_described_code_names_the_page_once(self):
        out = self.edit("src/billing.py")
        self.assertIn("wiki: src/billing.py is described by wiki/domain/invoices.md", out)
        self.assertIn("wiki-ingest", out)
        self.assertIsNone(self.edit("src/billing.py"))  # already flagged: no repeat
        flags = json.loads((self.state / "wiki-flags.json").read_text())
        self.assertEqual(flags["wiki/domain/invoices.md"]["files"], ["src/billing.py"])
        self.assertFalse((self.root / ".state").exists())  # state stays out of the repository

    def test_silent_for_uncovered_files_outside_projects_without_a_wiki_and_bad_input(self):
        self.assertIsNone(self.edit("README.md"))
        self.assertIsNone(self.hook("wiki_flag.py", {"tool_input": {"file_path": "/elsewhere/x.py"}}))
        self.assertIsNone(self.hook("wiki_flag.py", "not json"))
        self.assertIsNone(self.hook("wiki_reminder.py", "{}"))
        shutil.rmtree(self.root / "wiki")
        self.assertIsNone(self.edit("src/billing.py"))

    def test_session_start_reminds_until_the_page_is_updated(self):
        self.edit("src/billing.py")
        out = self.hook("wiki_reminder.py", {"source": "startup"})
        self.assertIn("1 page(s) may be out of date", out)
        self.assertIn("- wiki/domain/invoices.md (src/billing.py)", out)
        self.assertIn("/replicable:wiki-lint", out)
        self.edit("wiki/domain/invoices.md")  # editing the page clears its flag
        self.assertIsNone(self.hook("wiki_reminder.py", {}))

    def test_pages_changed_outside_claude_are_dropped(self):
        self.edit("src/billing.py")
        page = self.root / "wiki" / "domain" / "invoices.md"
        later = time.time() + 60
        os.utime(page, (later, later))
        self.assertIsNone(self.hook("wiki_reminder.py", {}))
        self.assertEqual(json.loads((self.state / "wiki-flags.json").read_text()), {})

    def test_reminder_is_capped(self):
        flags = {f"wiki/domain/p{i}.md": {"files": ["src/billing.py"], "since": 4e9} for i in range(7)}
        for key in flags:
            (self.root / key).write_text("x\n")
        (self.state / "wiki-flags.json").write_text(json.dumps(flags))
        out = self.hook("wiki_reminder.py", {})
        self.assertIn("7 page(s)", out)
        self.assertIn("- ... and 2 more", out)
        self.assertLessEqual(len(out.splitlines()), 8)


class WikiSkillTests(unittest.TestCase):
    """The skills drive the CLI by name; keep them in step with it."""

    SKILLS = ("wiki-ingest", "wiki-lint")

    def test_skills_are_well_formed_and_only_use_existing_cli_commands(self):
        commands = set(re.findall(r'add_parser\("(\w+)"', (PLUGIN / "harness" / "wiki.py").read_text()))
        self.assertEqual(commands, {"check", "index", "stale", "covers", "log", "new"})
        for name in self.SKILLS:
            with self.subTest(skill=name):
                text = (PLUGIN / "skills" / name / "SKILL.md").read_text()
                meta, _ = wiki.parse_frontmatter(text)
                self.assertEqual(meta["name"], name)
                self.assertTrue(meta["description"])
                used = set(re.findall(r"<scripts\.wiki> (\w+)", text.replace("\n", " ")))
                self.assertTrue(used)
                self.assertLessEqual(used, commands, used - commands)
                self.assertIn("the replicable `info` tool", text)

    def test_claude_section_points_to_the_skills(self):
        section = (PLUGIN / "harness" / "templates" / "claude-wiki-section.md").read_text()
        for name in self.SKILLS:
            self.assertIn(f"`{name}`", section)


import scaffold  # noqa: E402


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.env = dict(os.environ)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_scaffold(self, *args, **env):
        res = subprocess.run([sys.executable, str(PLUGIN / "harness" / "scaffold.py"), "--root", str(self.root), *args],
                             capture_output=True, text=True, timeout=60, env=dict(self.env, **env))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res.stdout

    def test_dry_run_writes_nothing_and_shows_diffs(self):
        out = self.run_scaffold()
        self.assertIn("(not set up) -> " + project.plugin_version(), out)
        self.assertIn("+++ b/CLAUDE.md", out)
        self.assertIn("dry run: nothing written", out)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_apply_creates_a_wiki_that_passes_check_and_is_idempotent(self):
        self.write("CLAUDE.md", "# My project\n\nOwn rules.\n")
        self.write(".gitignore", "node_modules/\n")
        self.write(".claude/replicable.json", '{"test": "make test"}')
        self.run_scaffold("--apply")
        res = subprocess.run([sys.executable, str(PLUGIN / "harness" / "wiki.py"), "--root", str(self.root),
                              "check", "--strict"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout)
        claude = (self.root / "CLAUDE.md").read_text()
        self.assertTrue(claude.startswith("# My project\n\nOwn rules.\n\n<!-- replicable:wiki v2 -->"))
        self.assertIn("## Project wiki", claude)
        self.assertEqual((self.root / ".gitignore").read_text().count("wiki/.obsidian/workspace*.json"), 1)
        self.assertTrue((self.root / "wiki" / "raw" / ".gitkeep").exists())
        config = json.loads((self.root / ".claude" / "replicable.json").read_text())
        self.assertEqual(config, {"test": "make test", "harness_version": project.plugin_version()})
        self.assertEqual(json.loads((self.root / "wiki" / ".obsidian" / "app.json").read_text()), scaffold.OBSIDIAN)
        workflow = (self.root / ".github" / "workflows" / "wiki.yml").read_text()
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn('--base "origin/$BASE"', workflow)  # the base ref goes through env, never into the script
        self.assertIn("https://github.com/replicablelabs/replicable-plugin.git", workflow)
        self.assertIn("everything is up to date", self.run_scaffold())

    def test_upgrade_replaces_only_managed_content(self):
        self.write("CLAUDE.md", "# P\n\n<!-- replicable-harness:wiki v0 -->\nold rules\n<!-- /replicable-harness:wiki -->\n\nAfter.\n")
        self.write("wiki/overview.md", "my own overview\n")
        self.write("wiki/raw/2026-01-01-note.md", "raw\n")
        self.write("wiki/.obsidian/app.json", '{"vimMode": true, "useMarkdownLinks": false}')
        self.write(".github/workflows/wiki.yml", "old\n")
        self.write(".claude/replicable.json", '{"harness_version": "0.0.1"}')
        out = self.run_scaffold("--apply")
        self.assertIn("0.0.1 -> " + project.plugin_version(), out)
        claude = (self.root / "CLAUDE.md").read_text()
        self.assertNotIn("old rules", claude)
        self.assertIn("v2 -->", claude)
        self.assertTrue(claude.endswith("<!-- /replicable:wiki -->\n\nAfter.\n"))
        self.assertEqual((self.root / "wiki" / "overview.md").read_text(), "my own overview\n")
        self.assertEqual((self.root / "wiki" / "raw" / "2026-01-01-note.md").read_text(), "raw\n")
        self.assertEqual(json.loads((self.root / "wiki" / ".obsidian" / "app.json").read_text()),
                         dict(scaffold.OBSIDIAN, vimMode=True))
        self.assertIn("fetch-depth: 0", (self.root / ".github" / "workflows" / "wiki.yml").read_text())

    def test_upgrade_from_pre_0_6_renames_the_config_markers_and_index(self):
        self.write("CLAUDE.md", "# P\n\n<!-- replicable-harness:wiki v1 -->\nold\n<!-- /replicable-harness:wiki -->\n")
        self.write(".gitignore", "a/\n# replicable-harness (managed)\nold/\n# /replicable-harness\nb/\n")
        self.write("wiki/index.md", "# Index\n\n<!-- Generated ... by the replicable-harness wiki CLI -->\n")
        self.write(".claude/replicable-harness.json", '{"test": "make test", "harness_version": "0.5.0"}')
        out = self.run_scaffold()
        self.assertIn("remove .claude/replicable-harness.json: renamed to .claude/replicable.json", out)
        self.assertTrue((self.root / ".claude" / "replicable-harness.json").exists())  # dry run
        self.run_scaffold("--apply")
        self.assertFalse((self.root / ".claude" / "replicable-harness.json").exists())
        config = json.loads((self.root / ".claude" / "replicable.json").read_text())
        self.assertEqual(config, {"test": "make test", "harness_version": project.plugin_version()})
        for rel in ("CLAUDE.md", ".gitignore", "wiki/index.md", ".github/workflows/wiki.yml"):
            self.assertNotIn("replicable-harness", (self.root / rel).read_text(), rel)
        self.assertEqual((self.root / "CLAUDE.md").read_text().count("<!-- replicable:wiki v2 -->"), 1)
        gitignore = (self.root / ".gitignore").read_text()
        self.assertTrue(gitignore.startswith("a/\n# replicable (managed)\n") and gitignore.endswith("# /replicable\nb/\n"))
        self.assertIn("everything is up to date", self.run_scaffold())

    def test_migrations_run_in_order_for_versions_skipped(self):
        ran = []

        def step(version):
            def migrate(root, changes):
                ran.append(version)
                return []
            return migrate

        self.write(".claude/replicable.json", '{"harness_version": "0.0.1"}')
        current = project.plugin_version()
        with patch.dict(scaffold.MIGRATIONS, {"0.0.10": step("0.0.10"), "0.0.2": step("0.0.2"),
                                              "0.0.1": step("0.0.1"), "99.0.0": step("99.0.0")}):
            scaffold.plan(self.root)
        self.assertEqual(ran, ["0.0.2", "0.0.10"])  # numeric order; neither the current nor future versions
        self.write(".claude/replicable.json", '{"harness_version": "99.0.0"}')
        with self.assertRaises(SystemExit) as raised:
            scaffold.plan(self.root)
        self.assertIn(f"newer than the installed plugin ({current})", str(raised.exception))

    def test_committed_credentials_warn_without_printing_them(self):
        self.assertNotIn("WARNING", self.run_scaffold())
        self.write(".claude/settings.json", '{"env": {"ANTHROPIC_API_KEY": "sk-x", "OTHER": "1"}}')
        out = self.run_scaffold()
        self.assertIn("WARNING: .claude/settings.json is committed with the project and sets ANTHROPIC_API_KEY.", out)
        self.assertNotIn("sk-x", out)

    def test_info_lists_the_wiki_scripts_and_versions(self):
        info = project.info(self.root)
        self.assertTrue(info["scripts"]["wiki"].endswith("harness/wiki.py"))
        self.assertTrue(Path(info["scripts"]["scaffold"]).exists())
        self.assertEqual(info["harness_version"], {"project": None, "plugin": project.plugin_version()})


if __name__ == "__main__":
    unittest.main()
