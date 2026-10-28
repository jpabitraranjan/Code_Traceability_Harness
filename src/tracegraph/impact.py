"""Change-impact: git diff -> changed symbols -> reverse traversal -> affected symbols, tests, requirements."""
from __future__ import annotations

import hashlib
import re
import subprocess
from collections import defaultdict

import networkx as nx

from . import covlink
from .graph import CALL_KINDS, to_networkx

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
HUNK2 = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


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


def diff_ranges(repo, base="HEAD") -> dict[str, dict[str, list[tuple[int, int]]]]:
    """Per file: {"old": [(a, b)], "new": [(a, b)]} line ranges changed vs base. Pure additions have no old lines."""
    out: dict[str, dict[str, list]] = defaultdict(lambda: {"old": [], "new": []})
    path = None
    for line in _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", base, "--", "*.py").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and (m := HUNK2.match(line)):
            oa, on, na, nn = int(m[1]), int(m[2] if m[2] is not None else 1), int(m[3]), int(m[4] if m[4] is not None else 1)
            if on:
                out[path]["old"].append((oa, oa + on - 1))
            if nn:
                out[path]["new"].append((na, na + nn - 1))
            out[path]  # make sure the file is listed even for pure deletions/additions
    return dict(out)


def _pytest_id(nid_: str) -> str:
    p, q = nid_.split("::", 1)
    return p + "::" + q.replace(".", "::")


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

    def record(nid_, dist, conf, chain, call_line, prov="static"):
        if nid_ in seeds:
            return
        cur = found.get(nid_)
        if cur is None or (dist, -conf) < (cur["distance"], -cur["confidence"]):
            n = node[nid_]
            found[nid_] = {"id": nid_, "kind": n["kind"], "path": n["path"], "start": n["start_line"],
                           "end": n["end_line"], "distance": dist, "confidence": round(conf, 3),
                           "chain": chain, "call_line": call_line, "is_test": bool(n["is_test"]),
                           "provenance": prov}

    for seed in seeds:
        if seed not in D and seed not in node:
            continue
        paths = nx.single_source_shortest_path(D, seed, cutoff=depth) if seed in D else {}
        for tgt, p in paths.items():
            if len(p) < 2:
                continue
            conf, trusted = 1.0, True
            for u, v in zip(p, p[1:]):
                conf *= D[u][v]["confidence"]
                trusted = trusted and D[u][v]["provenance"] in ("static", "typed", "declared")
            record(tgt, len(p) - 1, conf, p, D[p[-2]][p[-1]]["line"], "static" if trusted else "heuristic")
        if node[seed]["kind"] == "module":  # module-level change: importers are affected
            for r in conn.execute("SELECT src, line FROM edges WHERE dst=? AND kind='imports'", (seed,)):
                record(r["src"], 1, 1.0, [seed, r["src"]], r["line"])

    affected = set(seeds) | set(found)
    reqs: dict[str, dict] = {}
    for r in conn.execute("SELECT src, dst, provenance, confidence FROM edges WHERE kind='implements'"):
        if r["dst"] in affected:
            d = 0 if r["dst"] in seeds else found[r["dst"]]["distance"]
            e = reqs.setdefault(r["src"][4:], {"id": r["src"][4:], "distance": d, "via": [], "inferred": True,
                                               "confidence": 0.0})
            e["distance"] = min(e["distance"], d)
            e["via"].append(r["dst"])
            e["inferred"] = e["inferred"] and r["provenance"] == "inferred"  # any declared link makes it declared
            e["confidence"] = max(e["confidence"], r["confidence"])
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
        t["pytest_id"] = _pytest_id(t["id"])
        t["evidence"] = ["static"]
    known = covlink.tests_with_data(conn)
    by_id = {t["id"]: t for t in tests}
    for tid, syms in covlink.tests_covering(conn, set(seeds)).items():  # tests that actually EXECUTED the changed code
        if tid in by_id:
            by_id[tid]["evidence"].append("runtime-symbol")
            by_id[tid]["executed_symbols"] = syms
        elif tid in node:
            by_id[tid] = _runtime_entry(node[tid], "runtime-symbol", diag, executed_symbols=syms)
            tests.append(by_id[tid])
    for t in tests:
        if "runtime-symbol" not in t["evidence"] and t["id"] in known:
            t["line_check"] = "not-executed"  # coverage exists for this test and it never ran the changed code
    sort_tests(tests)
    changed = []
    for sid, lines in seeds.items():
        n = node[sid]
        changed.append({"id": sid, "kind": n["kind"], "path": n["path"], "start": n["start_line"],
                        "end": n["end_line"], "changed_lines": sorted(set(lines))[:50],
                        "diagnostics": diag.get(sid, 0)})
    return {"changed": changed, "impacted": [i for i in items if not i["is_test"]], "tests": tests,
            "requirements": sorted(reqs.values(), key=lambda x: (x["distance"], x["id"])),
            "runtime": {"available": bool(known), "tests_with_data": len(known)}}


def _runtime_entry(n, evidence, diag, executed_symbols=None, lines_hit=None) -> dict:
    return {"id": n["id"], "kind": n["kind"], "path": n["path"], "start": n["start_line"], "end": n["end_line"],
            "distance": 0, "confidence": 1.0, "chain": [n["id"]], "call_line": None, "is_test": True,
            "provenance": "runtime", "diagnostics": diag.get(n["id"], 0), "pytest_id": _pytest_id(n["id"]),
            "evidence": [evidence], **({"executed_symbols": executed_symbols} if executed_symbols else {}),
            **({"lines_hit": lines_hit} if lines_hit else {})}


def sort_tests(tests: list) -> None:
    """Tests that ran the exact changed lines first, then ones that ran the function, then statically reachable ones."""
    def rank(t):
        e = t["evidence"]
        if "runtime-line" in e:
            return 0
        if "runtime-symbol" in e:
            return 2 if t.get("line_check") == "miss" else 1
        return 4 if t.get("line_check") == "not-executed" else 3
    tests.sort(key=lambda t: (rank(t), t["distance"], t["id"]))


def _blob_sha(repo, base, path):
    r = subprocess.run(["git", "-C", str(repo), "show", f"{base}:{path}"], capture_output=True)
    return hashlib.sha1(r.stdout).hexdigest() if r.returncode == 0 else None


def refine_with_lines(conn, repo, base, res) -> None:
    """Upgrade runtime evidence to exact lines: which tests executed the very lines the diff touches.
    Coverage numbers belong to the file version it measured: matched against the working tree (new line numbers) or
    against `base` (old line numbers), whichever has the same content hash; otherwise it is left at symbol level."""
    diag = {}
    by_id = {t["id"]: t for t in res["tests"]}
    node = {r["id"]: r for r in conn.execute("SELECT * FROM nodes WHERE is_test=1")}
    for path, rng in diff_ranges(repo, base).items():
        rows = conn.execute("SELECT test, lines, file_hash FROM covlines WHERE path=?", (path,)).fetchall()
        if not rows:
            continue
        try:
            cur = covlink.sha(f"{repo}/{path}")
        except OSError:
            cur = None
        side = rng["new"] if rows[0]["file_hash"] == cur else rng["old"] if rows[0]["file_hash"] == _blob_sha(repo, base, path) else None
        if not side:
            continue
        for r in rows:
            hit = sorted(l for l in covlink.unpack(r["lines"]) if any(a <= l <= b for a, b in side))
            t = by_id.get(r["test"])
            if hit:
                if t is None and r["test"] in node:
                    t = by_id[r["test"]] = _runtime_entry(node[r["test"]], "runtime-line", diag, lines_hit=hit[:10])
                    res["tests"].append(t)
                elif t is not None:
                    if "runtime-line" not in t["evidence"]:
                        t["evidence"].append("runtime-line")
                    t["lines_hit"] = sorted(set(t.get("lines_hit", [])) | set(hit[:10]))[:10]
                    t["line_check"] = "hit"
            elif t is not None and "runtime-symbol" in t["evidence"] and t.get("line_check") != "hit":
                t["line_check"] = "miss"  # it ran the function, but none of the lines the diff changed
    sort_tests(res["tests"])


def impact_for_diff(conn, repo, base="HEAD", depth=6, min_conf=0.0):
    """git diff -> changed symbols -> impact, with test evidence refined to exact changed lines. None if no changes."""
    seeds = seeds_from_diff(conn, repo, base)
    if not seeds:
        return None
    res = impact(conn, seeds, depth, min_conf)
    refine_with_lines(conn, repo, base, res)
    return res
