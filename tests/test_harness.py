"""Tests for the replicable-harness plugin: project detection, MCP server, hooks (with graphify)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
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
    env = {k: v for k, v in os.environ.items() if k not in {"LAB_TEST_CMD", "LAB_TEST_GATE", "GRAPHIFY_BIN"}}
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
        self.write(".claude/replicable-harness.json", '{"test": "just check"}')
        self.assertEqual(project.test_command(), "just check")

    def test_state_lives_outside_the_project(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            os.environ.pop("HARNESS_DATA", None)
            data = project.data_dir()
        self.assertTrue(str(data).startswith(str(self.root / "cache" / "replicable-harness")))
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
        self.assertEqual(init["result"]["serverInfo"]["name"], "harness")
        self.assertEqual({t["name"] for t in tools["result"]["tools"]}, {"harness_info", "test_gate"})

    def test_gate_toggle_is_reflected_in_info(self):
        on, info, off = self.rpc(self.call("test_gate", action="on"), self.call("harness_info"),
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
        self.assertIn("/replicable-harness:setup", context)

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


if __name__ == "__main__":
    unittest.main()
