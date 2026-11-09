#!/usr/bin/env python3
"""Deterministic tooling for a project's wiki (design: docs/wiki.md).

    wiki.py [--root DIR] check [--strict] [--base REF]
    wiki.py [--root DIR] index
    wiki.py [--root DIR] stale
    wiki.py [--root DIR] new TYPE TITLE [--summary TEXT] [--code PATH ...]

The wiki lives in ``<root>/wiki``; the root defaults to the project root from
``project.py``. Standard library only, Python 3.9+.

History checks (staleness, raw immutability, ``--base``) need git history: in
CI, check out with ``fetch-depth: 0``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project  # noqa: E402

WIKI_DIR = "wiki"
# type -> (index heading, folder relative to wiki/ or None for the top level)
TYPES = {
    "overview": ("Overview", None),
    "decision": ("Decisions", "decisions"),
    "domain": ("Domain", "domain"),
    "trap": ("Traps", None),
}
FIXED_FILES = {"overview": "overview.md", "trap": "traps.md"}
STATUSES = ("proposed", "accepted", "superseded", "deprecated")
GENERATED = {"index.md", "log.md"}
MAX_SUMMARY = 120
MAX_LINES = 150

KEBAB = r"[a-z0-9]+(?:-[a-z0-9]+)*"
KEBAB_RE = re.compile(rf"^{KEBAB}$")
DECISION_RE = re.compile(rf"^\d{{4}}-{KEBAB}\.md$")
RAW_RE = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-{KEBAB}\.md$")

LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"']*[\"'])?\s*\)")
REF_LINK_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")
INLINE_CODE_RE = re.compile(r"(`+).+?\1")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("credential assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*[\"']?[^\s\"']{8,}")),
]


# -- frontmatter (a small YAML subset: scalars, quoted strings, flow and block lists) --
class FrontmatterError(ValueError):
    pass


def _scalar(raw: str, lineno: int) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except ValueError:
            raise FrontmatterError(f"line {lineno}: invalid double-quoted string")
        if not isinstance(value, str):
            raise FrontmatterError(f"line {lineno}: invalid double-quoted string")
        return value
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            raise FrontmatterError(f"line {lineno}: unterminated single-quoted string")
        return raw[1:-1].replace("''", "'")
    raw = re.sub(r"\s+#.*$", "", raw)
    if raw[:1] in set("{|>&*!%@`"):
        raise FrontmatterError(f"line {lineno}: unsupported YAML; wiki frontmatter uses plain values and lists")
    if ": " in raw:
        raise FrontmatterError(f"line {lineno}: a value containing ': ' must be quoted")
    return raw


def _flow_list(raw: str, lineno: int) -> List[str]:
    inner = raw.strip()[1:-1]
    items, buf, quote = [], "", None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            buf += ch
            quote = ch
        elif ch == ",":
            items.append(buf)
            buf = ""
        else:
            buf += ch
    if quote:
        raise FrontmatterError(f"line {lineno}: unterminated string in list")
    items.append(buf)
    return [_scalar(i, lineno) for i in items if i.strip()]


def parse_frontmatter(text: str) -> Tuple[dict, int]:
    """Return (fields, index of the first body line)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise FrontmatterError("missing frontmatter (the page must start with a '---' line)")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise FrontmatterError("frontmatter is not closed with a '---' line")
    data: Dict[str, object] = {}
    pending: Optional[str] = None  # key whose value is an empty line, so a block list may follow
    for i in range(1, end):
        line, lineno = lines[i], i + 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-(?:\s+(.*))?$", line)
        if item:
            if pending is None:
                raise FrontmatterError(f"line {lineno}: list item without a key")
            data[pending] = (data[pending] or []) + [_scalar(item.group(1) or "", lineno)]
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:(?:\s+(.*))?$", line)
        if not m:
            raise FrontmatterError(f"line {lineno}: expected 'key: value'")
        key, raw = m.group(1), (m.group(2) or "").strip()
        if key in data:
            raise FrontmatterError(f"line {lineno}: duplicate key '{key}'")
        if not raw or raw.startswith("#"):
            data[key], pending = None, key
        elif raw.startswith("[") and raw.endswith("]"):
            data[key], pending = _flow_list(raw, lineno), None
        else:
            data[key], pending = _scalar(raw, lineno), None
    return data, end + 1


def _dump_scalar(value: object) -> str:
    s = str(value)
    if (not s or s != s.strip() or re.search(r"[:#\[\]{},&*!|>'\"%@`]", s)
            or s.lower() in {"true", "false", "null", "yes", "no", "~"}):
        return json.dumps(s, ensure_ascii=False)
    return s


def dump_frontmatter(data: dict) -> str:
    out = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            out.append(f"{key}: [{', '.join(_dump_scalar(v) for v in value)}]")
        elif value is None:
            out.append(f"{key}:")
        else:
            out.append(f"{key}: {_dump_scalar(value)}")
    return "\n".join(out + ["---"]) + "\n"


def as_list(value: object) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# -- the wiki on disk ---------------------------------------------------------------
@dataclass
class Page:
    rel: str  # path relative to wiki/, POSIX
    path: Path
    text: str
    meta: Optional[dict] = None
    error: Optional[str] = None
    body_start: int = 0

    @property
    def title(self) -> str:
        for line in self.text.split("\n")[self.body_start:]:
            m = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
            if m:
                return m.group(1)
        return Path(self.rel).stem.replace("-", " ").capitalize()

    @property
    def valid_for_index(self) -> bool:
        return bool(self.meta) and self.meta.get("type") in TYPES and isinstance(self.meta.get("summary"), str)


@dataclass
class Wiki:
    root: Path
    dir: Path
    pages: List[Page] = field(default_factory=list)   # typed pages
    special: List[Page] = field(default_factory=list)  # index.md, log.md
    raw: List[Path] = field(default_factory=list)
    other: List[Path] = field(default_factory=list)    # attachments and the like

    def rel(self, path: Path) -> str:
        return path.relative_to(self.dir).as_posix()


def load(root: Path) -> Wiki:
    wiki = Wiki(root=root, dir=root / WIKI_DIR)
    if not wiki.dir.is_dir():
        return wiki
    for path in sorted(wiki.dir.rglob("*")):
        rel = path.relative_to(wiki.dir)
        if not path.is_file() or any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts[0] == "raw":
            wiki.raw.append(path)
        elif path.suffix != ".md":
            wiki.other.append(path)
        else:
            page = Page(rel=rel.as_posix(), path=path, text=path.read_text(encoding="utf-8", errors="replace"))
            if page.rel in GENERATED:
                wiki.special.append(page)
                continue
            try:
                page.meta, page.body_start = parse_frontmatter(page.text)
            except FrontmatterError as exc:
                page.error = str(exc)
            wiki.pages.append(page)
    return wiki


def render_index(wiki: Wiki) -> str:
    out = ["# Index", "",
           "<!-- Generated from page frontmatter by the replicable-harness wiki CLI (`wiki.py index`). "
           "Do not edit by hand. -->", ""]
    for type_, (heading, _) in TYPES.items():
        group = sorted((p for p in wiki.pages if p.valid_for_index and p.meta["type"] == type_), key=lambda p: p.rel)
        if group:
            out += [f"## {heading}", ""]
            out += [f"- [{p.title.replace(']', chr(92) + ']')}]({p.rel}): {p.meta['summary']}" for p in group]
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def write_index(wiki: Wiki) -> Path:
    path = wiki.dir / "index.md"
    path.write_text(render_index(wiki), encoding="utf-8")
    return path


# -- git ----------------------------------------------------------------------------------
def git(root: Path, *args: str) -> Optional[str]:
    try:
        res = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def git_lines(root: Path, *args: str) -> Optional[List[str]]:
    out = git(root, *args)
    return None if out is None else [line for line in out.splitlines() if line.strip()]


def is_repo(root: Path) -> bool:
    return (git(root, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"


def is_shallow(root: Path) -> bool:
    return (git(root, "rev-parse", "--is-shallow-repository") or "").strip() == "true"


def last_change(root: Path, path: str, cache: Dict[str, Optional[str]]) -> Optional[str]:
    """Date (YYYY-MM-DD) of the last commit touching ``path``."""
    if path not in cache:
        out = git(root, "log", "-1", "--format=%cd", "--date=short", "--", path)
        cache[path] = (out or "").strip() or None
    return cache[path]


def stale_pages(wiki: Wiki) -> List[Tuple[Page, List[Tuple[str, str]]]]:
    cache: Dict[str, Optional[str]] = {}
    result = []
    for page in wiki.pages:
        if not page.meta or not isinstance(page.meta.get("updated"), str):
            continue
        changed = []
        for code in code_paths(page):
            date = last_change(wiki.root, code, cache)
            if date and date > page.meta["updated"]:
                changed.append((code, date))
        if changed:
            result.append((page, changed))
    return result


def code_paths(page: Page) -> List[str]:
    return [c.strip().rstrip("/").removeprefix("./")
            for c in as_list((page.meta or {}).get("code")) if isinstance(c, str) and c.strip()]


# -- check ----------------------------------------------------------------------------------
@dataclass
class Finding:
    level: str  # "error" or "warning"
    where: str  # path relative to the project root
    message: str
    line: Optional[int] = None

    def __str__(self) -> str:
        loc = self.where + (f":{self.line}" if self.line else "")
        return f"{self.level}: {loc}: {self.message}"


def body_lines(page: Page) -> Iterable[Tuple[int, str]]:
    """(line number, text) of the body, with fenced code blocks and inline code removed."""
    fence = None
    for i, line in enumerate(page.text.split("\n")):
        if i < page.body_start:
            continue
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None:
            yield i + 1, INLINE_CODE_RE.sub("", line)


def link_targets(page: Page) -> Iterable[Tuple[int, str]]:
    for lineno, line in body_lines(page):
        for m in LINK_RE.finditer(line):
            yield lineno, m.group(1)
        m = REF_LINK_RE.match(line)
        if m:
            yield lineno, m.group(1)


class Checker:
    def __init__(self, wiki: Wiki):
        self.wiki = wiki
        self.findings: List[Finding] = []
        self.inbound: Dict[str, int] = {}
        self.repo = is_repo(wiki.root)
        self.shallow = self.repo and is_shallow(wiki.root)

    def add(self, level: str, rel: str, message: str, line: Optional[int] = None) -> None:
        self.findings.append(Finding(level, f"{WIKI_DIR}/{rel}", message, line))

    def run(self, base: Optional[str] = None) -> List[Finding]:
        w = self.wiki
        for page in w.pages:
            self.check_name(page.rel)
            if page.error:
                self.add("error", page.rel, page.error)
            else:
                self.check_frontmatter(page)
            if len(page.text.splitlines()) > MAX_LINES:
                self.add("warning", page.rel, f"page has {len(page.text.splitlines())} lines (keep pages to "
                                              f"{MAX_LINES}; split it by topic)")
        for page in w.pages + w.special:
            self.check_links(page)
        self.check_orphans()
        self.check_index()
        self.check_raw()
        if self.repo:
            if self.shallow:
                self.findings.append(Finding("warning", WIKI_DIR, "shallow git clone: staleness and raw-history "
                                                                  "checks are incomplete (CI: fetch-depth: 0)"))
            else:
                for page, changed in stale_pages(w):
                    files = ", ".join(f"{c} ({d})" for c, d in changed)
                    self.add("warning", page.rel, f"stale: code changed after updated {page.meta['updated']}: {files}")
        if base is not None:
            self.check_change_set(base)
        return self.findings

    def check_name(self, rel: str) -> None:
        parts = rel.split("/")
        for d in parts[:-1]:
            if not KEBAB_RE.match(d):
                self.add("error", rel, f"folder name '{d}' is not kebab-case")
        name = parts[-1]
        if parts[0] == "decisions" and len(parts) == 2:
            if not DECISION_RE.match(name):
                self.add("error", rel, "decision file names are numbered kebab-case: 0007-short-title.md")
        elif not KEBAB_RE.match(name[:-3]):
            self.add("error", rel, "file names are kebab-case, no spaces: short-title.md")

    def check_frontmatter(self, page: Page) -> None:
        meta, rel = page.meta, page.rel
        type_ = meta.get("type")
        if type_ not in TYPES:
            self.add("error", rel, f"type must be one of {', '.join(TYPES)} (got {type_!r})")
        else:
            folder = TYPES[type_][1]
            expected_dir = rel.rsplit("/", 1)[0] if "/" in rel else None
            if type_ in FIXED_FILES and rel != FIXED_FILES[type_]:
                self.add("error", rel, f"type {type_} belongs in {WIKI_DIR}/{FIXED_FILES[type_]}")
            elif type_ not in FIXED_FILES and expected_dir != folder:
                self.add("error", rel, f"type {type_} belongs in {WIKI_DIR}/{folder}/")
        summary = meta.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            self.add("error", rel, "summary is required: one line saying what the page holds")
        elif len(summary) > MAX_SUMMARY:
            self.add("error", rel, f"summary is {len(summary)} characters (at most {MAX_SUMMARY})")
        updated = meta.get("updated")
        try:
            dt.date.fromisoformat(updated if isinstance(updated, str) else "")
        except ValueError:
            self.add("error", rel, f"updated must be a date, YYYY-MM-DD (got {updated!r})")
        for key in ("code", "sources", "tags"):
            if any(not isinstance(v, str) for v in as_list(meta.get(key))):
                self.add("error", rel, f"{key} must be a list of strings")
        for code in code_paths(page):
            p = Path(code)
            if p.is_absolute() or ".." in p.parts:
                self.add("error", rel, f"code path must be relative to the repository root: {code}")
            elif not (self.wiki.root / p).exists():
                self.add("error", rel, f"code path does not exist (moved or deleted?): {code}")
        for source in as_list(meta.get("sources")):
            if isinstance(source, str) and source.startswith("raw/") and not (self.wiki.dir / source).is_file():
                self.add("error", rel, f"source does not exist: {source}")
        if type_ == "decision":
            status = meta.get("status")
            if status not in STATUSES:
                self.add("error", rel, f"decision status must be one of {', '.join(STATUSES)} (got {status!r})")
            superseded_by = meta.get("superseded_by")
            if status == "superseded" and not superseded_by:
                self.add("warning", rel, "superseded decision without superseded_by")
            if isinstance(superseded_by, str) and superseded_by:
                if not ((page.path.parent / superseded_by).is_file() or (self.wiki.dir / superseded_by).is_file()):
                    self.add("error", rel, f"superseded_by does not exist: {superseded_by}")

    def check_links(self, page: Page) -> None:
        wiki_dir = self.wiki.dir.resolve()
        for lineno, line in body_lines(page):
            if WIKILINK_RE.search(line):
                self.add("error", page.rel, "[[wikilinks]] don't render on GitHub; use [text](relative/path.md)", lineno)
        for lineno, target in link_targets(page):
            if target.startswith("#") or SCHEME_RE.match(target):
                continue
            if target.startswith("/"):
                self.add("error", page.rel, f"link must be relative: {target}", lineno)
                continue
            path = unquote(re.split(r"[#?]", target, 1)[0])
            resolved = (page.path.parent / path).resolve()
            try:
                inside = resolved.relative_to(wiki_dir).as_posix()
            except ValueError:
                self.add("error", page.rel, f"link leaves the wiki (reference code as an inline path): {target}", lineno)
                continue
            if not resolved.exists():
                self.add("error", page.rel, f"broken link: {target}", lineno)
            elif page.rel != "index.md" and inside != page.rel:
                self.inbound[inside] = self.inbound.get(inside, 0) + 1

    def check_orphans(self) -> None:
        for page in self.wiki.pages:
            if page.meta and page.meta.get("type") != "overview" and not self.inbound.get(page.rel):
                self.add("warning", page.rel, "orphan: no page links here (only the index does)")

    def check_index(self) -> None:
        index = self.wiki.dir / "index.md"
        if not index.exists():
            self.add("error", "index.md", "missing; run `wiki.py index`")
        elif index.read_text(encoding="utf-8", errors="replace") != render_index(self.wiki):
            self.add("error", "index.md", "out of date with the pages' frontmatter; run `wiki.py index`")

    def check_raw(self) -> None:
        for path in self.wiki.raw:
            rel = self.wiki.rel(path)
            if not RAW_RE.match(path.name) or len(rel.split("/")) != 2:
                self.add("error", rel, "raw sources are text files named raw/YYYY-MM-DD-short-title.md")
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                self.add("error", rel, "raw sources must be text; keep binaries in Git LFS or external")
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for label, pattern in SECRET_PATTERNS:
                    if pattern.search(line):
                        self.add("warning", rel, f"likely secret ({label}); remove it before committing", lineno)
                        break
        if not self.repo:
            return
        raw_dir = f"{WIKI_DIR}/raw"
        modified = set(git_lines(self.wiki.root, "log", "--diff-filter=M", "--name-only", "--relative",
                                 "--format=", "--", raw_dir) or [])
        modified |= set(git_lines(self.wiki.root, "diff", "--name-only", "--relative", "--diff-filter=M",
                                  "HEAD", "--", raw_dir) or [])
        for path in sorted(modified):
            self.findings.append(Finding("error", path, "raw sources are immutable: this file was modified after "
                                                        "its first commit (put corrections in wiki pages)"))

    def check_change_set(self, base: str) -> None:
        root = self.wiki.root
        if not self.repo:
            self.findings.append(Finding("error", WIKI_DIR, "--base needs a git repository"))
            return
        merge_base = (git(root, "merge-base", base, "HEAD") or "").strip()
        if not merge_base:
            self.findings.append(Finding("error", WIKI_DIR, f"no merge base between {base} and HEAD "
                                                            "(fetch the base branch; CI: fetch-depth: 0)"))
            return
        changed = set(git_lines(root, "diff", "--name-only", "--relative", merge_base) or [])
        changed |= set(git_lines(root, "ls-files", "--others", "--exclude-standard") or [])
        for page in self.wiki.pages:
            if f"{WIKI_DIR}/{page.rel}" in changed:
                continue
            hits = sorted(f for f in changed for c in code_paths(page) if f == c or f.startswith(c + "/"))
            if hits:
                self.add("error", page.rel, f"its code changed since {base} but the page did not: {', '.join(hits)} "
                                            "(update the page, or bump updated after checking it)")


# -- new ------------------------------------------------------------------------------------
TEMPLATES = {
    "overview": "# {title}\n\n## Purpose\n\nTODO: what the system is for.\n\n## Users\n\nTODO\n\n"
                "## Main flows\n\nTODO\n\n## Where to read next\n\nTODO: links to the decisions and domain pages.\n",
    "decision": "# {title}\n\n## Context\n\nTODO: the situation and the forces at play.\n\n## Decision\n\nTODO\n\n"
                "## Consequences\n\nTODO: what follows, good and bad.\n\n## Alternatives considered\n\nTODO\n",
    "domain": "# {title}\n\n## Rules\n\nTODO: each rule the code must uphold.\n\n"
              "## Examples and edge cases\n\nTODO\n",
    "trap": "\n## {title}\n\n- **Symptom:** TODO\n- **Cause:** TODO\n- **Fix:** TODO\n- **How to avoid it:** TODO\n",
}
DEFAULT_SUMMARY = "TODO: one line, at most 120 characters"


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def new_page(wiki: Wiki, type_: str, title: str, summary: Optional[str], code: List[str]) -> Path:
    today = dt.date.today().isoformat()
    wiki.dir.mkdir(parents=True, exist_ok=True)
    if type_ == "trap" and (wiki.dir / "traps.md").exists():
        path = wiki.dir / "traps.md"
        text = path.read_text(encoding="utf-8")
        if code:
            meta, start = parse_frontmatter(text)
            meta["code"] = as_list(meta.get("code")) + [c for c in code if c not in as_list(meta.get("code"))]
            text = dump_frontmatter(meta) + "\n".join(text.split("\n")[start:])
        path.write_text(text.rstrip("\n") + "\n" + TEMPLATES["trap"].format(title=title), encoding="utf-8")
        return path
    if type_ in FIXED_FILES:
        path = wiki.dir / FIXED_FILES[type_]
    else:
        slug = slugify(title)
        if not slug:
            raise SystemExit("title needs at least one letter or digit")
        folder = wiki.dir / TYPES[type_][1]
        if type_ == "decision":
            numbers = [int(p.name[:4]) for p in folder.glob("*.md") if DECISION_RE.match(p.name)] if folder.is_dir() else []
            slug = f"{max(numbers, default=0) + 1:04d}-{slug}"
        path = folder / f"{slug}.md"
    if path.exists():
        raise SystemExit(f"{path.relative_to(wiki.root)} already exists")
    meta = {"type": type_, "summary": summary or (
        "Known pitfalls, each with the symptom and the fix" if type_ == "trap" else DEFAULT_SUMMARY)}
    if type_ == "decision":
        meta["status"] = "proposed"
    meta.update(code=code, sources=[], updated=today)
    body = "# Traps\n" + TEMPLATES["trap"].format(title=title) if type_ == "trap" else TEMPLATES[type_].format(title=title)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(meta) + "\n" + body, encoding="utf-8")
    return path


# -- CLI ----------------------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki.py", description="Check and maintain the project wiki (wiki/).")
    parser.add_argument("--root", type=Path, help="project root (default: detected by project.py)")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="run every check; exit 1 on errors")
    check.add_argument("--strict", action="store_true", help="fail on warnings too")
    check.add_argument("--base", metavar="REF", help="also fail when code changed since REF without its pages")
    sub.add_parser("index", help="regenerate wiki/index.md from frontmatter")
    sub.add_parser("stale", help="list pages whose code changed after their updated date")
    new = sub.add_parser("new", help="create a page from its template")
    new.add_argument("type", choices=list(TYPES))
    new.add_argument("title")
    new.add_argument("--summary", help=f"the index line, at most {MAX_SUMMARY} characters")
    new.add_argument("--code", nargs="+", default=[], metavar="PATH", help="repository paths the page describes")
    args = parser.parse_args(argv)

    root = (args.root or project.root()).resolve()
    wiki = load(root)
    if args.command == "new":
        path = new_page(wiki, args.type, args.title, args.summary, args.code)
        write_index(load(root))
        print(f"wrote {path.relative_to(root)} and {WIKI_DIR}/index.md")
        return 0
    if not wiki.dir.is_dir():
        print(f"no {WIKI_DIR}/ directory in {root}; nothing to do (create pages with `wiki.py new`)")
        return 0
    if args.command == "index":
        print(f"wrote {write_index(wiki).relative_to(root)}")
        return 0
    if args.command == "stale":
        if not is_repo(root):
            print("not a git repository: cannot tell which pages are stale")
            return 0
        if is_shallow(root):
            print("warning: shallow git clone; dates may be wrong", file=sys.stderr)
        stale = stale_pages(wiki)
        for page, changed in stale:
            print(f"{WIKI_DIR}/{page.rel} (updated {page.meta['updated']}): "
                  + ", ".join(f"{c} changed {d}" for c, d in changed))
        if not stale:
            print("no stale pages")
        return 0
    findings = Checker(wiki).run(base=args.base)
    findings.sort(key=lambda f: (f.level != "error", f.where, f.line or 0))
    for f in findings:
        print(f)
    errors = sum(f.level == "error" for f in findings)
    warnings = len(findings) - errors
    print(f"wiki check: {errors} error(s), {warnings} warning(s) in {len(wiki.pages)} page(s)")
    return 1 if errors or (args.strict and warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
