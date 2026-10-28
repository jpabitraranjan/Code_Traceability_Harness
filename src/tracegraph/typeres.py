"""Type-aware call resolution via Pyright's language server (LSP `textDocument/definition`).

Name matching cannot tell which `total()` is meant in `order.total()`. Pyright can: it infers the type of `order`.
Only call sites the resolver left heuristic/unresolved are asked (or all of them with include_static=True, which
cross-checks the static edges and reports agreement). Answers are stored per file hash in `resolutions`, so they
survive re-indexing while the file is unchanged and are discarded automatically when it changes.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .llm import LLMError

NOISE = "-"  # Pyright answered, and the answer is builtins/typing: not repo code, nothing worth an edge
SKIP_EXT = {"builtins", "typing", "_typeshed", "typing_extensions", "types", "abc", "collections.abc"}
BATCH = 50


class TypeResError(LLMError):
    """User-facing setup/runtime problem (kept a subclass of LLMError so CLI and UI report it the same way)."""


def find_server() -> list[str]:
    for name in ("pyright-langserver", "pyright-python-langserver"):
        if shutil.which(name):
            return [shutil.which(name)]
    raise TypeResError("Pyright not found. Install it with: pip install pyright   (it also needs Node.js)")


def file_uri(p: Path) -> str:
    return "file://" + quote(str(Path(p).resolve()))


def utf16_col(line: str, byte_col: int) -> int:
    """tree-sitter columns are UTF-8 byte offsets; LSP positions are UTF-16 code units."""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore").encode("utf-16-le")) // 2


class LSP:
    def __init__(self, cmd: list[str], root: Path, python: str | None = None):
        self.root, self.python, self.next, self.q = Path(root).resolve(), python, 1, queue.Queue()
        self.diags: dict[str, list] = {}  # uri -> latest diagnostics (used to explain why answers are missing)
        try:
            self.p = subprocess.Popen([*cmd, "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, cwd=self.root)
        except OSError as e:
            raise TypeResError(f"could not start {cmd[0]}: {e}")
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        f = self.p.stdout
        while True:
            headers, line = {}, f.readline()
            if not line:
                self.q.put(None)
                return
            while line.strip():
                k, _, v = line.decode("ascii", "replace").partition(":")
                headers[k.strip().lower()] = v.strip()
                line = f.readline()
            self.q.put(json.loads(f.read(int(headers.get("content-length", 0))) or b"{}"))

    def _send(self, obj):
        data = json.dumps(obj).encode()
        try:
            self.p.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            raise TypeResError("pyright-langserver exited unexpectedly")

    def request(self, method, params) -> int:
        rid, self.next = self.next, self.next + 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def wait(self, ids: set[int], timeout: float) -> dict:
        got, deadline = {}, time.time() + timeout
        while len(got) < len(ids):
            try:
                m = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                raise TypeResError(f"timed out after {timeout:.0f}s waiting for Pyright")
            if m is None:
                raise TypeResError("pyright-langserver exited unexpectedly")
            if "method" in m:
                self._server_msg(m)
            elif m.get("id") in ids:
                got[m["id"]] = None if "error" in m else m.get("result")
            if time.time() > deadline:
                raise TypeResError(f"timed out after {timeout:.0f}s waiting for Pyright")
        return got

    def _server_msg(self, m):
        if "id" in m:  # server -> client request: must be answered or Pyright stalls
            items = (m.get("params") or {}).get("items", [])
            result = [{"pythonPath": self.python} if self.python and it.get("section") == "python" else {}
                      for it in items] if m["method"] == "workspace/configuration" else None
            self._send({"jsonrpc": "2.0", "id": m["id"], "result": result})
        elif m["method"] == "textDocument/publishDiagnostics":
            p = m.get("params") or {}
            self.diags[p.get("uri", "")] = p.get("diagnostics", [])

    def drain(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            try:
                m = self.q.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                return
            if m is None:
                return
            if "method" in m:
                self._server_msg(m)

    def start(self):
        uri = file_uri(self.root)
        rid = self.request("initialize", {
            "processId": os.getpid(), "rootUri": uri, "workspaceFolders": [{"uri": uri, "name": "repo"}],
            "capabilities": {"workspace": {"configuration": True}, "textDocument": {"definition": {"linkSupport": True}}}})
        self.wait({rid}, 240)  # first run may download the Pyright package
        self.notify("initialized", {})

    def close(self):
        try:
            self.wait({self.request("shutdown", None)}, 5)
            self.notify("exit", None)
            self.p.wait(timeout=3)
        except Exception:
            pass
        finally:
            if self.p.poll() is None:
                self.p.kill()


# ------------------------------------------------------------------ mapping answers back to graph nodes
def locations(res) -> list[tuple[str, dict]]:
    out = []
    for it in ([] if not res else res if isinstance(res, list) else [res]):
        if "targetUri" in it:
            out.append((it["targetUri"], it.get("targetSelectionRange") or it["targetRange"]))
        elif "uri" in it:
            out.append((it["uri"], it["range"]))
    return out


def _ident(line: str, ch: int) -> str | None:
    m = re.match(r"\w+", line[ch:])
    return m.group(0) if m else None


def ext_name(path: Path, ident: str) -> str | None:
    """Module path of a library definition + identifier, e.g. .../site-packages/django/db/models/query.py + filter
    -> ext::django.db.models.query.filter. Builtins/typing stubs return None (they would only add noise)."""
    parts = path.parts
    tail = None
    if "typeshed-fallback" in parts or "typeshed" in parts:  # stubs bundled with Pyright live inside site-packages
        i = parts.index("typeshed-fallback") if "typeshed-fallback" in parts else parts.index("typeshed")
        kind = parts[i + 1] if i + 1 < len(parts) else ""
        tail = list(parts[i + 2:]) if kind == "stdlib" else list(parts[i + 3:]) if kind == "stubs" else None
    else:
        for marker in ("site-packages", "dist-packages"):
            if marker in parts:
                tail = list(parts[parts.index(marker) + 1:])
                break
        else:
            for i, part in enumerate(parts):  # system stdlib, e.g. /usr/lib/python3.12/json/decoder.py
                if re.fullmatch(r"python3\.\d+", part):
                    tail = list(parts[i + 1:])
                    break
    if not tail:
        return None
    stem = re.sub(r"\.pyi?$", "", tail[-1])
    mod = ".".join(tail[:-1] + ([] if stem == "__init__" else [stem]))
    if not mod or mod in SKIP_EXT or mod.split(".")[0] in ("builtins", "typing", "_typeshed"):
        return NOISE
    return f"ext::{mod}.{ident}"


def to_node(conn, repo: Path, uri: str, rng: dict, fallback_ident: str, cache: dict) -> str | None:
    p = Path(unquote(urlparse(uri).path))
    line0, ch = rng["start"]["line"], rng["start"]["character"]
    if p not in cache:
        try:
            cache[p] = p.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            cache[p] = []
    lines = cache[p]
    ident = _ident(lines[line0], ch) if line0 < len(lines) else None
    try:
        rel = p.resolve().relative_to(repo).as_posix()
    except ValueError:
        return ext_name(p, ident or fallback_ident)
    if not ident:
        return None
    r = conn.execute("""SELECT id FROM nodes WHERE path=? AND kind IN ('class','function','method') AND name=?
                        AND start_line<=? AND end_line>=? ORDER BY (end_line-start_line) LIMIT 1""",
                     (rel, ident, line0 + 1, line0 + 1)).fetchone()
    return r["id"] if r else None  # e.g. the definition is a variable, not a function/class


# ------------------------------------------------------------------ driver
def _count(conn, where="") -> int:
    return conn.execute(f"SELECT count(*) FROM edges WHERE kind='calls' {where}").fetchone()[0]


def resolve(repo, conn, include_static=False, python=None, limit=None, progress=None, server=None) -> dict:
    from . import requirements as rq
    from .graph import build_graph

    from .graph import index_is_current

    repo = Path(repo).resolve()
    if not index_is_current(conn):
        raise TypeResError(f"the index for {repo} is empty or was built by an older tracegraph version. "
                           f"Run first: tracegraph index --repo {repo}")
    hashes = {r["path"]: r["hash"] for r in conn.execute("SELECT path, hash FROM files")}
    done = {(r["path"], r["nline"], r["col"]) for r in conn.execute("SELECT path, nline, col, file_hash FROM resolutions")
            if hashes.get(r["path"]) == r["file_hash"]}
    q = "SELECT * FROM callsites WHERE callee LIKE '%.%'" + ("" if include_static else " AND status IN ('heuristic','unresolved')")
    sites = [s for s in conn.execute(q + " ORDER BY path, nline, col") if (s["path"], s["nline"], s["col"]) not in done]
    if limit:
        sites = sites[:int(limit)]
    before = {"heuristic": _count(conn, "AND provenance='heuristic'"), "typed": _count(conn, "AND provenance='typed'")}
    stats = {"sites_asked": len(sites), "resolved_in_repo": 0, "resolved_external": 0, "no_answer": 0,
             "heuristic_edges_before": before["heuristic"], "agree": 0, "disagree": 0, "external_only": 0, "builtin_calls": 0,
             "disagreements": []}
    if not sites:
        return {**stats, "message": "nothing to resolve (already answered, or no uncertain call sites)",
                "heuristic_edges_after": before["heuristic"]}

    lsp = LSP(server or find_server(), repo, python)
    cache: dict = {}
    t0 = time.time()
    try:
        lsp.start()
        by_path: dict[str, list] = {}
        for s in sites:
            by_path.setdefault(s["path"], []).append(s)
        n = 0
        for path, group in by_path.items():
            conn.execute("DELETE FROM resolutions WHERE path=? AND file_hash!=?", (path, hashes.get(path)))
            try:
                text = (repo / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines, uri = text.split("\n"), file_uri(repo / path)
            lsp.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}})
            for i in range(0, len(group), BATCH):
                batch, ids = group[i:i + BATCH], {}
                for s in batch:
                    ch = utf16_col(lines[s["nline"] - 1], s["col"]) if s["nline"] - 1 < len(lines) else s["col"]
                    ids[lsp.request("textDocument/definition", {"textDocument": {"uri": uri},
                                    "position": {"line": s["nline"] - 1, "character": ch}})] = s
                got = lsp.wait(set(ids), 180)
                for rid, s in ids.items():
                    n += 1
                    found = []
                    for u, rng in locations(got.get(rid))[:3]:
                        d = to_node(conn, repo, u, rng, s["callee"].rsplit(".", 1)[-1], cache)
                        if d and d not in found:
                            found.append(d)
                    if NOISE in found and len(found) > 1:
                        found.remove(NOISE)
                    ours = set(json.loads(s["targets"] or "[]"))
                    if s["status"] == "static" and found:  # cross-check only; static facts are never overridden
                        repo_found = {d for d in found if d != NOISE and not d.startswith("ext::")}
                        if not repo_found:
                            stats["external_only"] += 1  # Pyright says "library code": names differ, not comparable
                        elif repo_found & ours:
                            stats["agree"] += 1
                        else:
                            stats["disagree"] += 1
                            if len(stats["disagreements"]) < 5:
                                stats["disagreements"].append(f"{path}:{s['nline']} {s['callee']}: static={sorted(ours)} pyright={found}")
                    if found == [NOISE]:
                        stats["builtin_calls"] += 1
                        conn.execute("INSERT OR REPLACE INTO resolutions VALUES(?,?,?,?,?,?,?,?,?)",
                                     (path, s["nline"], s["col"], s["line"], s["callee"], NOISE, 1.0, hashes.get(path), "pyright"))
                        continue
                    conf = 0.95 if len(found) == 1 else 0.85
                    for d in found or [""]:  # '' = tombstone: asked, no usable answer, don't ask again
                        conn.execute("INSERT OR REPLACE INTO resolutions VALUES(?,?,?,?,?,?,?,?,?)",
                                     (path, s["nline"], s["col"], s["line"], s["callee"], d, conf if d else 0,
                                      hashes.get(path), "pyright"))
                    if not found:
                        stats["no_answer"] += 1
                    elif any(not d.startswith("ext::") for d in found):
                        stats["resolved_in_repo"] += 1
                    else:
                        stats["resolved_external"] += 1
                if progress:
                    progress(n, len(sites), f"{path}")
            lsp.drain()
            lsp.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            conn.commit()
        missing = {mm.group(1).split(".")[0] for ds in lsp.diags.values() for d in ds
                   if (mm := re.search(r'Import "([^"]+)" could not be resolved', d.get("message", "")))}
    finally:
        lsp.close()
    build_graph(conn)  # re-derive edges: typed answers now replace heuristic ones
    rq.link(conn)
    stats.update(heuristic_edges_after=_count(conn, "AND provenance='heuristic'"),
                 typed_edges=_count(conn, "AND provenance='typed'"), seconds=round(time.time() - t0, 1))
    if missing:
        stats["unresolved_imports"] = sorted(missing)[:12]
        stats["hint"] = (f"Pyright cannot see these packages: {', '.join(sorted(missing)[:6])}. Calls into them stay unanswered. "
                         "Install them in the interpreter Pyright uses, or pass --python /path/to/project/venv/bin/python")
    elif len(sites) > 20 and stats["no_answer"] > 0.8 * len(sites):
        stats["hint"] = ("most call sites have no inferable type (untyped function parameters, ORM managers, dynamic "
                         "attributes). That is expected for dynamic code; annotating parameters or adding stubs improves it")
    return stats
