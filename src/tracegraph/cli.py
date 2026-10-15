"""tracegraph CLI: index | impact | show | requirements | export"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import impact as imp, requirements as rq
from .graph import connect, index_repo, to_networkx


def _db(a) -> Path:
    return Path(a.db) if a.db else Path(a.repo).resolve() / ".tracegraph" / "graph.db"


def _loc(x) -> str:
    return f"{x['id']}  [{x['kind']}] L{x['start']}-{x['end']}"


def cmd_index(a):
    req = a.requirements or next((str(Path(a.repo) / f) for f in ("requirements.yaml", "requirements.yml")
                                  if (Path(a.repo) / f).exists()), None)
    stats = index_repo(a.repo, _db(a), requirements=req,
                       quality=[t for t in ("ruff", "pyright") if getattr(a, t)] or None)
    print(json.dumps(stats) if a.json else "  ".join(f"{k}={v}" for k, v in stats.items()))


def cmd_impact(a):
    conn = connect(_db(a))
    if a.symbol:
        seeds = {}
        for s in a.symbol:
            t = imp.resolve_target(conn, s)
            if not t:
                sys.exit(f"cannot resolve symbol: {s}")
            seeds[t] = []
    else:
        seeds = imp.seeds_from_diff(conn, a.repo, a.base)
    res = imp.impact(conn, seeds, depth=a.depth, min_conf=a.min_confidence)
    if a.json:
        print(json.dumps(res, indent=2))
        return
    if not res["changed"]:
        print("No changed symbols found.")
        return
    print(f"Changed symbols ({len(res['changed'])}):")
    for c in res["changed"]:
        d = f"  ruff/pyright findings: {c['diagnostics']}" if c["diagnostics"] else ""
        lines = f"  lines {c['changed_lines'][0]}..{c['changed_lines'][-1]}" if c["changed_lines"] else ""
        print(f"  {_loc(c)}{lines}{d}")
    print(f"\nPotentially affected ({len(res['impacted'])}):")
    for i in res["impacted"]:
        flag = "" if i["provenance"] == "static" else "  ~heuristic"
        print(f"  d={i['distance']} conf={i['confidence']:.2f}  {_loc(i)}  (calls at L{i['call_line']}){flag}")
    print(f"\nTests to run ({len(res['tests'])}):")
    for t in res["tests"]:
        print(f"  d={t['distance']} conf={t['confidence']:.2f}  {t['pytest_id']}")
    if res["requirements"]:
        print(f"\nRequirements affected ({len(res['requirements'])}):")
        for r in res["requirements"]:
            print(f"  {r['id']} [{r['status']}] {r['title']}  (distance {r['distance']})")


def cmd_show(a):
    conn = connect(_db(a))
    t = imp.resolve_target(conn, a.target)
    if not t:
        sys.exit(f"cannot resolve (or ambiguous): {a.target}")
    n = conn.execute("SELECT * FROM nodes WHERE id=?", (t,)).fetchone()
    print(f"{n['id']}  [{n['kind']}] L{n['start_line']}-{n['end_line']}" + ("  (test)" if n["is_test"] else ""))
    q = lambda sql, *p: conn.execute(sql, p).fetchall()
    for title, rows, key in (
        ("calls", q("SELECT dst x, line, confidence c, provenance p FROM edges WHERE src=? AND kind='calls' ORDER BY line", t), "x"),
        ("called by", q("SELECT src x, line, confidence c, provenance p FROM edges WHERE dst=? AND kind='calls' ORDER BY src", t), "x"),
        ("inherits", q("SELECT dst x, line, confidence c, provenance p FROM edges WHERE src=? AND kind='inherits'", t), "x"),
        ("subclasses", q("SELECT src x, line, confidence c, provenance p FROM edges WHERE dst=? AND kind='inherits'", t), "x"),
        ("requirements", q("SELECT src x, line, confidence c, provenance p FROM edges WHERE dst=? AND kind IN ('implements','verified_by')", t), "x"),
    ):
        if rows:
            print(f"\n{title}:")
            for r in rows:
                at = f" @L{r['line']}" if r["line"] else ""
                print(f"  {r['x']}{at}  ({r['p']}, {r['c']:.2f})")
    for d in q("SELECT * FROM diagnostics WHERE node_id=?", t):
        print(f"\n{d['tool']} {d['code']} L{d['line']}: {d['message']}")


def cmd_requirements(a):
    rows = rq.report(connect(_db(a)))
    if not rows and not a.json:
        print("No requirements loaded. Create requirements.yaml (see README) and run:\n"
              "  tracegraph index --repo <repo> --requirements requirements.yaml")
        return
    if a.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        print(f"{r['id']:<10} {r['status']:<12} impl={len(r['implementations']):<3} "
              f"tests={len(r['tests_reaching']):<3} {r['title']}")
        for p in r["unmatched_patterns"]:
            print(f"    ! no code matches: {p}")


def cmd_find(a):
    conn = connect(_db(a))
    rows = conn.execute("""SELECT id, kind, start_line, end_line, is_test FROM nodes
        WHERE kind IN ('class','function','method','module','route') AND (name=? OR id LIKE ?)
        ORDER BY (name=?) DESC, length(id) LIMIT ?""", (a.query, f"%{a.query}%", a.query, a.limit)).fetchall()
    if a.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
    for r in ([] if a.json else rows):
        print(f"{r['id']}  [{r['kind']}] L{r['start_line']}-{r['end_line']}" + ("  (test)" if r["is_test"] else ""))
    if not rows and not a.json:
        print("no matches")


def cmd_ui(a):
    from .server import serve
    serve(a.repo, a.db, a.requirements, port=a.port, open_browser=not a.no_browser)


def cmd_export(a):
    G = to_networkx(connect(_db(a)))
    data = {"nodes": [{"id": n, **d} for n, d in G.nodes(data=True)],
            "links": [{"source": s, "target": t, **d} for s, t, d in G.edges(data=True)]}
    (open(a.out, "w") if a.out else sys.stdout).write(json.dumps(data, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracegraph", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--repo", default=".")
        p.add_argument("--db", help="default: <repo>/.tracegraph/graph.db")
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
        return p

    p = add("index", cmd_index, "(re)build the graph; unchanged files are not re-parsed")
    p.add_argument("--requirements", help="requirements.yaml to load and link")
    p.add_argument("--ruff", action="store_true"); p.add_argument("--pyright", action="store_true")
    p = add("impact", cmd_impact, "what is affected by the current git diff (or given symbols)")
    p.add_argument("--base", default="HEAD"); p.add_argument("--symbol", action="append")
    p.add_argument("--depth", type=int, default=6); p.add_argument("--min-confidence", type=float, default=0.0)
    p = add("show", cmd_show, "neighbourhood of a symbol: id, name, or path:line")
    p.add_argument("target")
    add("requirements", cmd_requirements, "requirement status and coverage")
    p = add("export", cmd_export, "dump the graph as JSON"); p.add_argument("--out")
    p = add("find", cmd_find, "search symbols by name or id fragment")
    p.add_argument("query"); p.add_argument("--limit", type=int, default=20)
    p = add("ui", cmd_ui, "start the local web UI (views: requirements, traceability, change impact)")
    p.add_argument("--port", type=int, default=8765); p.add_argument("--no-browser", action="store_true")
    p.add_argument("--requirements", help="requirements.yaml (auto-detected in the repo root)")
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
