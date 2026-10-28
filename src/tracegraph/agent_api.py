"""Agent-facing queries over the graph. Compact, token-cheap dicts; no MCP dependency (testable on its own)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import impact as imp, requirements as rq, semantic as sem
from .graph import connect, index_repo

REQ_FILES = ("requirements.yaml", "requirements.yml")


class Workspace:
    def __init__(self, repo=".", db=None, requirements=None, auto_index=True):
        self.repo = Path(repo).resolve()
        self.db = Path(db) if db else self.repo / ".tracegraph" / "graph.db"
        self.auto_index = auto_index
        self.req_file = Path(requirements) if requirements else next(
            (self.repo / f for f in REQ_FILES if (self.repo / f).exists()), None)
        self._last = 0.0

    def refresh(self, force=False) -> dict | None:
        """Incremental re-index (unchanged files are skipped). Throttled to once per 2s unless forced."""
        if not (force or (self.auto_index and time.time() - self._last > 2)) and self.db.exists():
            return None
        stats = index_repo(self.repo, self.db, requirements=self.req_file)
        self._last = time.time()
        return stats

    def conn(self):
        self.refresh()
        return connect(self.db)

    # ---------------------------------------------------------------- queries
    def find_symbol(self, query: str, limit: int = 10) -> dict:
        c = self.conn()
        rows = c.execute("""SELECT id, kind, start_line, end_line FROM nodes
            WHERE kind IN ('class','function','method','module') AND (name=? OR id LIKE ?)
            ORDER BY (name=?) DESC, length(id) LIMIT ?""", (query, f"%{query}%", query, limit)).fetchall()
        return {"matches": [{"id": r["id"], "kind": r["kind"], "lines": f"{r['start_line']}-{r['end_line']}"}
                            for r in rows]}

    def _resolve(self, c, target):
        t = imp.resolve_target(c, target)
        if t:
            return t, None
        cands = [r["id"] for r in c.execute(
            "SELECT id FROM nodes WHERE kind!='external' AND (name=? OR id LIKE ?) LIMIT 8", (target, f"%{target}%"))]
        return None, {"error": f"could not resolve '{target}' to one symbol",
                      "candidates": cands, "hint": "use an id like path.py::Class.method or path.py:LINE"}

    def _source(self, node, max_lines=120) -> str | None:
        if node["kind"] in ("external", "requirement") or not node["path"]:
            return None
        try:
            lines = (self.repo / node["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        a, b = node["start_line"], node["end_line"]
        body = [f"{i}: {lines[i - 1]}" for i in range(a, min(b, a + max_lines - 1) + 1)]
        if b - a + 1 > max_lines:
            body.append(f"... ({b - a + 1 - max_lines} more lines)")
        return "\n".join(body)

    def get_context(self, target: str, include_source: bool = True, max_edges: int = 20) -> dict:
        c = self.conn()
        t, err = self._resolve(c, target)
        if err:
            return err
        n = c.execute("SELECT * FROM nodes WHERE id=?", (t,)).fetchone()

        def edges(sql):
            rows = c.execute(sql, (t,)).fetchall()
            out = [{"id": r["x"], "line": r["line"], "confidence": r["c"],
                    **({"heuristic": True} if r["p"] == "heuristic" else {})} for r in rows[:max_edges]]
            return out + ([{"truncated": len(rows) - max_edges}] if len(rows) > max_edges else [])

        res = imp.impact(c, {t: []}, depth=5)
        out = {"symbol": {"id": t, "kind": n["kind"], "lines": f"{n['start_line']}-{n['end_line']}",
                          "is_test": bool(n["is_test"]), **json.loads(n["attrs"] or "{}")}}
        if include_source:
            out["source"] = self._source(n)
        out["intent"] = sem.semantics_for(c, self.repo, n)
        out["calls"] = edges("SELECT dst x, line, confidence c, provenance p FROM edges WHERE src=? AND kind='calls' ORDER BY line")
        out["called_by"] = edges("SELECT src x, line, confidence c, provenance p FROM edges WHERE dst=? AND kind='calls' ORDER BY src")
        out["inherits"] = [r["x"] for r in c.execute("SELECT dst x FROM edges WHERE src=? AND kind='inherits'", (t,))]
        out["subclasses"] = [r["x"] for r in c.execute("SELECT src x FROM edges WHERE dst=? AND kind='inherits'", (t,))]
        out["tests_reaching_this"] = [{"pytest_id": x["pytest_id"], "distance": x["distance"]} for x in res["tests"][:15]]
        out["requirements"] = [{"id": r["id"], "title": r["title"], "status": r["status"],
                                "relation": "implements" if r["distance"] == 0 else f"via callers (distance {r['distance']})",
                                **({"inferred_link": True, "confidence": r["confidence"]} if r.get("inferred") else {})}
                               for r in res["requirements"]]
        out["diagnostics"] = [{"tool": d["tool"], "code": d["code"], "line": d["line"], "message": d["message"]}
                              for d in c.execute("SELECT * FROM diagnostics WHERE node_id=?", (t,))]
        return {k: v for k, v in out.items() if v not in (None, [], "")}

    @staticmethod
    def _compact(res: dict, limit: int = 40) -> dict:
        imp_ = [{"id": i["id"], "distance": i["distance"], "confidence": i["confidence"],
                 "calls_at_line": i["call_line"], **({"heuristic": True} if i["provenance"] != "static" else {})}
                for i in res["impacted"]]
        tests = [t["pytest_id"] for t in res["tests"]]  # ordered: ran the changed lines > ran the function > static
        out = {"changed": [{"id": c["id"], "lines": f"{c['start']}-{c['end']}",
                            **({"changed_lines": c["changed_lines"][:10]} if c["changed_lines"] else {}),
                            **({"diagnostics": c["diagnostics"]} if c["diagnostics"] else {})} for c in res["changed"]],
               "affected": imp_[:limit],
               "affected_total": len(imp_),
               "tests_to_run": tests,
               "requirements": [{"id": r["id"], "title": r["title"], "status": r["status"], "distance": r["distance"],
                                 "declared_tests": r["declared_tests"],
                                 **({"inferred_link": True, "confidence": r["confidence"]} if r.get("inferred") else {})}
                                for r in res["requirements"]]}
        executed = [t["pytest_id"] for t in res["tests"] if any(e.startswith("runtime") for e in t["evidence"])
                    and t.get("line_check") != "miss"]
        if executed:
            out["tests_that_executed_changed_code"] = executed  # measured, not inferred: run these first
        out["runtime_coverage"] = ("available" if res["runtime"]["available"] else
                                   "not imported: run `tracegraph coverage run` to see which tests really execute this code")
        if tests:
            out["run_command"] = "pytest " + " ".join(tests)
        out["note"] = ("confidence<0.9 / heuristic edges come from name matching without type inference; "
                       "treat as 'possibly affected'")
        return out

    @staticmethod
    def _with_summaries(c, out, n=15):
        items = out.get("changed", []) + out.get("affected", [])[:n]
        ids = [x["id"] for x in items]
        if ids:
            rows = {r["node_id"]: r["summary"] for r in c.execute(
                f"SELECT node_id, summary FROM semantics WHERE node_id IN ({','.join('?' * len(ids))})", ids)}
            for x in items:
                if x["id"] in rows:
                    x["summary"] = rows[x["id"]]
        return out

    def search_by_intent(self, query: str, limit: int = 10) -> dict:
        return {"matches": sem.search_by_intent(self.conn(), query, limit)}

    def impact_of_diff(self, base: str = "HEAD", depth: int = 6, min_confidence: float = 0.0) -> dict:
        c = self.conn()
        try:
            res = imp.impact_for_diff(c, self.repo, base, depth, min_confidence)
        except Exception as e:
            return {"error": f"git diff failed: {e}"}
        if res is None:
            return {"changed": [], "note": f"no changed Python symbols vs {base}"}
        return self._with_summaries(c, self._compact(res))

    def impact_of_symbols(self, targets: list[str], depth: int = 6, min_confidence: float = 0.0) -> dict:
        c = self.conn()
        seeds = {}
        for s in targets:
            t, err = self._resolve(c, s)
            if err:
                return err
            seeds[t] = []
        return self._with_summaries(c, self._compact(imp.impact(c, seeds, depth, min_confidence)))

    def requirements_for(self, target: str) -> dict:
        c = self.conn()
        t, err = self._resolve(c, target)
        if err:
            return err
        res = imp.impact(c, {t: []}, depth=6)
        return {"symbol": t, "requirements": [
            {"id": r["id"], "title": r["title"], "status": r["status"],
             "relation": "implements" if r["distance"] == 0 else f"depends on this via callers (distance {r['distance']})",
             **({"inferred_link": True, "confidence": r["confidence"]} if r.get("inferred") else {})}
            for r in res["requirements"]]}

    def list_requirements(self, status: str | None = None) -> dict:
        rows = rq.report(self.conn())
        return {"requirements": [
            {"id": r["id"], "title": r["title"], "status": r["status"], "implemented_by": r["implementations"],
             "tests_reaching": r["tests_reaching"], "declared_tests": r["declared_tests"],
             **({"inferred_links": r["inferred"]} if r["inferred"] else {}),
             **({"unmatched_patterns": r["unmatched_patterns"]} if r["unmatched_patterns"] else {}),
             **({"design": r["design"]} if r["design"] else {})}
            for r in rows if not status or r["status"] == status]}

    def reindex(self) -> dict:
        return self.refresh(force=True) or {}
