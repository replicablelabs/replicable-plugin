"""Tests for the replicable-harness plugin: project detection, indexer, MCP server, hooks, drift."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "graph"))
sys.path.insert(0, str(LAB / "loops"))
import graph_drift  # noqa: E402
import indexer  # noqa: E402
import mcp_server  # noqa: E402
import project  # noqa: E402

FIXTURE = {
    "pkg/__init__.py": "",
    "pkg/base.py": '''
        """Base types."""
        class Store:
            def save(self, item):
                return self._write(item)
            def _write(self, item):
                return item
    ''',
    "pkg/service.py": '''
        from .base import Store
        from . import util

        class CachedStore(Store):
            def put(self, item):
                return self.save(util.clean(item))

        def run():
            return CachedStore().put("x")

        def run_twice():
            store = CachedStore()
            return store.put("y")
    ''',
    "pkg/util.py": '''
        def clean(value):
            """Strip whitespace."""
            return value.strip()
    ''',
    "tests/test_service.py": '''
        from pkg.service import run
        def test_run():
            assert run() == "x"
    ''',
}


def make_repo(files=FIXTURE) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip())
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return tmp


def edges(graph, kind):
    return {(e["source"], e["target"]) for e in graph["edges"] if e["kind"] == kind}


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.dict(os.environ, {"CODE_GRAPH_ROOT": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("LAB_TEST_CMD", None)

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

    def test_config_overrides_detection_and_excludes_paths(self):
        self.write("go.mod")
        self.write(".claude/replicable-harness.json", '{"test": "just check", "exclude": ["vendor"]}')
        self.write("vendor/lib.py", "def hidden():\n    pass\n")
        self.write("app.py", "def shown():\n    pass\n")
        self.assertEqual(project.test_command(), "just check")
        ids = {n["id"] for n in indexer.build_graph(self.root)["nodes"]}
        self.assertIn("app:shown", ids)
        self.assertNotIn("vendor.lib:hidden", ids)

    def test_state_lives_outside_the_project(self):
        with patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")}):
            os.environ.pop("CODE_GRAPH_DATA", None)
            data = project.data_dir()
        self.assertTrue(str(data).startswith(str(self.root / "cache" / "replicable-harness")))
        self.assertNotEqual(data, project.data_dir(self.root / "other"))


class IndexerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_repo()
        self.addCleanup(self.tmp.cleanup)
        self.graph = indexer.build_graph(Path(self.tmp.name))

    def test_resolves_relative_imports_inheritance_and_method_calls(self):
        self.assertIn(("pkg.service", "pkg.base"), edges(self.graph, "imports"))
        self.assertIn(("pkg.service", "pkg.util"), edges(self.graph, "imports"))
        self.assertIn(("pkg.service:CachedStore", "pkg.base:Store"), edges(self.graph, "inherits"))
        calls = edges(self.graph, "calls")
        self.assertIn(("pkg.base:Store.save", "pkg.base:Store._write"), calls)  # self.method
        self.assertIn(("pkg.service:CachedStore.put", "pkg.base:Store.save"), calls)  # inherited self.method
        self.assertIn(("pkg.service:CachedStore.put", "pkg.util:clean"), calls)  # module_alias.func
        self.assertIn(("pkg.service:run", "pkg.service:CachedStore.put"), calls)  # Class().method
        self.assertIn(("pkg.service:run_twice", "pkg.service:CachedStore.put"), calls)  # x = Class(); x.method
        self.assertIn(("tests.test_service:test_run", "pkg.service:run"), calls)

    def test_syntax_errors_are_reported_not_fatal(self):
        (Path(self.tmp.name) / "pkg" / "broken.py").write_text("def nope(:\n")
        graph = indexer.build_graph(Path(self.tmp.name))
        self.assertEqual([e["file"] for e in graph["errors"]], ["pkg/broken.py"])
        self.assertIn("pkg.util:clean", {n["id"] for n in graph["nodes"]})


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_repo()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = dict(os.environ, CODE_GRAPH_ROOT=str(root), CODE_GRAPH_DATA=str(root / ".state"))

    def rpc(self, *messages):
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        out = subprocess.run([sys.executable, str(LAB / "graph" / "mcp_server.py")], input=stdin,
                             capture_output=True, text=True, env=self.env, timeout=30, check=True).stdout
        return [json.loads(line) for line in out.splitlines()]

    def call(self, tool, **arguments):
        return {"jsonrpc": "2.0", "id": tool, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}

    def test_handshake_lists_tools_and_ignores_notifications(self):
        replies = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "code-graph")
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertTrue({"overview", "symbol", "impact", "path", "callers"} <= names)

    def test_impact_finds_transitive_callers_and_tests(self):
        [reply] = self.rpc(self.call("impact", targets="pkg/util.py"))
        text = reply["result"]["content"][0]["text"]
        self.assertIn("tests/test_service.py", text.split("Affected symbols")[0])
        self.assertIn("pkg.service:run", text)

    def test_path_and_ambiguity_errors(self):
        path, ambiguous, unknown = self.rpc(
            self.call("path", source="pkg.service:run", target="clean"),
            self.call("symbol", name="test_run"),
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        )
        self.assertIn("pkg.service:CachedStore.put --calls--> pkg.util:clean", path["result"]["content"][0]["text"])
        self.assertFalse(ambiguous["result"]["isError"])
        self.assertEqual(unknown["error"]["code"], -32602)

    def test_gate_info_and_drift_tools(self):
        on, info, first, second = self.rpc(
            self.call("test_gate", action="on"), self.call("harness_info"), self.call("drift"), self.call("drift"))
        self.assertIn("test-gate is ON", on["result"]["content"][0]["text"])
        data = json.loads(info["result"]["content"][0]["text"])
        self.assertTrue(data["test_gate"])
        self.assertEqual(data["project_root"], str(Path(self.tmp.name).resolve()))
        self.assertIn("baseline", first["result"]["content"][0]["text"])
        self.assertIn("no structural change", second["result"]["content"][0]["text"])

    def test_unknown_symbol_is_a_tool_error_not_a_crash(self):
        [reply] = self.rpc(self.call("symbol", name="does_not_exist"))
        self.assertTrue(reply["result"]["isError"])


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_repo()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, CODE_GRAPH_ROOT=str(self.root), CODE_GRAPH_DATA=str(self.root / ".state"),
                        LAB_TEST_GATE="1")

    def hook(self, name, event, **env):
        res = subprocess.run([sys.executable, str(LAB / "hooks" / name)], input=json.dumps(event),
                             capture_output=True, text=True, env=dict(self.env, **env), timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout) if res.stdout.strip() else None

    def test_reindex_reports_blast_radius_of_the_edited_file(self):
        out = self.hook("reindex.py", {"tool_name": "Edit", "tool_input": {"file_path": str(self.root / "pkg" / "util.py")}})
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("tests/test_service.py", context)

    def test_reindex_flags_a_file_that_no_longer_parses(self):
        (self.root / "pkg" / "util.py").write_text("def clean(:\n")
        out = self.hook("reindex.py", {"tool_input": {"file_path": str(self.root / "pkg" / "util.py")}})
        self.assertIn("no longer parses", out["hookSpecificOutput"]["additionalContext"])

    def test_reindex_ignores_non_python_files(self):
        self.assertIsNone(self.hook("reindex.py", {"tool_input": {"file_path": str(self.root / "README.md")}}))

    def test_test_gate_blocks_on_failure_then_gives_up_at_the_cap(self):
        decisions = [self.hook("test_gate.py", {}, LAB_TEST_CMD="false", LAB_TEST_GATE_MAX="2") for _ in range(3)]
        self.assertEqual([d.get("decision") for d in decisions], ["block", "block", None])
        self.assertIn("attempt 2/2", decisions[1]["reason"])
        self.assertIn("handing back", decisions[2]["systemMessage"])

    def test_test_gate_skips_docs_only_changes(self):
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "init"],
                       cwd=self.root, check=True)
        (self.root / "README.md").write_text("docs")
        self.assertIsNone(self.hook("test_gate.py", {}, LAB_TEST_CMD="false"))

    def test_test_gate_passes_and_is_off_without_opt_in(self):
        self.assertIn("pass", self.hook("test_gate.py", {}, LAB_TEST_CMD="true")["systemMessage"])
        self.assertIsNone(self.hook("test_gate.py", {}, LAB_TEST_CMD="false", LAB_TEST_GATE=""))


class DriftTests(unittest.TestCase):
    def test_detects_new_cycles_dependencies_and_untested_code(self):
        tmp = make_repo()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        old = indexer.build_graph(root)
        (root / "pkg" / "util.py").write_text("from . import service\n\ndef clean(v):\n    return v\n")
        (root / "pkg" / "orphan.py").write_text("def lonely():\n    return 1\n")
        report = graph_drift.diff(old, indexer.build_graph(root))
        self.assertIn(["pkg.service", "pkg.util"], report["new_cycles"])
        self.assertIn("pkg.util --imports--> pkg.service", report["added_dependencies"])
        self.assertIn("pkg/orphan.py", report["newly_untested"])
        self.assertIn("pkg.orphan:lonely", report["added_symbols"])


if __name__ == "__main__":
    unittest.main()
