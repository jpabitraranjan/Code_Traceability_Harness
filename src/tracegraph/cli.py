"""tracegraph CLI: index | impact | show | requirements | export"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import impact as imp, requirements as rq
from .graph import connect, index_repo, to_networkx
from .llm import LLMError


def _db(a) -> Path:
    return Path(a.db) if a.db else Path(a.repo).resolve() / ".tracegraph" / "graph.db"


def _conn(a):
    db = _db(a)
    if not db.exists():  # never create an empty index silently: that made `resolve` look like it had succeeded
        sys.exit(f"error: no index at {db}\n  run first:  tracegraph index --repo {a.repo}\n"
                 "  (every command defaults to --repo . ; pass --repo for any other folder)")
    return connect(db)


def _loc(x) -> str:
    return f"{x['id']}  [{x['kind']}] L{x['start']}-{x['end']}"


def cmd_resolve(a):
    from . import typeres
    conn = _conn(a)
    last = {}

    def prog(n, total, what):
        print(f"\r  {n}/{total}  {str(what)[-60:]:<60}", end="", file=sys.stderr, flush=True)

    res = typeres.resolve(a.repo, conn, include_static=a.all, python=a.python, limit=a.limit, progress=prog)
    print(file=sys.stderr)
    if a.json:
        print(json.dumps(res, indent=2))
        return
    print("  ".join(f"{k}={v}" for k, v in res.items() if k not in ("disagreements", "message", "hint")))
    for d in res.get("disagreements", []):
        print("  disagreement:", d)
    for k in ("message", "hint"):
        if res.get(k):
            print(("hint: " if k == "hint" else "") + res[k])


def cmd_coverage(a):
    from . import covlink
    conn = _conn(a)
    if a.act == "run":
        cmd = [x for x in (a.cmd or [])]
        res = covlink.run(a.repo, conn, cmd[1:] if cmd[:1] == ["--"] else cmd, a.python)
    elif a.act == "import":
        res = covlink.import_data(conn, a.repo)
    else:
        res = covlink.status(conn)
    print(json.dumps(res, indent=2) if a.json else "  ".join(f"{k}={v}" for k, v in res.items()))


def cmd_index(a):
    req = a.requirements or next((str(Path(a.repo) / f) for f in ("requirements.yaml", "requirements.yml")
                                  if (Path(a.repo) / f).exists()), None)
    stats = index_repo(a.repo, _db(a), requirements=req,
                       quality=[t for t in ("ruff", "pyright") if getattr(a, t)] or None)
    if getattr(a, "resolve", False):
        from . import typeres
        r = typeres.resolve(a.repo, _conn(a), python=a.python)
        stats.update(typed_edges=r.get("typed_edges", 0), heuristic_edges_after=r["heuristic_edges_after"])
    print(json.dumps(stats) if a.json else "  ".join(f"{k}={v}" for k, v in stats.items()))


def cmd_impact(a):
    conn = _conn(a)
    if a.symbol:
        seeds = {}
        for s in a.symbol:
            t = imp.resolve_target(conn, s)
            if not t:
                sys.exit(f"cannot resolve symbol: {s}")
            seeds[t] = []
    else:
        seeds = None
    res = (imp.impact(conn, seeds, depth=a.depth, min_conf=a.min_confidence) if seeds is not None else
           imp.impact_for_diff(conn, a.repo, a.base, a.depth, a.min_confidence) or {"changed": []})
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
        tag = "+".join(e.replace("runtime-", "ran-") for e in t["evidence"] if e != "static") or "static"
        note = {"miss": "  (ran the function, not the changed lines)", "not-executed": "  (never executed it when last measured)"}.get(t.get("line_check"), "")
        print(f"  d={t['distance']} conf={t['confidence']:.2f}  [{tag}]  {t['pytest_id']}{note}")
    if res["requirements"]:
        print(f"\nRequirements affected ({len(res['requirements'])}):")
        for r in res["requirements"]:
            print(f"  {r['id']} [{r['status']}] {r['title']}  (distance {r['distance']})")


def cmd_show(a):
    conn = _conn(a)
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
    rows = rq.report(_conn(a))
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
        for p in r["unexecuted"][:5]:
            print(f"    ! never executed by any test run: {p}")
        if len(r["unexecuted"]) > 5:
            print(f"    ! ... and {len(r['unexecuted']) - 5} more never executed")


def cmd_find(a):
    conn = _conn(a)
    rows = conn.execute("""SELECT id, kind, start_line, end_line, is_test FROM nodes
        WHERE kind IN ('class','function','method','module','route') AND (name=? OR id LIKE ?)
        ORDER BY (name=?) DESC, length(id) LIMIT ?""", (a.query, f"%{a.query}%", a.query, a.limit)).fetchall()
    if a.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
    for r in ([] if a.json else rows):
        print(f"{r['id']}  [{r['kind']}] L{r['start_line']}-{r['end_line']}" + ("  (test)" if r["is_test"] else ""))
    if not rows and not a.json:
        print("no matches")


def cmd_search(a):
    from . import semantic
    rows = semantic.search_by_intent(_conn(a), a.query, a.limit)
    if a.json:
        print(json.dumps(rows, indent=2))
    for r in ([] if a.json else rows):
        print(f"{r['score']:>5}  {r['id']}  [{r['kind']}] L{r['lines']}" + (f"\n       {r['summary']}" if r.get("summary") else ""))
    if not rows and not a.json:
        print("no matches")


def cmd_llm(a):
    from . import llm, semantic
    cfg = llm.resolve_config(a.repo, a.llm, a.llm_url)
    conn = None if a.act == "config" else _conn(a)
    if a.act == "config":
        if a.llm or a.llm_url:
            llm.save_config(a.repo, cfg)
            print("saved to .tracegraph/llm.json")
        print(f"provider={cfg.provider or '-'}  model={cfg.model or '-'}  url={cfg.base_url or '(provider default)'}")
        return
    if a.act == "usage":
        u = llm.usage_summary(conn)
        print(json.dumps(u, indent=2) if a.json else "\n".join(
            [f"{r['task']:<10} {r['model']:<28} calls={r['calls']:<4} tokens={r['prompt_tokens']}+{r['completion_tokens']} "
             f"time={r['seconds']}s failed={r['failed']}" for r in u["by_task"]]
            + [f"TOTAL      calls={u['calls']} tokens={u['prompt_tokens']}+{u['completion_tokens']} time={u['seconds']}s"]))
        return
    if a.act == "models":
        cfg.model = cfg.model or "-"
    client = llm.make_client(cfg)

    def prog(n, total, what):
        print(f"\r  {n}/{total}  {str(what)[-60:]:<60}", end="", file=sys.stderr, flush=True)

    if a.act == "models":
        print("\n".join(client.models()))
        return
    if a.act == "test":
        res = llm.ping(client)
    elif a.act == "summarize":
        res = semantic.summarize(a.repo, conn, client, limit=a.limit, force=a.force,
                                 include_tests=a.include_tests, workers=a.workers, progress=prog)
    elif a.act == "link":
        res = semantic.infer_links(a.repo, conn, client, a.min_confidence, progress=prog)
    else:
        res = semantic.draft_requirements(a.repo, conn, client, out_path=a.out, progress=prog)
    print(file=sys.stderr)
    if a.act == "draft":
        print(f"wrote {res['path']}  ({res['count']} requirements, {res['dropped_invalid_ids']} invalid ids dropped)\n"
              "Review it, save as requirements.yaml, then: tracegraph index")
    else:
        print(json.dumps(res) if a.json else "  ".join(f"{k}={v}" for k, v in res.items()))


def cmd_ui(a):
    from .server import serve
    serve(a.repo, a.db, a.requirements, port=a.port, open_browser=not a.no_browser)


def cmd_export(a):
    G = to_networkx(_conn(a))
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
    p.add_argument("--resolve", action="store_true", help="then resolve uncertain calls with Pyright (type-aware)")
    p.add_argument("--python", help="interpreter whose installed packages Pyright should see")
    p = add("resolve", cmd_resolve, "type-aware resolution of heuristic/unresolved calls via Pyright")
    p.add_argument("--all", action="store_true", help="also cross-check statically resolved calls (reports agreement)")
    p.add_argument("--python", help="interpreter whose installed packages Pyright should see (default: Pyright's own)")
    p.add_argument("--limit", type=int, help="ask about at most N call sites")
    p = add("impact", cmd_impact, "what is affected by the current git diff (or given symbols)")
    p.add_argument("--base", default="HEAD"); p.add_argument("--symbol", action="append")
    p.add_argument("--depth", type=int, default=6); p.add_argument("--min-confidence", type=float, default=0.0)
    p = add("show", cmd_show, "neighbourhood of a symbol: id, name, or path:line")
    p.add_argument("target")
    add("requirements", cmd_requirements, "requirement status and coverage")
    p = add("export", cmd_export, "dump the graph as JSON"); p.add_argument("--out")
    p = add("find", cmd_find, "search symbols by name or id fragment")
    p.add_argument("query"); p.add_argument("--limit", type=int, default=20)
    p = add("search", cmd_search, "find code by what it does (best after `llm summarize`)")
    p.add_argument("query"); p.add_argument("--limit", type=int, default=10)
    cp = sub.add_parser("coverage", help="runtime evidence: run | import | status")
    csub = cp.add_subparsers(dest="act", required=True)
    for act, h in (("run", "run your tests under coverage: records which test executed which code (default: pytest)"),
                   ("import", "re-import coverage data from the last run"), ("status", "what runtime data is loaded")):
        q = csub.add_parser(act, help=h)
        q.add_argument("--repo", default="."); q.add_argument("--db"); q.add_argument("--json", action="store_true")
        q.set_defaults(fn=cmd_coverage)
        if act == "run":
            q.add_argument("--python", help="interpreter to run the tests with (needs coverage installed)")
            q.add_argument("cmd", nargs=argparse.REMAINDER, help="test command after --, e.g. -- python manage.py test")
    lp = sub.add_parser("llm", help="LLM layer: config | models | test | summarize | link | draft | usage")
    lsub = lp.add_subparsers(dest="act", required=True)
    for act, h in (("config", "show or set provider:model"), ("models", "list models the provider offers"),
                   ("test", "ping the model"), ("summarize", "intent summaries for symbols (cached by source hash)"),
                   ("link", "infer requirement -> code/test links"), ("draft", "draft a requirements file from the code"),
                   ("usage", "tokens and time spent on LLM calls")):
        q = lsub.add_parser(act, help=h)
        q.add_argument("--repo", default="."); q.add_argument("--db"); q.add_argument("--json", action="store_true")
        q.add_argument("--llm", help="provider:model, e.g. ollama:qwen2.5-coder:7b"); q.add_argument("--llm-url")
        q.set_defaults(fn=cmd_llm)
        if act == "summarize":
            q.add_argument("--limit", type=int, default=200); q.add_argument("--force", action="store_true")
            q.add_argument("--include-tests", action="store_true"); q.add_argument("--workers", type=int, default=2)
        if act == "link":
            q.add_argument("--min-confidence", type=float, default=0.6)
        if act == "draft":
            q.add_argument("--out", help="default: <repo>/requirements.draft.yaml")
    p = add("ui", cmd_ui, "start the local web UI (views: requirements, traceability, change impact)")
    p.add_argument("--port", type=int, default=8765); p.add_argument("--no-browser", action="store_true")
    p.add_argument("--requirements", help="requirements.yaml (auto-detected in the repo root)")
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except LLMError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
