"""Runtime evidence: which tests actually executed which code (coverage.py dynamic contexts).

Static analysis cannot see through subprocesses, dynamic dispatch or framework glue; execution can. `run` executes
your tests under coverage with one context per test, `import_data` turns that into
  covlines   test -> executed lines per file (line-exact traceability)
  covlinks   test -> symbols it executed  (materialised as `covers` edges, provenance `runtime`)
Line numbers are only valid for the file version they were measured on, so a hash manifest is written when the
tests start and every row remembers it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from .graph import HASH_PREFIX, module_dotted, scan_files
from .llm import LLMError

BOOT = Path(__file__).parent / "boot"


class CoverageError(LLMError):
    """User-facing setup/runtime problem (a subclass of LLMError so CLI and UI report it the same way)."""


def cov_dir(repo) -> Path:
    return Path(repo) / ".tracegraph" / "cov"


def sha(path) -> str:
    return hashlib.sha1(Path(path).read_bytes()).hexdigest()


def pack(lines) -> str:
    out, lines = [], sorted(set(lines))
    i = 0
    while i < len(lines):
        j = i
        while j + 1 < len(lines) and lines[j + 1] == lines[j] + 1:
            j += 1
        out.append(str(lines[i]) if i == j else f"{lines[i]}-{lines[j]}")
        i = j + 1
    return ",".join(out)


def unpack(s: str) -> list[int]:
    out = []
    for part in filter(None, s.split(",")):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


def normalize_command(cmd) -> tuple[list[str], bool]:
    """-> (arguments for `coverage run`, is_pytest)."""
    cmd = list(cmd or ["pytest", "-q"])
    exe = Path(cmd[0]).name
    if exe in ("pytest", "py.test"):
        return ["-m", "pytest", *cmd[1:]], True
    if re.fullmatch(r"python[\d.]*", exe) or cmd[0] == sys.executable:
        rest = cmd[1:]
        return rest, rest[:2] == ["-m", "pytest"]
    raise CoverageError(f"don't know how to run '{cmd[0]}' under coverage. Use: -- pytest ...   or   -- python manage.py test")


# ------------------------------------------------------------------ running
def run(repo, conn, cmd=None, python=None) -> dict:
    repo, py = Path(repo).resolve(), python or sys.executable
    if subprocess.run([py, "-c", "import coverage"], capture_output=True).returncode:
        raise CoverageError(f"coverage.py is not installed for {py}. Install it with: {py} -m pip install coverage")
    stale = [p for p, h in ((r["path"], r["hash"]) for r in conn.execute("SELECT path, hash FROM files"))
             if not (repo / p).exists() or HASH_PREFIX + sha(repo / p) != h]
    if not conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone() or stale:
        raise CoverageError("the index is empty or out of date" + (f" (e.g. {stale[0]})" if stale else "") +
                            f". Run first: tracegraph index --repo {repo}")
    args, is_pytest = normalize_command(cmd)
    d = cov_dir(repo)
    shutil.rmtree(d, ignore_errors=True)
    (d / "boot").mkdir(parents=True)
    for f in BOOT.glob("*.py"):
        shutil.copy(f, d / "boot" / f.name)
    (d / "manifest.json").write_text(json.dumps({rel: sha(p) for rel, p in scan_files(repo)}))
    omit = "\n    ".join(["*/site-packages/*", "*/dist-packages/*", *(f"{repo}/{x}/*" for x in ("env", "venv", ".venv", ".tracegraph"))])
    (d / "rc").write_text(f"[run]\ndata_file = {d / '.coverage'}\nparallel = True\nsource = {repo}\n"
                          + ("" if is_pytest else "dynamic_context = test_function\n") + f"omit =\n    {omit}\n")
    env = dict(os.environ, COVERAGE_PROCESS_START=str(d / "rc"),
               PYTHONPATH=os.pathsep.join([str(d / "boot")] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])))
    if is_pytest:
        env["PYTEST_ADDOPTS"] = (os.environ.get("PYTEST_ADDOPTS", "") + " -p tracegraph_pytest_plugin").strip()
    t0 = time.time()
    proc = subprocess.run([py, "-m", "coverage", "run", "--rcfile", str(d / "rc"), *args], cwd=repo, env=env)
    stats = import_data(conn, repo, " ".join(cmd or ["pytest", "-q"]))
    stats.update(exit_code=proc.returncode, mode="pytest" if is_pytest else "function", seconds=round(time.time() - t0, 1))
    return stats


# ------------------------------------------------------------------ importing
def _context_resolver(conn):
    by_pytest, by_dotted = {}, {}
    for r in conn.execute("SELECT id, path FROM nodes WHERE is_test=1 AND kind IN ('function','method')"):
        qual = r["id"].split("::", 1)[1]
        by_pytest[f"{r['path']}::{qual.replace('.', '::')}"] = r["id"]
        by_dotted[f"{module_dotted(r['path'])}.{qual}"] = r["id"]
    cache = {}

    def resolve(ctx):
        if ctx not in cache:
            c = re.sub(r"\[.*\]$", "", ctx.split("|")[0])  # parametrised ids, pytest-cov phase suffix
            hit = by_pytest.get(c)
            if hit is None:
                cands = {i for full, i in by_dotted.items() if full == c or full.endswith("." + c)}
                hit = next(iter(cands)) if len(cands) == 1 else None
            cache[ctx] = hit
        return cache[ctx]
    return resolve


def import_data(conn, repo, command="") -> dict:
    from coverage import Coverage

    from . import requirements as rq

    repo = Path(repo).resolve()
    d = cov_dir(repo)
    if not list(d.glob(".coverage*")):
        raise CoverageError(f"no coverage data in {d}. Run your tests under coverage first: tracegraph coverage run --repo {repo}")
    cov = Coverage(data_file=str(d / ".coverage"), config_file=str(d / "rc") if (d / "rc").exists() else False)
    cov.combine(data_paths=[str(d)], strict=False, keep=True)
    data = cov.get_data()
    try:
        manifest = json.loads((d / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    resolve = _context_resolver(conn)
    index_hash = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM files")}
    conn.execute("DELETE FROM covlines")
    conn.execute("DELETE FROM covlinks")
    seen_tests, unmapped, files, links, stale_files = set(), Counter(), 0, 0, 0
    for fname in sorted(data.measured_files()):
        try:
            rel = Path(fname).resolve().relative_to(repo).as_posix()
        except ValueError:
            continue
        if rel not in index_hash or not (repo / rel).exists():
            continue
        per_test = defaultdict(list)
        for ln, ctxs in data.contexts_by_lineno(fname).items():
            for c in ctxs:
                if not c:
                    continue
                t = resolve(c)
                if t:
                    per_test[t].append(ln)
                else:
                    unmapped[c] += 1
        if not per_test:
            continue
        files += 1
        cur, ran_with = sha(repo / rel), manifest.get(rel)
        fhash = ran_with or cur
        for t, lines in per_test.items():
            seen_tests.add(t)
            conn.execute("INSERT OR REPLACE INTO covlines VALUES(?,?,?,?)", (t, rel, pack(lines), fhash))
        # symbol-level links need current symbol ranges == the ranges the tests ran against
        if not (fhash == cur and index_hash[rel] == HASH_PREFIX + cur):
            stale_files += 1
            continue
        owner, start = {}, {}
        syms = conn.execute("SELECT id, start_line, end_line, is_test FROM nodes WHERE path=? AND kind IN ('function','method') "
                            "ORDER BY (end_line - start_line) DESC", (rel,)).fetchall()
        for s in syms:  # widest first, so inner functions overwrite their parents
            for ln in range(s["start_line"], s["end_line"] + 1):
                owner[ln] = s
        for t, lines in per_test.items():
            hits = Counter()
            for ln in lines:
                s = owner.get(ln)
                if s and not s["is_test"] and (ln > s["start_line"] or s["start_line"] == s["end_line"]):
                    hits[s["id"]] += 1  # the def line itself runs at import time, not on call
            for sid, n in hits.items():
                conn.execute("INSERT OR REPLACE INTO covlinks VALUES(?,?,?,?)", (t, sid, n, fhash))
                links += 1
    conn.execute("DELETE FROM covrun")
    conn.execute("INSERT INTO covrun VALUES(?,?,?,?)", (time.time(), command, len(seen_tests), files))
    conn.commit()
    rq.link(conn)  # materialise `covers` edges
    return {"tests_with_data": len(seen_tests), "files": files, "symbol_links": links,
            "files_skipped_stale": stale_files, "unmapped_contexts": [c for c, _ in unmapped.most_common(5)]}


def materialize(conn) -> None:
    ids = {r["id"] for r in conn.execute("SELECT id FROM nodes WHERE kind IN ('function','method','class')")}
    for r in conn.execute("SELECT test, symbol, hits FROM covlinks").fetchall():
        if r["test"] in ids and r["symbol"] in ids:
            conn.execute("INSERT INTO edges VALUES(?,?,?,?,?,?)", (r["test"], r["symbol"], "covers", "runtime", 1.0, None))


# ------------------------------------------------------------------ queries
def _chunks(xs, n=400):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def tests_covering(conn, symbol_ids) -> dict[str, list[str]]:
    """test id -> symbols (among `symbol_ids`, class members included) it executed."""
    ids = set(symbol_ids)
    for chunk in _chunks(list(ids)):
        for r in conn.execute(f"SELECT dst FROM edges WHERE kind='contains' AND src IN ({','.join('?' * len(chunk))})", chunk):
            ids.add(r["dst"])
    out: dict[str, list[str]] = defaultdict(list)
    for chunk in _chunks(ids):
        for r in conn.execute(f"SELECT src, dst FROM edges WHERE kind='covers' AND dst IN ({','.join('?' * len(chunk))})", chunk):
            out[r["src"]].append(r["dst"])
    return dict(out)


def tests_with_data(conn) -> set[str]:
    return {r["test"] for r in conn.execute("SELECT DISTINCT test FROM covlines")}


def status(conn) -> dict:
    r = conn.execute("SELECT * FROM covrun").fetchone()
    if not r:
        return {"available": False, "hint": "run: tracegraph coverage run --repo <repo>"}
    return {"available": True, "tests": r["tests"], "files": r["files"], "command": r["command"],
            "ran_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["ran_at"]))}
