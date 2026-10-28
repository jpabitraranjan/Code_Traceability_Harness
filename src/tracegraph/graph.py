"""Graph construction: index files (incrementally), resolve names, persist to SQLite, load into NetworkX."""
from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

import networkx as nx

from .parser import Symbol, parse

PARSER_VERSION = "3"
HASH_PREFIX = f"v{PARSER_VERSION}:"  # cached parses from an older tracegraph never match, so they are re-parsed
SKIP_DIRS = {".git", ".hg", ".venv", "venv", "env", "node_modules", "__pycache__", "build", "dist",
             "site-packages", ".tox", ".mypy_cache", ".ruff_cache", ".tracegraph", ".pytest_cache"}
MAX_BYTES = 1_000_000
SKIP_PATH_PARTS = {"migrations"}
CALL_KINDS = ("calls", "inherits")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, hash TEXT, parsed TEXT);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT,
    start_line INT, end_line INT, is_test INT DEFAULT 0, attrs TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS edges(src TEXT, dst TEXT, kind TEXT, provenance TEXT, confidence REAL, line INT);
CREATE INDEX IF NOT EXISTS e_src ON edges(src);
CREATE INDEX IF NOT EXISTS e_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS n_path ON nodes(path);
CREATE TABLE IF NOT EXISTS requirements(id TEXT PRIMARY KEY, title TEXT, status TEXT, description TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS diagnostics(node_id TEXT, tool TEXT, code TEXT, severity TEXT, message TEXT, line INT);
CREATE TABLE IF NOT EXISTS semantics(node_id TEXT PRIMARY KEY, summary TEXT, intent TEXT, effects TEXT,
    source_hash TEXT, model TEXT, created REAL);
CREATE TABLE IF NOT EXISTS inferences(src TEXT, dst TEXT, kind TEXT, confidence REAL, rationale TEXT, model TEXT,
    PRIMARY KEY(src, dst, kind));
CREATE TABLE IF NOT EXISTS callsites(path TEXT, nline INT, col INT, line INT, caller TEXT, callee TEXT,
    status TEXT, targets TEXT);
CREATE INDEX IF NOT EXISTS cs_path ON callsites(path);
CREATE TABLE IF NOT EXISTS resolutions(path TEXT, nline INT, col INT, line INT, callee TEXT, dst TEXT,
    confidence REAL, file_hash TEXT, source TEXT, PRIMARY KEY(path, nline, col, dst));
CREATE TABLE IF NOT EXISTS covlines(test TEXT, path TEXT, lines TEXT, file_hash TEXT, PRIMARY KEY(test, path));
CREATE TABLE IF NOT EXISTS covlinks(test TEXT, symbol TEXT, hits INT, file_hash TEXT, PRIMARY KEY(test, symbol));
CREATE TABLE IF NOT EXISTS covrun(ran_at REAL, command TEXT, tests INT, files INT);
CREATE TABLE IF NOT EXISTS llm_usage(task TEXT, model TEXT, prompt_tokens INT, completion_tokens INT,
    seconds REAL, ok INT, created REAL);
"""


def index_is_current(conn) -> bool:
    """False when the index is empty or was built by an older tracegraph (call columns etc. would be missing)."""
    rows = [r["hash"] for r in conn.execute("SELECT hash FROM files")]
    return bool(rows) and all(h.startswith(HASH_PREFIX) for h in rows)


def connect(db) -> sqlite3.Connection:
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")  # the UI reads while an LLM job writes
    conn.executescript(SCHEMA)
    return conn


def nid(path: str, qual: str) -> str:
    return f"{path}::{qual}"


def load_ignores(repo: Path) -> list[str]:
    """<repo>/.tracegraphignore: one glob per line, '#' comments; 'dir/' matches directories only; a pattern with
    a slash is matched against the repo-relative path, otherwise against the file or directory name."""
    try:
        lines = (Path(repo) / ".tracegraphignore").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [x.strip() for x in lines if x.strip() and not x.strip().startswith("#")]


def _ignored(rel: str, name: str, is_dir: bool, pats: list[str]) -> bool:
    for pat in pats:
        only_dir, pat = pat.endswith("/"), pat.rstrip("/").lstrip("/")
        if only_dir and not is_dir:
            continue
        if fnmatch.fnmatch(rel if "/" in pat else name, pat):
            return True
    return False


def scan_files(repo: Path):
    pats = load_ignores(repo)
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info") and not _ignored(
            (Path(root) / d).relative_to(repo).as_posix(), d, True, pats))
        for f in sorted(files):
            if f.endswith(".py"):
                p = Path(root) / f
                rel = p.relative_to(repo).as_posix()
                if SKIP_PATH_PARTS & set(rel.split("/")[:-1]) or _ignored(rel, f, False, pats):
                    continue
                if p.stat().st_size <= MAX_BYTES:
                    yield rel, p


def is_test_file(path: str) -> bool:
    parts = path.split("/")
    b = parts[-1]
    if b in ("__init__.py", "conftest.py"):
        return False
    return b.startswith("test") or b.endswith("_test.py") or "tests" in parts[:-1] or "test" in parts[:-1]


def is_test_class(s) -> bool:
    return s.kind == "class" and (s.name.startswith("Test") or any(
        b.rsplit(".", 1)[-1].endswith(("TestCase", "Test")) for b in s.bases))


def module_dotted(path: str) -> str:
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# --------------------------------------------------------------------------- resolution
class Resolver:
    def __init__(self, files: dict[str, dict]):
        self.files = files
        self.syms = {p: {s.qualname: s for s in f["symbols"]} for p, f in files.items()}
        self.top = {p: {s.name: s.qualname for s in f["symbols"] if s.parent == ""} for p, f in files.items()}
        self.modmap: dict[str, str] = {}
        for p in files:
            d = module_dotted(p)
            self.modmap[d] = p
            if d.split(".")[0] in ("src", "lib") and "." in d:
                self.modmap.setdefault(d.split(".", 1)[1], p)
        self._suffix: dict[str, str | None] = {}
        self.methods_by_name: dict[str, list[str]] = {}
        for p, f in files.items():
            for s in f["symbols"]:
                if s.kind == "method":
                    self.methods_by_name.setdefault(s.name, []).append(nid(p, s.qualname))
        self.amap = {p: self._build_amap(p, f["imports"]) for p, f in files.items()}
        self.externals: set[str] = set()

    # -- imports
    def find_module(self, mod: str):
        if not mod:
            return None
        if mod in self.modmap:
            return self.modmap[mod]
        if mod not in self._suffix:
            c = {p for d, p in self.modmap.items() if d.endswith("." + mod)}
            self._suffix[mod] = next(iter(c)) if len(c) == 1 else None
        return self._suffix[mod]

    @staticmethod
    def abs_module(path: str, imp) -> str:
        if imp.level == 0:
            return imp.module
        parts = [x for x in os.path.dirname(path).split("/") if x]
        base = parts[:max(0, len(parts) - (imp.level - 1))]
        return ".".join(base + ([imp.module] if imp.module else []))

    def _build_amap(self, path, imports):
        m = {}
        for imp in imports:
            absmod = self.abs_module(path, imp)
            if imp.name is None:
                f = self.find_module(absmod)
                m[imp.alias] = ("module", f) if f else ("ext", absmod)
            else:
                m[imp.alias] = ("from", self.find_module(absmod), absmod, imp.name)
        return m

    def resolve_from(self, tfile, absmod, name, depth=0):
        if tfile:
            if name in self.top[tfile]:
                return ("symbol", tfile, self.top[tfile][name])
            e = self.amap[tfile].get(name)
            if e and depth < 5:
                return e if e[0] != "from" else self.resolve_from(e[1], e[2], e[3], depth + 1)
        sub = self.find_module(f"{absmod}.{name}" if absmod else name)
        if sub:
            return ("module", sub)
        if tfile:
            return None  # module exists in repo but name not statically visible (star import, dynamic)
        return ("ext", f"{absmod}.{name}" if absmod else name)

    def follow(self, entry, rest, depth=0):
        if entry[0] == "from":
            entry = self.resolve_from(entry[1], entry[2], entry[3])
            if entry is None:
                return []
        kind = entry[0]
        if kind == "ext":
            full = ".".join([entry[1]] + rest)
            self.externals.add(full)
            return [(f"ext::{full}", "static", 1.0)]
        if kind == "symbol":
            return self.descend(entry[1], entry[2], rest)
        if not rest or depth > 5:  # module
            return []
        p = entry[1]
        if rest[0] in self.top[p]:
            return self.descend(p, self.top[p][rest[0]], rest[1:])
        sub = self.find_module(f"{module_dotted(p)}.{rest[0]}")
        if sub:
            return self.follow(("module", sub), rest[1:], depth + 1)
        e = self.amap[p].get(rest[0])
        if e:
            return self.follow(e, rest[1:], depth + 1)
        return []

    # -- symbols
    def descend(self, path, qual, rest):
        sym = self.syms[path][qual]
        me = nid(path, qual)
        if not rest:
            res = [(me, "static", 1.0)]
            if sym.kind == "class":
                init = self.class_member(path, qual, "__init__")
                if init:
                    res.append((init, "static", 1.0))
            return res
        if sym.kind == "class":
            m = self.class_member(path, qual, rest[0])
            return [(m, "static", 0.9)] if m else []
        return [(me, "heuristic", 0.5)]

    def class_member(self, path, cls, name, depth=0):
        q = f"{cls}.{name}"
        if q in self.syms[path]:
            return nid(path, q)
        if depth > 5:
            return None
        parent = self.syms[path][cls].parent
        for base in self.syms[path][cls].bases:
            for tid, _, _ in self.resolve(path, parent, base):
                if tid.startswith("ext::"):
                    continue
                p, qq = tid.split("::", 1)
                if self.syms[p][qq].kind == "class":
                    r = self.class_member(p, qq, name, depth + 1)
                    if r:
                        return r
        return None

    def enclosing_class(self, path, scope):
        sc = scope.split(".") if scope and scope != "<module>" else []
        for i in range(len(sc), 0, -1):
            q = ".".join(sc[:i])
            if q in self.syms[path] and self.syms[path][q].kind == "class":
                return q
        return None

    def fallback(self, name):
        c = self.methods_by_name.get(name, [])
        if len(c) == 1:
            return [(c[0], "heuristic", 0.5)]
        if 2 <= len(c) <= 4:
            return [(x, "heuristic", 0.25) for x in c]
        return []

    def member_or_ext(self, path, cls, name, depth=0, skip_self=False):
        """Find `name` on cls or its bases; (id, is_external) or None. External bases yield ext::<base>.<name>."""
        if not skip_self and f"{cls}.{name}" in self.syms[path]:
            return nid(path, f"{cls}.{name}"), False
        if depth > 5:
            return None
        sym = self.syms[path][cls]
        for base in sym.bases:
            for tid, _, _ in self.resolve(path, sym.parent, base, fallback=False):
                if tid.startswith("ext::"):
                    return f"{tid}.{name}", True
                p, qq = tid.split("::", 1)
                if self.syms[p][qq].kind == "class":
                    r = self.member_or_ext(p, qq, name, depth + 1)
                    if r:
                        return r
                    break
        return None

    def resolve(self, path, scope, dotted, fallback=True):
        parts = dotted.split(".")
        head = parts[0]
        if head in ("self", "cls", "super") and len(parts) >= 2:
            cls = self.enclosing_class(path, scope)
            if cls and (len(parts) == 2):
                r = self.member_or_ext(path, cls, parts[1], skip_self=(head == "super"))
                if r:
                    mid, is_ext = r
                    if not is_ext:
                        return [(mid, "static", 0.9)]
                    self.externals.add(mid[5:])
                    return [(mid, "static", 0.9)] if head == "super" else [(mid, "heuristic", 0.6)]
            if head == "super" or not fallback:
                return []
            return self.fallback(parts[-1])
        sc = scope.split(".") if scope and scope != "<module>" else []
        for i in range(len(sc), -1, -1):
            prefix = ".".join(sc[:i])
            if i and self.syms[path][prefix].kind == "class" and prefix != scope:
                continue  # class scope is not visible from nested functions
            q = ".".join(sc[:i] + [head])
            if q in self.syms[path]:
                return self.descend(path, q, parts[1:])
        amap = self.amap[path]
        for k in range(len(parts), 0, -1):
            a = ".".join(parts[:k])
            if a in amap:
                return self.follow(amap[a], parts[k:])
        if len(parts) >= 2 and fallback:
            return self.fallback(parts[-1])
        return []


# --------------------------------------------------------------------------- build
def _load_files(conn) -> dict[str, dict]:
    files = {}
    for r in conn.execute("SELECT path, parsed FROM files ORDER BY path"):
        d = json.loads(r["parsed"])
        d["symbols"] = [Symbol(**s) for s in d["symbols"]]
        from .parser import Call, Import, Route
        d["routes"] = [Route(**r) for r in d.get("routes", [])]
        d["calls"] = [Call(**c) for c in d["calls"]]
        d["imports"] = [Import(**i) for i in d["imports"]]
        files[r["path"]] = d
    return files


def build_graph(conn) -> dict:
    files = _load_files(conn)
    R = Resolver(files)
    nodes: dict[str, tuple] = {}
    edges: set[tuple] = set()
    callsites: list[tuple] = []
    ids = {nid(p, s.qualname) for p, f in files.items() for s in f["symbols"]}
    hashes = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM files")}
    typed = defaultdict(list)  # type-checker answers, valid only while the file is unchanged
    for r in conn.execute("SELECT * FROM resolutions"):
        if r["dst"] and hashes.get(r["path"]) == r["file_hash"] and (r["dst"] in ("-",) or r["dst"].startswith("ext::") or r["dst"] in ids):
            typed[(r["path"], r["nline"], r["col"])].append((r["dst"], r["confidence"]))

    for path, f in files.items():
        test_file = is_test_file(path)
        nodes[nid(path, "<module>")] = (nid(path, "<module>"), "module", path.rsplit("/", 1)[-1], path,
                                        1, f["lines"], int(test_file), "{}")
        for s in f["symbols"]:
            par = R.syms[path].get(s.parent) if s.parent else None
            is_test = int(test_file and (
                (s.kind == "function" and not s.parent and s.name.startswith("test")) or
                (s.kind == "method" and par is not None and not par.parent and is_test_class(par)
                 and s.name.startswith("test")) or
                (s.kind == "class" and not s.parent and is_test_class(s))))
            attrs = json.dumps({"decorators": s.decorators} if s.decorators else {})
            nodes[nid(path, s.qualname)] = (nid(path, s.qualname), s.kind, s.name, path, s.start, s.end, is_test, attrs)
            edges.add((nid(path, s.parent or "<module>"), nid(path, s.qualname), "contains", "static", 1.0, s.start))
            for base in s.bases:
                for tid, prov, conf in R.resolve(path, s.parent, base):
                    if not tid.startswith("ext::") and R.syms[tid.split("::", 1)[0]][tid.split("::", 1)[1]].kind == "class":
                        edges.add((nid(path, s.qualname), tid, "inherits", prov, conf, s.start))
                    elif tid.startswith("ext::"):
                        edges.add((nid(path, s.qualname), tid, "inherits", prov, conf, s.start))
        for imp in f["imports"]:
            absmod = R.abs_module(path, imp)
            tf = R.find_module(absmod)
            if imp.name is not None:
                sub = R.find_module(f"{absmod}.{imp.name}" if absmod else imp.name)
                tf = tf or sub
            if tf and tf != path:
                edges.add((nid(path, "<module>"), nid(tf, "<module>"), "imports", "static", 1.0, imp.line))
        for r in f["routes"]:  # Django-style url routes: route node --dispatches to--> view
            rid = nid(path, f"route:{r.route or '/'}@{r.line}")
            nodes[rid] = (rid, "route", "/" + r.route.lstrip("/"), path, r.line, r.line, 0, "{}")
            edges.add((nid(path, "<module>"), rid, "contains", "static", 1.0, r.line))
            for tid, prov, conf in R.resolve(path, r.caller, r.view, fallback=False):
                if not tid.startswith("ext::") and R.syms[tid.split("::", 1)[0]][tid.split("::", 1)[1]].kind in (
                        "function", "class", "method"):
                    edges.add((rid, tid, "calls", prov, conf, r.line))
                    break
        for c in f["calls"]:
            targets = R.resolve(path, c.caller, c.callee)
            status = "static" if any(p == "static" for _, p, _ in targets) else ("heuristic" if targets else "unresolved")
            tk = typed.get((path, c.nline, c.col))
            if tk and status != "static":  # a type-aware answer replaces name-matching guesses, never static facts
                targets, status = [(d, "typed", cf) for d, cf in tk if d != "-"], "typed"  # "-": builtin, no edge
            callsites.append((path, c.nline, c.col, c.line, c.caller, c.callee, status, json.dumps([t[0] for t in targets][:6])))
            for tid, prov, conf in targets:
                src = nid(path, c.caller)
                if tid != src or prov in ("static", "typed"):  # keep real recursion, drop heuristic self-loops
                    edges.add((src, tid, "calls", prov, conf, c.line))

    for e in R.externals | {t for (_, t, *_r) in edges if t.startswith("ext::")}:
        e = e.removeprefix("ext::")
        nodes[f"ext::{e}"] = (f"ext::{e}", "external", e.rsplit(".", 1)[-1], "", 0, 0, 0, "{}")
    for (s, t, *_r) in list(edges):
        if t.startswith("ext::") and t not in nodes:
            nodes[t] = (t, "external", t[5:].rsplit(".", 1)[-1], "", 0, 0, 0, "{}")

    conn.execute("DELETE FROM nodes")
    conn.execute("DELETE FROM callsites")
    conn.executemany("INSERT INTO callsites VALUES(?,?,?,?,?,?,?,?)", callsites)
    conn.execute("DELETE FROM edges WHERE provenance != 'declared'")
    conn.executemany("INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)", nodes.values())
    conn.executemany("INSERT INTO edges VALUES(?,?,?,?,?,?)", sorted(edges, key=lambda e: tuple(map(str, e))))
    conn.commit()
    ncalls = sum(1 for e in edges if e[2] == "calls")
    return {"files": len(files), "nodes": len(nodes), "edges": len(edges), "calls": ncalls,
            "routes": sum(1 for n in nodes.values() if n[1] == "route"),
            "typed_calls": sum(1 for e in edges if e[2] == "calls" and e[3] == "typed"),
            "heuristic_calls": sum(1 for e in edges if e[2] == "calls" and e[3] == "heuristic")}


def index_repo(repo, db, requirements=None, quality=None) -> dict:
    repo = Path(repo).resolve()
    conn = connect(db)
    old = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM files")}
    seen, reparsed = set(), 0
    for rel, p in scan_files(repo):
        data = p.read_bytes()
        h = HASH_PREFIX + hashlib.sha1(data).hexdigest()
        seen.add(rel)
        if old.get(rel) == h:
            continue
        pf = parse(data)
        conn.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)",
                     (rel, h, json.dumps({"lines": data.count(b"\n") + 1, **pf.to_dict()})))
        reparsed += 1
    for rel in set(old) - seen:
        conn.execute("DELETE FROM files WHERE path=?", (rel,))
    conn.commit()
    stats = build_graph(conn)
    stats["reparsed"] = reparsed
    from . import requirements as rq
    if requirements:
        stats["requirements"] = rq.load(conn, requirements)
    rq.link(conn)
    if quality:
        from . import quality as q
        stats["diagnostics"] = q.run(conn, repo, tools=quality)
    return stats


def to_networkx(conn, kinds=None) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for r in conn.execute("SELECT * FROM nodes"):
        G.add_node(r["id"], **{k: r[k] for k in r.keys() if k != "id"})
    q = "SELECT * FROM edges"
    for r in conn.execute(q):
        if kinds is None or r["kind"] in kinds:
            G.add_edge(r["src"], r["dst"], kind=r["kind"], provenance=r["provenance"],
                       confidence=r["confidence"], line=r["line"])
    return G
