"""Local web UI backend (stdlib only). Binds to 127.0.0.1; reads files only via paths stored in the graph."""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import impact as imp, llm, requirements as rq, semantic
from .agent_api import Workspace
from .graph import connect, index_repo
from .llm import LLMConfig, LLMError

UI_FILE = Path(__file__).parent / "ui" / "index.html"


def _pytest_id(nid_: str) -> str:
    p, q = nid_.split("::", 1)
    return p + "::" + q.replace(".", "::")


class App:
    def __init__(self, repo, db=None, requirements=None):
        self.lock = threading.Lock()
        self.job = None
        self.open(repo, db, requirements)

    def open(self, repo, db=None, requirements=None):
        if not Path(repo).is_dir():
            raise ValueError(f"not a directory: {repo}")
        self.ws = Workspace(repo, db, requirements, auto_index=False)
        self.ws.refresh()  # builds the graph on first open; no-op when a graph already exists

    # ---- GET
    def status(self, **_):
        c = connect(self.ws.db)
        n = lambda sql: c.execute(sql).fetchone()[0]
        calls = n("SELECT count(*) FROM edges WHERE kind='calls'")
        return {"repo": str(self.ws.repo), "requirements_file": str(self.ws.req_file or ""),
                "files": n("SELECT count(*) FROM files"),
                "symbols": n("SELECT count(*) FROM nodes WHERE kind IN ('class','function','method')"),
                "tests": n("SELECT count(*) FROM nodes WHERE is_test=1 AND kind IN ('function','method')"),
                "routes": n("SELECT count(*) FROM nodes WHERE kind='route'"),
                "edges": n("SELECT count(*) FROM edges WHERE provenance!='declared'"), "calls": calls,
                "heuristic_calls": n("SELECT count(*) FROM edges WHERE kind='calls' AND provenance='heuristic'"),
                "requirements": n("SELECT count(*) FROM requirements"),
                "diagnostics": n("SELECT count(*) FROM diagnostics"),
                "summaries": n("SELECT count(*) FROM semantics")}

    def requirements(self, q="", status="", **_):
        rows = rq.report(connect(self.ws.db))
        out = []
        for r in rows:
            if status and r["status"] != status:
                continue
            if q and q.lower() not in (r["id"] + " " + r["title"]).lower():
                continue
            cov = "no-code" if not r["implementations"] else ("tested" if r["tests_reaching"] else "untested")
            out.append({"id": r["id"], "title": r["title"], "status": r["status"], "coverage": cov,
                        "implementations": len(r["implementations"]), "tests": len(r["tests_reaching"]),
                        "declared_tests": len(r["declared_tests"]), "unmatched": r["unmatched_patterns"], "inferred": r["inferred"]})
        return {"requirements": out}

    def trace(self, id="", **_):
        c = connect(self.ws.db)
        row = c.execute("SELECT * FROM requirements WHERE id=?", (id,)).fetchone()
        if not row:
            return {"error": f"unknown requirement {id}"}
        data = json.loads(row["data"])
        rid = f"REQ:{id}"
        declared = [_pytest_id(r["dst"]) for r in c.execute(
            "SELECT dst FROM edges WHERE src=? AND kind='verified_by'", (rid,))]
        diag = {r["node_id"]: r["n"] for r in c.execute("SELECT node_id, count(*) n FROM diagnostics GROUP BY node_id")}
        summ = {r["node_id"]: r["summary"] for r in c.execute("SELECT node_id, summary FROM semantics")}
        why = {r["dst"]: r["rationale"] for r in c.execute("SELECT dst, rationale FROM inferences WHERE src=? AND kind='implements'", (rid,))}
        impls = []
        for r in c.execute("SELECT n.*, e.provenance prov, e.confidence conf FROM edges e JOIN nodes n ON n.id=e.dst "
                           "WHERE e.src=? AND e.kind='implements' "
                           "ORDER BY n.path, n.start_line", (rid,)).fetchall():
            res = imp.impact(c, {r["id"]: []}, depth=6)
            callees = [x["dst"] for x in c.execute(
                "SELECT dst FROM edges WHERE src=? AND kind='calls' ORDER BY line", (r["id"],))]
            impls.append({"id": r["id"], "kind": r["kind"], "lines": f"{r['start_line']}-{r['end_line']}",
                          "diagnostics": diag.get(r["id"], 0), "summary": summ.get(r["id"], ""),
                          "inferred": r["prov"] == "inferred", "confidence": r["conf"], "rationale": why.get(r["id"], ""),
                          "calls": [x for x in callees if not x.startswith("ext::")][:10],
                          "external_calls": len([x for x in callees if x.startswith("ext::")]),
                          "tests": [{"pytest_id": t["pytest_id"], "distance": t["distance"],
                                     "declared": t["pytest_id"] in declared} for t in res["tests"][:10]]})
        return {"id": id, "title": row["title"], "status": row["status"], "description": row["description"],
                "design": data.get("design", []), "implementations": impls, "declared_tests": declared,
                "unmatched": [p for p in data.get("implemented_by", []) or []
                              if not any(p.split("::")[0] in i["id"] for i in impls)]}

    def search(self, q="", **_):
        return self.ws.find_symbol(q, 25) if q else {"matches": []}

    def symbol(self, id="", **_):
        return self.ws.get_context(id, include_source=True, max_edges=40)

    def changed(self, base="HEAD", **_):
        self.ws.refresh(force=True)
        c = connect(self.ws.db)
        try:
            seeds = imp.seeds_from_diff(c, self.ws.repo, base)
        except Exception as e:
            return {"error": f"git diff failed ({e}). Is this a git repository?"}
        out = []
        for sid, lines in seeds.items():
            n = c.execute("SELECT * FROM nodes WHERE id=?", (sid,)).fetchone()
            out.append({"id": sid, "kind": n["kind"], "lines": f"{n['start_line']}-{n['end_line']}",
                        "changed_lines": sorted(set(lines))[:20]})
        return {"changed": out}

    # ---- POST
    def index(self, requirements="", ruff=False, **_):
        req = Path(requirements) if requirements else self.ws.req_file
        if req and not req.is_absolute():
            req = self.ws.repo / req
        if req:
            self.ws.req_file = req
        stats = index_repo(self.ws.repo, self.ws.db, requirements=req, quality=["ruff"] if ruff else None)
        return {"stats": stats, "status": self.status()}

    def repo(self, path="", requirements="", **_):
        self.open(Path(path).expanduser(), None, requirements or None)
        return self.status()

    def impact(self, targets=(), use_diff=False, base="HEAD", depth=6, min_confidence=0.0, **_):
        if use_diff:
            self.ws.refresh(force=True)
        c = connect(self.ws.db)
        seeds = imp.seeds_from_diff(c, self.ws.repo, base) if use_diff else {}
        for t in targets:
            tid = imp.resolve_target(c, t)
            if not tid:
                return {"error": f"cannot resolve {t}"}
            seeds.setdefault(tid, [])
        if not seeds:
            return {"error": "nothing selected: load the git diff or add symbols first"}
        res = imp.impact(c, seeds, int(depth), float(min_confidence))
        return {"result": res, "graph": _graph(res)}

    # ---- LLM layer
    def _cfg(self):
        return llm.resolve_config(self.ws.repo)

    def llm_config(self, **_):
        cfg = self._cfg()
        return {"provider": cfg.provider, "model": cfg.model, "base_url": cfg.base_url, "ready": cfg.ready,
                "usage": llm.usage_summary(connect(self.ws.db))}

    def llm_set(self, provider="", model="", base_url="", **_):
        llm.save_config(self.ws.repo, LLMConfig(provider.lower(), model, base_url))
        return self.llm_config()

    def llm_models(self, **_):
        cfg = self._cfg()
        cfg.model = cfg.model or "-"
        return {"models": llm.make_client(cfg).models()}

    def llm_run(self, task="", limit=100, min_confidence=0.6, force=False, **_):
        if self.job and self.job["state"] == "running":
            return {"error": "a job is already running"}
        if task not in ("test", "summarize", "link", "draft"):
            return {"error": f"unknown task {task}"}
        client = llm.make_client(self._cfg())
        repo, db = self.ws.repo, self.ws.db
        job = self.job = {"task": task, "state": "running", "done": 0, "total": 0, "message": "starting", "result": None}

        def prog(n, total, what):
            job.update(done=n, total=total, message=str(what)[-70:])

        def run():
            conn = connect(db)
            try:
                if task == "test":
                    res = llm.ping(client)
                elif task == "summarize":
                    res = semantic.summarize(repo, conn, client, limit=int(limit), force=bool(force), progress=prog)
                elif task == "link":
                    res = semantic.infer_links(repo, conn, client, float(min_confidence), progress=prog)
                else:
                    res = semantic.draft_requirements(repo, conn, client, progress=prog)
                job.update(state="done", result=res, message="finished")
            except Exception as e:  # shown in the UI; the job thread must never die silently
                job.update(state="error", message=f"{type(e).__name__}: {e}")
            finally:
                conn.close()

        threading.Thread(target=run, daemon=True).start()
        return {"started": task}

    def llm_job(self, **_):
        return self.job or {"state": "idle"}


def _graph(res):
    nodes, edges = {}, set()

    def label(i):
        return i.split("::", 1)[-1] if "::" in i else i

    def add(i, role, d=0):
        nodes.setdefault(i, {"id": i, "label": label(i), "role": role, "distance": d})

    for c in res["changed"]:
        add(c["id"], "changed")
    for it in res["impacted"] + res["tests"]:
        add(it["id"], "test" if it["is_test"] else "impacted", it["distance"])
    for it in res["impacted"] + res["tests"]:
        for a, b in zip(it["chain"], it["chain"][1:]):
            add(a, "impacted"), add(b, "impacted")
            edges.add((a, b))
    for r in res["requirements"]:
        add("REQ:" + r["id"], "requirement", r["distance"])
        for v in r["via"]:
            add(v, "impacted")
            edges.add((v, "REQ:" + r["id"]))
    return {"nodes": list(nodes.values()), "edges": sorted(edges)}


class Handler(BaseHTTPRequestHandler):
    app: App

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _dispatch(self, method, params):
        url = urlparse(self.path)
        if method == "GET" and url.path == "/":
            return self._send(200, UI_FILE.read_bytes(), "text/html")
        name = url.path.removeprefix("/api/").replace("/", "_")
        allowed = {"GET": {"status", "requirements", "trace", "search", "symbol", "changed",
                           "llm_config", "llm_models", "llm_job"},
                   "POST": {"index", "repo", "impact", "llm_set", "llm_run"}}[method]
        if not url.path.startswith("/api/") or name not in allowed:
            return self._send(404, {"error": "not found"})
        try:
            with self.app.lock:
                out = getattr(self.app, name)(**params)
            self._send(200 if "error" not in out else 400, out)
        except LLMError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # surface errors to the UI instead of dropping the connection
            self._send(500, {"error": f"{e.__class__.__name__}: {e}"})

    def do_GET(self):
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        self._dispatch("GET", q)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        self._dispatch("POST", body)


def serve(repo=".", db=None, requirements=None, port=8765, open_browser=True):
    Handler.app = App(repo, db, requirements)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"tracegraph UI on {url}  (repo: {Handler.app.ws.repo})  Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
