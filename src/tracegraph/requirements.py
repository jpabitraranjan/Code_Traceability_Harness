"""Requirements: load from YAML, link to code by pattern, report implementation/test coverage.

requirements.yaml:
  requirements:
    - id: REQ-001
      title: Tax is computed per line item
      status: implemented            # planned | partial | implemented
      description: ...
      design: ["Rates live in pkg/rates.py"]   # design decisions (free text)
      implemented_by: ["pkg/billing.py::calculate_tax", "pkg/rates.py"]   # fnmatch on node ids
      verified_by: ["tests/test_billing.py::*"]
"""
from __future__ import annotations

import fnmatch
import json

import yaml
import networkx as nx


def load(conn, path) -> int:
    data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    reqs = data.get("requirements", data if isinstance(data, list) else [])
    conn.execute("DELETE FROM requirements")
    for r in reqs:
        conn.execute("INSERT INTO requirements VALUES(?,?,?,?,?)",
                     (str(r["id"]), r.get("title", ""), r.get("status", "planned"),
                      r.get("description", ""), json.dumps(r)))
    conn.commit()
    return len(reqs)


def _patterns(pats):
    out = []
    for p in pats or []:
        out.append(p if "::" in p else f"{p}::*")
    return out


def link(conn) -> None:
    conn.execute("DELETE FROM edges WHERE provenance IN ('declared', 'inferred', 'runtime')")
    conn.execute("DELETE FROM nodes WHERE kind = 'requirement'")
    code = [r["id"] for r in conn.execute("SELECT id FROM nodes WHERE kind NOT IN ('external','requirement')")]
    for r in conn.execute("SELECT * FROM requirements").fetchall():
        d = json.loads(r["data"])
        rid = f"REQ:{r['id']}"
        conn.execute("INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
                     (rid, "requirement", r["title"], "", 0, 0, 0, json.dumps({"status": r["status"]})))
        for key, kind in (("implemented_by", "implements"), ("verified_by", "verified_by")):
            for pat in _patterns(d.get(key)):
                for n in fnmatch.filter(code, pat):
                    if "<module>" in n and "<module>" not in pat:
                        continue
                    conn.execute("INSERT INTO edges VALUES(?,?,?,?,?,?)", (rid, n, kind, "declared", 1.0, None))
    known, codeset = {f"REQ:{r['id']}" for r in conn.execute("SELECT id FROM requirements")}, set(code)
    for r in conn.execute("SELECT * FROM inferences").fetchall():  # LLM-proposed links, kept apart from declared ones
        if r["src"] in known and r["dst"] in codeset:
            conn.execute("INSERT INTO edges VALUES(?,?,?,?,?,?)",
                         (r["src"], r["dst"], r["kind"], "inferred", r["confidence"], None))
    from . import covlink
    covlink.materialize(conn)  # runtime `covers` edges survive re-indexing
    conn.commit()


def report(conn) -> list[dict]:
    """View A data: each requirement with status, implementing symbols, and tests reaching them."""
    from .graph import to_networkx, CALL_KINDS
    G = to_networkx(conn, kinds=CALL_KINDS)
    D = nx.DiGraph()
    D.add_nodes_from(G.nodes)
    for s, t in G.edges():
        D.add_edge(t, s)  # dependents direction
    tests = {n for n, a in G.nodes(data=True) if a.get("is_test") and a.get("kind") in ("function", "method")}
    rows = []
    has_cov = conn.execute("SELECT 1 FROM covrun").fetchone() is not None
    executed = {r["dst"] for r in conn.execute("SELECT DISTINCT dst FROM edges WHERE kind='covers'")} if has_cov else set()
    kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM nodes")} if has_cov else {}
    for r in conn.execute("SELECT * FROM requirements ORDER BY id").fetchall():
        d = json.loads(r["data"])
        rid = f"REQ:{r['id']}"
        impl = [x["dst"] for x in conn.execute("SELECT dst FROM edges WHERE src=? AND kind='implements'", (rid,))]
        declared = [x["dst"] for x in conn.execute("SELECT dst FROM edges WHERE src=? AND kind='verified_by'", (rid,))]
        reach = set()
        for s in impl:
            if s in D:
                reach |= (nx.descendants(D, s) | {s}) & tests
        runtime = set()
        for chunk in (impl[i:i + 400] for i in range(0, len(impl), 400)):
            runtime |= {x["src"] for x in conn.execute(
                f"SELECT src FROM edges WHERE kind='covers' AND dst IN ({','.join('?' * len(chunk))})", chunk)}
        reach |= runtime  # tests that executed the code at runtime, even where static analysis sees no path
        unmatched = [p for p in d.get("implemented_by", []) or []
                     if not any(fnmatch.fnmatch(i, p if "::" in p else p + "::*") for i in impl)]
        n_inf = conn.execute("SELECT count(*) FROM edges WHERE src=? AND kind='implements' AND provenance='inferred'",
                             (rid,)).fetchone()[0]
        rows.append({"id": r["id"], "title": r["title"], "status": r["status"], "inferred": n_inf,
                     "implementations": impl, "unmatched_patterns": unmatched,
                     "tests_reaching": sorted(reach), "runtime_tests": sorted(runtime),
                     "unexecuted": [i for i in impl if has_cov and i not in executed and kinds.get(i) in ("function", "method")], "declared_tests": declared,
                     "design": d.get("design", [])})
    return rows
