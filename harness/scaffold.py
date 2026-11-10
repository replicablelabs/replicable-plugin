#!/usr/bin/env python3
"""Install or upgrade the plugin's per-project files (design: docs/wiki.md, "Setup and upgrades").

    scaffold.py [--root DIR]            show what would change, with diffs (writes nothing)
    scaffold.py [--root DIR] --apply    make those changes

What it manages in the project:

- the ``wiki/`` skeleton (``overview.md``, ``log.md``, ``index.md``, the
  folders): created when missing, **never modified afterwards**
- ``wiki/.obsidian/app.json``: the link settings are set, other keys kept
- the wiki schema section of ``CLAUDE.md``, between the plugin's markers
- a block of ``.gitignore`` entries, between the plugin's markers
- ``.github/workflows/wiki.yml``: owned by the plugin, replaced on upgrade
- ``harness_version`` in ``.claude/replicable.json`` (other keys kept)
- the pre-0.6 names: ``.claude/replicable-harness.json`` and the
  ``replicable-harness`` markers are renamed to their ``replicable`` forms

Upgrades never touch wiki pages or ``raw/``. Version-specific steps go in
``MIGRATIONS`` and run in order, so a repository several versions behind
upgrades in steps. It also warns when a committed settings file holds
Anthropic credentials.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project  # noqa: E402
import wiki  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent / "templates"
PLUGIN_MANIFEST = project.PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
SECTION_VERSION = 2
# The markers also match their pre-0.6 "replicable-harness" spelling, so upgrades replace old blocks.
SECTION_RE = re.compile(r"<!-- replicable(?:-harness)?:wiki v\d+ -->\n.*?<!-- /replicable(?:-harness)?:wiki -->\n?", re.S)
GITIGNORE_START, GITIGNORE_END = f"# {project.PLUGIN} (managed)", f"# /{project.PLUGIN}"
GITIGNORE_RE = re.compile(r"# replicable(?:-harness)? \(managed\)\n.*?# /replicable(?:-harness)?\n?", re.S)
GITIGNORE_LINES = ["wiki/.obsidian/workspace*.json", "graphify-out/cache/"]
OBSIDIAN = {"useMarkdownLinks": True, "newLinkFormat": "relative", "alwaysUpdateLinks": True}
CREDENTIAL_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# version -> step run when a repository upgrades past that version. A step gets the
# project root and the planned changes so far, and returns extra changes. None yet:
# 0.3.0 is the first version with setup.
MIGRATIONS: Dict[str, Callable[[Path, List["Change"]], List["Change"]]] = {}


def plugin_manifest() -> dict:
    return json.loads(PLUGIN_MANIFEST.read_text())


def parse_version(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        raise SystemExit(f"invalid version {v!r} (expected numbers like 0.3.0)")


@dataclass
class Change:
    path: str  # relative to the project root
    old: Optional[str]  # None when the file is new
    new: Optional[str]  # None when the file is removed
    why: str

    @property
    def verb(self) -> str:
        return "create" if self.old is None else "remove" if self.new is None else "update"

    def diff(self) -> str:
        return "".join(difflib.unified_diff((self.old or "").splitlines(True), (self.new or "").splitlines(True),
                                            "/dev/null" if self.old is None else f"a/{self.path}",
                                            "/dev/null" if self.new is None else f"b/{self.path}"))


def template(name: str, **values: str) -> str:
    text = (TEMPLATES / name).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def read(root: Path, rel: str) -> Optional[str]:
    path = root / rel
    return path.read_text(encoding="utf-8") if path.exists() else None


def claude_section() -> str:
    return (f"<!-- {project.PLUGIN}:wiki v{SECTION_VERSION} -->\n" + template("claude-wiki-section.md").rstrip("\n")
            + f"\n<!-- /{project.PLUGIN}:wiki -->\n")


def with_claude_section(text: Optional[str], project_name: str) -> str:
    section = claude_section()
    if text is None:
        return f"# {project_name}\n\n{section}"
    if SECTION_RE.search(text):
        return SECTION_RE.sub(lambda _: section, text, count=1)
    return text.rstrip("\n") + "\n\n" + section


def with_gitignore_block(text: Optional[str]) -> str:
    block = "\n".join([GITIGNORE_START, *GITIGNORE_LINES, GITIGNORE_END]) + "\n"
    text = text or ""
    if GITIGNORE_RE.search(text):
        return GITIGNORE_RE.sub(lambda _: block, text, count=1)
    return (text.rstrip("\n") + "\n\n" if text.strip() else "") + block


def with_obsidian_settings(text: Optional[str]) -> str:
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {}
    data = data if isinstance(data, dict) else {}
    data.update(OBSIDIAN)
    return json.dumps(data, indent=2) + "\n"


def with_version(root: Path, text: Optional[str], version: str) -> str:
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        raise SystemExit(f"{project.config_path(root)} is not valid JSON; fix it first")
    data["harness_version"] = version
    return json.dumps(data, indent=2) + "\n"


def plan(root: Path) -> Tuple[List[Change], Optional[str], str]:
    """(changes, version the repository is on, plugin version)."""
    version = project.plugin_version()
    current = project.config(root).get("harness_version")
    if current and parse_version(current) > parse_version(version):
        raise SystemExit(f"this repository uses {project.PLUGIN} {current}, newer than the installed plugin "
                         f"({version}); update the plugin instead")
    today = dt.date.today().isoformat()
    changes: List[Change] = []

    def want(rel: str, new: str, why: str, create_only: bool = False) -> None:
        old = read(root, rel)
        if old is None or (not create_only and old != new):
            changes.append(Change(rel, old, new, why))

    overview = template("wiki-overview.md", today=today)
    new_overview = read(root, "wiki/overview.md") is None
    want("wiki/overview.md", overview, "wiki skeleton", create_only=True)
    want("wiki/log.md", template("wiki-log.md", today=today, version=version), "wiki skeleton", create_only=True)
    for folder in ("decisions", "domain", "raw"):
        if not (root / "wiki" / folder).is_dir():
            want(f"wiki/{folder}/.gitkeep", "", "wiki skeleton", create_only=True)
    pages = wiki.load(root)
    if new_overview:  # index the overview this run creates
        page = wiki.Page("overview.md", root / "wiki" / "overview.md", overview)
        page.meta, page.body_start = wiki.parse_frontmatter(overview)
        pages.pages.append(page)
    # Generated and never edited by hand, so an upgrade regenerates it (its header names the plugin).
    want("wiki/index.md", wiki.render_index(pages), "generated catalogue")
    want("wiki/.obsidian/app.json", with_obsidian_settings(read(root, "wiki/.obsidian/app.json")),
         "Obsidian creates relative Markdown links")
    want("CLAUDE.md", with_claude_section(read(root, "CLAUDE.md"), root.name), "wiki schema section")
    want(".gitignore", with_gitignore_block(read(root, ".gitignore")), "ignore Obsidian's personal files, graphify's cache")
    repository = plugin_manifest().get("repository", "https://github.com/replicablelabs/replicable-plugin")
    want(".github/workflows/wiki.yml", template("wiki-workflow.yml", repository=repository), "CI gate for the wiki")
    for step_version, step in sorted(MIGRATIONS.items(), key=lambda kv: parse_version(kv[0])):
        if current and parse_version(current) < parse_version(step_version) <= parse_version(version):
            changes += step(root, changes)
    config_rel, legacy_rel = f".claude/{project.CONFIG_NAME}", f".claude/{project.LEGACY_CONFIG_NAME}"
    legacy = read(root, legacy_rel)
    if changes or current != version or legacy is not None:
        source = read(root, config_rel)
        want(config_rel, with_version(root, source if source is not None else legacy, version),
             f"harness_version {current or '(none)'} -> {version}")
    if legacy is not None:
        changes.append(Change(legacy_rel, legacy, None, f"renamed to {config_rel}"))
    return changes, current, version


def apply(root: Path, changes: List[Change]) -> None:
    for change in changes:
        path = root / change.path
        if change.new is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(change.new, encoding="utf-8")


# -- credentials (each developer's Claude Code signs in on its own; never commit them) ---------
def _settings_env(path: Path) -> dict:
    try:
        env = json.loads(path.read_text()).get("env", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return env if isinstance(env, dict) else {}


def credential_warnings(root: Path) -> List[str]:
    lines = []
    for rel in (".claude/settings.json", ".mcp.json"):
        committed = [k for k in CREDENTIAL_KEYS if k in _settings_env(root / rel)]
        if committed:
            lines.append(f"WARNING: {rel} is committed with the project and sets {', '.join(committed)}. "
                         "Remove it: credentials never belong in the repository.")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="scaffold.py", description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, help="project root (default: detected by project.py)")
    parser.add_argument("--apply", action="store_true", help="write the changes (default: only show them)")
    args = parser.parse_args(argv)
    root = (args.root or project.root()).resolve()
    changes, current, version = plan(root)
    for line in credential_warnings(root):
        print(line)
    if not changes:
        print(f"{project.PLUGIN} {version}: everything is up to date")
        return 0
    print(f"{project.PLUGIN} {current or '(not set up)'} -> {version}: {len(changes)} change(s)")
    for change in changes:
        print(f"  {change.verb} {change.path}: {change.why}")
    if args.apply:
        apply(root, changes)
        print("applied")
    else:
        print()
        for change in changes:
            print(change.diff() or f"+++ b/{change.path} (empty file)\n")
        print("dry run: nothing written; rerun with --apply to make these changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
