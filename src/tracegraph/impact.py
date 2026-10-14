"""Change-impact: git diff -> changed symbols -> reverse traversal -> affected symbols, tests, requirements."""
from __future__ import annotations

import re
import subprocess
from collections import defaultdict

import networkx as nx

from .graph import CALL_KINDS, to_networkx

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def changed_lines(repo, base="HEAD") -> dict[str, list[tuple[int, int]]]:
    """New-side line ranges changed vs `base` (working tree incl. staged), plus untracked .py files."""
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    path = None
    for line in _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", base, "--", "*.py").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and (m := HUNK.match(line)):
            start, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out[path].append((max(start, 1), max(start, 1) if n == 0 else start + n - 1))
    for p in _git(repo, "ls-files", "--others", "--exclude-standard", "--", "*.py").splitlines():
        out[p].append((1, 10**9))
    return dict(out)


def symbol_at(conn, path: str, line: int):
    """Innermost symbol covering a line; falls back to the module node."""
    r = conn.execute("""SELECT * FROM nodes WHERE path=? AND kind!='module' AND start_line<=? AND end_line>=?
                        ORDER BY (end_line-start_line) ASC LIMIT 1""", (path, line, line)).fetchone()
    return r or conn.execute("SELECT * FROM nodes WHERE id=?", (f"{path}::<module>",)).fetchone()


def seeds_from_diff(conn, repo, base="HEAD") -> dict[str, list[int]]:
    seeds: dict[str, list[int]] = defaultdict(list)
    for path, ranges in changed_lines(repo, base).items():
        n = conn.execute("SELECT end_line FROM nodes WHERE id=?", (f"{path}::<module>",)).fetchone()
        if not n:
            continue
        for a, b in ranges:
            b = min(b, n["end_line"])
            ids = set()
            for ln in range(a, b + 1):
                s = symbol_at(conn, path, ln)
                if s and s["id"] not in ids:
                    ids.add(s["id"])
                    seeds[s["id"]].append(ln)
    return dict(seeds)


def resolve_target(conn, spec: str):
    m = re.match(r"^(.+?):(\d+)$", spec)
    if m:
        r = symbol_at(conn, m.group(1), int(m.group(2)))
        return r["id"] if r else None
    if conn.execute("SELECT 1 FROM nodes WHERE id=?", (spec,)).fetchone():
        return spec
    rows = conn.execute("SELECT id FROM nodes WHERE id LIKE ? OR name=?", (f"%{spec}%", spec)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def impact(conn, seeds: dict[str, list[int]], depth: int = 6, min_conf: float = 0.0) -> dict:
    G = to_networkx(conn, kinds=CALL_KINDS)
    D = nx.DiGraph()  # edges point from a dependency to its dependents
    for s, t, a in G.edges(data=True):
        if a["confidence"] < min_conf:
            continue
        if D.has_edge(t, s) and D[t][s]["confidence"] >= a["confidence"]:
            continue
        D.add_edge(t, s, confidence=a["confidence"], line=a["line"], kind=a["kind"], provenance=a["provenance"])
    node = {r["id"]: r for r in conn.execute("SELECT * FROM nodes")}
    found: dict[str, dict] = {}

    def record(nid_, dist, conf, chain, call_line):
        if nid_ in seeds:
            return
        cur = found.get(nid_)
        if cur is None or (dist, -conf) < (cur["distance"], -cur["confidence"]):
            n = node[nid_]
            found[nid_] = {"id": nid_, "kind": n["kind"], "path": n["path"], "start": n["start_line"],
                           "end": n["end_line"], "distance": dist, "confidence": round(conf, 3),
                           "chain": chain, "call_line": call_line, "is_test": bool(n["is_test"]),
                           "provenance": "static" if conf >= 0.9 else "heuristic"}

    for seed in seeds:
        if seed not in D and seed not in node:
            continue
        paths = nx.single_source_shortest_path(D, seed, cutoff=depth) if seed in D else {}
        for tgt, p in paths.items():
            if len(p) < 2:
                continue
            conf = 1.0
            for u, v in zip(p, p[1:]):
                conf *= D[u][v]["confidence"]
            record(tgt, len(p) - 1, conf, p, D[p[-2]][p[-1]]["line"])
        if node[seed]["kind"] == "module":  # module-level change: importers are affected
            for r in conn.execute("SELECT src, line FROM edges WHERE dst=? AND kind='imports'", (seed,)):
                record(r["src"], 1, 1.0, [seed, r["src"]], r["line"])

    affected = set(seeds) | set(found)
    reqs: dict[str, dict] = {}
    for r in conn.execute("SELECT src, dst FROM edges WHERE kind='implements'"):
        if r["dst"] in affected:
            d = 0 if r["dst"] in seeds else found[r["dst"]]["distance"]
            e = reqs.setdefault(r["src"][4:], {"id": r["src"][4:], "distance": d, "via": []})
            e["distance"] = min(e["distance"], d)
            e["via"].append(r["dst"])
    titles = {r["id"]: (r["title"], r["status"]) for r in conn.execute("SELECT id,title,status FROM requirements")}
    for rid, e in reqs.items():
        e["title"], e["status"] = titles.get(rid, ("", ""))
        e["declared_tests"] = [x["dst"] for x in conn.execute(
            "SELECT dst FROM edges WHERE src=? AND kind='verified_by'", (f"REQ:{rid}",))]

    diag = {r["node_id"]: r["n"] for r in conn.execute("SELECT node_id, count(*) n FROM diagnostics GROUP BY node_id")}
    items = sorted(found.values(), key=lambda x: (x["distance"], -x["confidence"], x["id"]))
    for it in items:
        it["diagnostics"] = diag.get(it["id"], 0)
    tests = [i for i in items if i["is_test"] and i["kind"] in ("function", "method")]
    for t in tests:
        t["pytest_id"] = t["path"] + "::" + t["id"].split("::", 1)[1].replace(".", "::")
    changed = []
    for sid, lines in seeds.items():
        n = node[sid]
        changed.append({"id": sid, "kind": n["kind"], "path": n["path"], "start": n["start_line"],
                        "end": n["end_line"], "changed_lines": sorted(set(lines))[:50],
                        "diagnostics": diag.get(sid, 0)})
    return {"changed": changed, "impacted": [i for i in items if not i["is_test"]], "tests": tests,
            "requirements": sorted(reqs.values(), key=lambda x: (x["distance"], x["id"]))}
