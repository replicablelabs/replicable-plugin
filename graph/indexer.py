"""Build a code graph of a Python repository using only the standard library.

Nodes are modules, classes and functions. Edges are:

- ``defines``  module -> class/function, class -> method
- ``imports``  module -> module (only modules that live in the repository)
- ``calls``    function/module -> function/class (resolved best-effort)
- ``inherits`` class -> class

The graph is written as JSON so the MCP server, hooks and humans can share it.
Resolution is static and heuristic: it favours precision (an edge we emit is
very likely real) over recall (dynamic dispatch is not followed).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project  # noqa: E402

SCHEMA_VERSION = 1
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".graph", ".data", "build", "dist"}


@dataclass
class ModuleInfo:
    path: str  # repo-relative posix path
    name: str  # dotted module name (or path-derived name for loose scripts)
    tree: ast.Module
    # local alias -> fully qualified target ("pkg.mod" or "pkg.mod:Symbol")
    aliases: Dict[str, str] = field(default_factory=dict)


def python_files(root: Path) -> List[Path]:
    """Python files to index: git's view of the repo (respects .gitignore) minus configured excludes."""
    skip = project.excludes(root)
    return [f for f in _candidate_files(root) if not any(f.relative_to(root).as_posix().startswith(p) for p in skip)]


def _candidate_files(root: Path) -> List[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        files = [root / line for line in out.splitlines() if line]
        files = [f for f in files if f.exists() and not (set(f.relative_to(root).parts) & SKIP_DIRS)]
        if files:
            return sorted(files)
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        found.extend(Path(dirpath) / f for f in filenames if f.endswith(".py"))
    return sorted(found)


def module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or rel.stem


class _Collector(ast.NodeVisitor):
    """First pass: definitions and import aliases for one module."""

    def __init__(self, mod: ModuleInfo, nodes: Dict[str, dict], edges: List[Tuple[str, str, str]]):
        self.mod = mod
        self.nodes = nodes
        self.edges = edges
        self.stack: List[str] = []  # qualified names of enclosing defs

    def _parent_id(self) -> str:
        return f"{self.mod.name}:{self.stack[-1]}" if self.stack else self.mod.name

    def _add_def(self, node, kind: str) -> None:
        qual = ".".join(self.stack + [node.name])
        node_id = f"{self.mod.name}:{qual}"
        if kind == "function" and self.stack and self.nodes.get(self._parent_id(), {}).get("kind") == "class":
            kind = "method"
        signature = ""
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signature = f"({ast.unparse(node.args)})" if hasattr(ast, "unparse") else ""
        doc = ast.get_docstring(node) or ""
        self.nodes[node_id] = {
            "id": node_id, "kind": kind, "name": node.name, "qualname": qual,
            "module": self.mod.name, "file": self.mod.path,
            "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno),
            "signature": signature, "doc": doc.strip().splitlines()[0] if doc.strip() else "",
        }
        self.edges.append((self._parent_id(), node_id, "defines"))
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_def(node, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add_def(node, "function")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.mod.aliases[local] = alias.name if alias.asname else alias.name.split(".")[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:  # relative import
            pkg = self.mod.name.split(".")
            if not self.mod.path.endswith("__init__.py"):
                pkg = pkg[:-1]
            pkg = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
            base = ".".join(pkg + ([base] if base else []))
        for alias in node.names:
            if alias.name == "*":
                continue
            self.mod.aliases[alias.asname or alias.name] = f"{base}:{alias.name}"


class _CallResolver(ast.NodeVisitor):
    """Second pass: resolve call sites and base classes against known symbols."""

    def __init__(self, graph: "GraphBuilder", mod: ModuleInfo):
        self.g = graph
        self.mod = mod
        self.stack: List[str] = []
        # per-function `name -> class id` from `name = SomeClass(...)` (flow-insensitive)
        self.locals: List[Dict[str, str]] = [{}]

    def _scope_id(self) -> str:
        return f"{self.mod.name}:{'.'.join(self.stack)}" if self.stack else self.mod.name

    def _enclosing_class(self) -> Optional[str]:
        for depth in range(len(self.stack), 0, -1):
            candidate = f"{self.mod.name}:{'.'.join(self.stack[:depth])}"
            if self.g.nodes.get(candidate, {}).get("kind") == "class":
                return candidate
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        class_id = f"{self.mod.name}:{'.'.join(self.stack + [node.name])}"
        for base in node.bases:
            target = self.resolve_expr(base)
            if target and self.g.nodes.get(target, {}).get("kind") == "class":
                self.g.edges.add((class_id, target, "inherits"))
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node) -> None:
        self.stack.append(node.name)
        self.locals.append({})
        self.generic_visit(node)
        self.locals.pop()
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Call):
            cls = self.resolve_expr(node.value.func)
            if cls and self.g.nodes.get(cls, {}).get("kind") == "class":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.locals[-1][target.id] = cls
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = self.resolve_expr(node.func)
        if target and target in self.g.nodes and target != self._scope_id():
            self.g.edges.add((self._scope_id(), target, "calls"))
        self.generic_visit(node)

    def resolve_expr(self, expr: ast.expr) -> Optional[str]:
        if isinstance(expr, ast.Name):
            return self.resolve_name(expr.id)
        if isinstance(expr, ast.Attribute):
            # self.method() / cls.method() inside a class
            if isinstance(expr.value, ast.Name) and expr.value.id in {"self", "cls"}:
                owner = self._enclosing_class()
                if owner:
                    return self.g.find_member(owner, expr.attr)
                return None
            # var.method() where `var = SomeClass(...)` earlier in this function
            if isinstance(expr.value, ast.Name) and expr.value.id in self.locals[-1]:
                return self.g.find_member(self.locals[-1][expr.value.id], expr.attr)
            # SomeClass(...).method(): the call's result is an instance of the class
            if isinstance(expr.value, ast.Call):
                cls = self.resolve_expr(expr.value.func)
                if cls and self.g.nodes.get(cls, {}).get("kind") == "class":
                    return self.g.find_member(cls, expr.attr)
                return None
            # module_alias.func() or Class.method()
            owner = self.resolve_expr(expr.value) if isinstance(expr.value, (ast.Name, ast.Attribute)) else None
            if owner:
                if self.g.nodes.get(owner, {}).get("kind") == "module":
                    return self.g.lookup(owner, expr.attr)
                if self.g.nodes.get(owner, {}).get("kind") == "class":
                    return self.g.find_member(owner, expr.attr)
        return None

    def resolve_name(self, name: str) -> Optional[str]:
        # innermost enclosing scope outwards, then module level, then imports
        for depth in range(len(self.stack), -1, -1):
            qual = ".".join(self.stack[:depth] + [name])
            candidate = f"{self.mod.name}:{qual}"
            if candidate in self.g.nodes:
                return candidate
        target = self.mod.aliases.get(name)
        if target:
            return self.g.resolve_qualified(target, self.mod)
        return None


class GraphBuilder:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.modules: Dict[str, ModuleInfo] = {}
        self.nodes: Dict[str, dict] = {}
        self.edges: Set[Tuple[str, str, str]] = set()
        self.errors: List[dict] = []
        self.hashes: Dict[str, str] = {}

    # -- symbol lookup helpers -------------------------------------------------
    def lookup(self, module: str, attr: str) -> Optional[str]:
        if f"{module}:{attr}" in self.nodes:
            return f"{module}:{attr}"
        if f"{module}.{attr}" in self.nodes:  # submodule
            return f"{module}.{attr}"
        mod = self.modules.get(module)
        if mod and attr in mod.aliases:  # re-export
            return self.resolve_qualified(mod.aliases[attr], mod)
        return None

    def find_member(self, class_id: str, attr: str, _seen: Optional[Set[str]] = None) -> Optional[str]:
        seen = _seen or set()
        if class_id in seen:
            return None
        seen.add(class_id)
        candidate = f"{class_id}.{attr}"
        if candidate in self.nodes:
            return candidate
        for src, dst, kind in self.edges:
            if src == class_id and kind == "inherits":
                found = self.find_member(dst, attr, seen)
                if found:
                    return found
        return None

    def resolve_module(self, dotted: str, importer: Optional[ModuleInfo]) -> Optional[str]:
        if dotted in self.modules:
            return dotted
        # loose scripts (e.g. lab apps) import siblings by bare name
        if importer is not None:
            sibling_dir = str(Path(importer.path).parent)
            for mod in self.modules.values():
                if str(Path(mod.path).parent) == sibling_dir and Path(mod.path).stem == dotted:
                    return mod.name
        # `sys.path.insert(...)` + `import name`: accept a bare name only when it is unambiguous
        if "." not in dotted:
            matches = [m.name for m in self.modules.values() if Path(m.path).stem == dotted]
            if len(matches) == 1:
                return matches[0]
        return None

    def resolve_qualified(self, target: str, importer: Optional[ModuleInfo]) -> Optional[str]:
        if ":" in target:
            module, symbol = target.split(":", 1)
            resolved = self.resolve_module(module, importer)
            if resolved:
                return self.lookup(resolved, symbol) or (
                    self.resolve_module(f"{module}.{symbol}", importer))
            return None
        return self.resolve_module(target, importer)

    # -- build -----------------------------------------------------------------
    def build(self, files: Iterable[Path]) -> dict:
        started = time.perf_counter()
        raw_edges: List[Tuple[str, str, str]] = []
        for path in files:
            rel = path.resolve().relative_to(self.root).as_posix()
            source = path.read_bytes()
            self.hashes[rel] = hashlib.sha1(source).hexdigest()[:12]
            try:
                tree = ast.parse(source, filename=rel)
            except SyntaxError as exc:
                self.errors.append({"file": rel, "line": exc.lineno, "error": exc.msg})
                continue
            name = module_name(Path(rel))
            if name in self.modules:  # two loose scripts with the same dotted name
                name = rel[:-3].replace("/", ".")
            mod = ModuleInfo(path=rel, name=name, tree=tree)
            self.modules[name] = mod
            doc = ast.get_docstring(tree) or ""
            self.nodes[name] = {
                "id": name, "kind": "module", "name": name.rsplit(".", 1)[-1], "qualname": name,
                "module": name, "file": rel, "line": 1, "end_line": len(source.splitlines()),
                "signature": "", "doc": doc.strip().splitlines()[0] if doc.strip() else "",
                "is_test": Path(rel).name.startswith("test_") or "tests" in Path(rel).parts,
            }
            _Collector(mod, self.nodes, raw_edges).visit(tree)
        self.edges.update(raw_edges)

        for mod in self.modules.values():
            for target in set(mod.aliases.values()):
                resolved = self.resolve_qualified(target, mod)
                if resolved:
                    module_id = self.nodes[resolved]["module"]
                    if module_id != mod.name:
                        self.edges.add((mod.name, module_id, "imports"))
        for mod in self.modules.values():
            _CallResolver(self, mod).visit(mod.tree)

        return {
            "schema": SCHEMA_VERSION,
            "root": str(self.root),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "build_ms": round((time.perf_counter() - started) * 1000, 1),
            "files": self.hashes,
            "errors": self.errors,
            "nodes": sorted(self.nodes.values(), key=lambda n: n["id"]),
            "edges": [{"source": s, "target": t, "kind": k} for s, t, k in sorted(self.edges)],
        }


def build_graph(root: Path) -> dict:
    return GraphBuilder(root).build(python_files(root.resolve()))


def write_graph(graph: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(graph, indent=1))
    os.replace(tmp, out)  # atomic so the MCP server never reads a half-written file


def default_root() -> Path:
    return project.root()


def default_out(root: Path) -> Path:
    return Path(os.environ["CODE_GRAPH_PATH"]) if os.environ.get("CODE_GRAPH_PATH") else project.graph_path(root)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=None, help="repository to index (default: this repo)")
    parser.add_argument("--out", type=Path, default=None, help="output JSON (default: <root>/.graph/graph.json)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    root = (args.root or default_root()).resolve()
    out = args.out or default_out(root)
    graph = build_graph(root)
    write_graph(graph, out)
    if not args.quiet:
        kinds: Dict[str, int] = {}
        for edge in graph["edges"]:
            kinds[edge["kind"]] = kinds.get(edge["kind"], 0) + 1
        print(f"Indexed {len(graph['files'])} files -> {len(graph['nodes'])} nodes, "
              f"{len(graph['edges'])} edges {kinds} in {graph['build_ms']} ms -> {out}")
        for err in graph["errors"]:
            print(f"  syntax error {err['file']}:{err['line']}: {err['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
