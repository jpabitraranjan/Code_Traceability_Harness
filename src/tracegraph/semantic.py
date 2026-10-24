"""LLM-backed semantic layer: intent summaries, requirement->code link inference, requirement drafting.

Everything the model produces is stored apart from static facts (tables `semantics`, `inferences`) and tagged
`inferred` with a confidence, so agents and the UI can tell proof (static) from hypothesis (LLM).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import networkx as nx
import yaml

from . import requirements as rq
from .graph import CALL_KINDS, to_networkx
from .llm import LLMError, ask_json, log_usage

PROMPT_VERSION = "1"
CODE = "kind IN ('class','function','method')"

SYSTEM_SUMMARY = """You analyse source code for a code-intelligence tool used by coding agents.
Reply with ONE JSON object and nothing else:
{"summary": "what this code does, max 25 words",
 "intent": "why it exists: the business or user need it serves, max 25 words",
 "effects": ["side effects, e.g. database write, network call, file IO, raises ValidationError, mutates global state"]}
Be concrete and do not just repeat the name. If the purpose is unclear, say so in "intent"."""

SYSTEM_LINK = """You link software requirements to the code that implements or verifies them.
Reply with ONE JSON object:
{"implements": [{"id": "<candidate id>", "confidence": 0.0-1.0, "rationale": "max 20 words"}],
 "verified_by": [{"id": "<candidate test id>", "confidence": 0.0-1.0, "rationale": "max 20 words"}]}
Use ONLY ids from the candidate lists. Include a candidate only if it clearly contributes to the requirement.
Use confidence >= 0.8 only when the summary directly matches the requirement. Empty lists are fine."""

SYSTEM_DRAFT = """You are a business analyst reverse-engineering requirements from an existing codebase.
Reply with ONE JSON object:
{"requirements": [{"title": "...", "description": "1-2 sentences of user-visible behaviour",
 "design": ["key design decisions visible in the code"], "implemented_by": ["<symbol id>"],
 "status": "implemented|partial"}]}
Rules: 6-20 requirements, each a user-facing capability ("Teachers can ..."). implemented_by must use ONLY ids from
the lists provided (max 6 each; prefer entry points such as views, handlers, public functions). Do not invent features."""


def _sha(s: str) -> str:
    return hashlib.sha1((PROMPT_VERSION + s).encode()).hexdigest()


def read_source(repo, node, max_lines=None) -> str:
    try:
        lines = (Path(repo) / node["path"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    a, b = node["start_line"], node["end_line"]
    if max_lines and b - a + 1 > max_lines:
        return "\n".join(lines[a - 1:a - 1 + max_lines]) + f"\n# ... {b - a + 1 - max_lines} more lines"
    return "\n".join(lines[a - 1:b])


def semantics_for(conn, repo, node) -> dict | None:
    """Stored intent for a node, flagged stale if its source changed since it was summarised."""
    r = conn.execute("SELECT * FROM semantics WHERE node_id=?", (node["id"],)).fetchone()
    if not r:
        return None
    out = {"summary": r["summary"], "intent": r["intent"], "model": r["model"]}
    if r["effects"] and json.loads(r["effects"]):
        out["effects"] = json.loads(r["effects"])
    if _sha(read_source(repo, node)) != r["source_hash"]:
        out["stale"] = True
    return out


# ------------------------------------------------------------------ retrieval (no embeddings needed)
_STOP = set("a an the of to and or in for on with is are be by from that this it as at we can user users system".split())


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def tokens(text: str) -> list[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _STOP]


def rank(query: str, docs: dict[str, str], k: int = 10) -> list[tuple[str, float]]:
    q = Counter(tokens(query))
    if not q or not docs:
        return []
    toks = {i: Counter(tokens(t)) for i, t in docs.items()}
    n, df = len(docs), Counter(w for c in toks.values() for w in c)
    scores = {}
    for i, c in toks.items():
        s = sum(math.log(1 + n / df[w]) * (1 + math.log(c[w])) for w in q if w in c)
        if s:
            scores[i] = s / math.sqrt(sum(c.values()) + 1)
    return sorted(scores.items(), key=lambda x: -x[1])[:k]


def symbol_docs(conn, tests: bool = False) -> tuple[dict[str, str], dict[str, object]]:
    sem = {r["node_id"]: r for r in conn.execute("SELECT * FROM semantics")}
    docs, rows = {}, {}
    for r in conn.execute(f"SELECT * FROM nodes WHERE {CODE} AND is_test={1 if tests else 0}"):
        s = sem.get(r["id"])
        docs[r["id"]] = " ".join([r["name"], r["name"], r["path"].replace("/", " "),
                                  s["summary"] if s else "", s["intent"] if s else ""])
        rows[r["id"]] = r
    return docs, rows


def search_by_intent(conn, query: str, limit: int = 10) -> list[dict]:
    docs, rows = symbol_docs(conn)
    sem = {r["node_id"]: r for r in conn.execute("SELECT node_id, summary FROM semantics")}
    return [{"id": i, "kind": rows[i]["kind"], "lines": f"{rows[i]['start_line']}-{rows[i]['end_line']}",
             "score": round(s, 2), **({"summary": sem[i]["summary"]} if i in sem else {})}
            for i, s in rank(query, docs, limit)]


# ------------------------------------------------------------------ 1. summaries
def _degrees(conn):
    deg = Counter()
    for r in conn.execute("SELECT src, dst FROM edges WHERE kind='calls'"):
        deg[r["src"]] += 1
        deg[r["dst"]] += 1
    return deg


def summarize(repo, conn, client, limit=200, force=False, include_tests=False, workers=2, progress=None) -> dict:
    deg = _degrees(conn)
    linked = {r["dst"] for r in conn.execute("SELECT dst FROM edges WHERE kind IN ('implements','verified_by')")}
    done = {r["node_id"]: r["source_hash"] for r in conn.execute("SELECT node_id, source_hash FROM semantics")}
    rows = conn.execute(f"SELECT * FROM nodes WHERE {CODE}" + ("" if include_tests else " AND is_test=0")).fetchall()
    rows.sort(key=lambda r: (r["id"] not in linked, -deg[r["id"]], r["id"]))  # most connected / requirement-linked first
    callees, callers = defaultdict(list), defaultdict(list)
    for r in conn.execute("SELECT src, dst FROM edges WHERE kind='calls'"):
        callees[r["src"]].append(r["dst"])
        callers[r["dst"]].append(r["src"])
    todo, cached = [], 0
    for r in rows:
        full = read_source(repo, r)
        if not full:
            continue
        h = _sha(full)
        if not force and done.get(r["id"]) == h:
            cached += 1
        else:
            todo.append((r, h))
    batch = todo[:limit]
    stats = {"summarized": 0, "cached": cached, "failed": 0, "remaining": len(todo) - len(batch), "errors": []}

    def work(item):
        r, h = item
        user = (f"Symbol: {r['id']}\nKind: {r['kind']}\n"
                f"Calls: {', '.join(callees[r['id']][:8]) or '-'}\nCalled by: {', '.join(callers[r['id']][:6]) or '-'}\n\n"
                f"Source:\n{read_source(repo, r, max_lines=120)}")
        return item, ask_json(client, SYSTEM_SUMMARY, user)

    consecutive = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(work, it) for it in batch]
        for n, f in enumerate(as_completed(futs), 1):
            if f.cancelled():
                continue
            (r, h), (data, replies, err) = f.result()
            ok = bool(data and isinstance(data.get("summary"), str))
            for rep in replies:
                log_usage(conn, "summarize", client.label, rep, ok)
            if ok:
                consecutive = 0
                eff = [str(e)[:80] for e in (data.get("effects") or []) if e][:6] if isinstance(data.get("effects"), list) else []
                conn.execute("INSERT OR REPLACE INTO semantics VALUES(?,?,?,?,?,?,?)",
                             (r["id"], data["summary"][:300], str(data.get("intent", ""))[:300], json.dumps(eff),
                              h, client.label, time.time()))
                conn.commit()
                stats["summarized"] += 1
            else:
                stats["failed"] += 1
                consecutive += 1
                if len(stats["errors"]) < 3:
                    stats["errors"].append(f"{r['id']}: {err or 'bad reply'}")
                if consecutive >= 5 and not stats["summarized"]:  # provider is down or misconfigured: stop early
                    for g in futs:
                        g.cancel()
                    raise LLMError(err or "model returned unusable replies")
            if progress:
                progress(n, len(batch), r["id"])
    if batch and not stats["summarized"]:  # every call failed: surface the cause instead of reporting "0 done"
        raise LLMError(stats["errors"][0] if stats["errors"] else "model returned unusable replies")
    return stats


# ------------------------------------------------------------------ 2. requirement -> code links
def infer_links(repo, conn, client, min_confidence=0.6, progress=None) -> dict:
    reqs = conn.execute("SELECT * FROM requirements ORDER BY id").fetchall()
    if not reqs:
        raise LLMError("no requirements loaded: write requirements.yaml or run the draft task first")
    docs, rows = symbol_docs(conn)
    tdocs, trows = symbol_docs(conn, tests=True)
    sem = {r["node_id"]: r for r in conn.execute("SELECT * FROM semantics")}
    G = to_networkx(conn, kinds=CALL_KINDS)
    D = nx.DiGraph()
    D.add_nodes_from(G.nodes)
    D.add_edges_from((t, s) for s, t in G.edges())
    stats = {"requirements": len(reqs), "implements": 0, "verified_by": 0, "failed": 0, "dropped": 0}

    for n, r in enumerate(reqs, 1):
        data = json.loads(r["data"])
        rid = f"REQ:{r['id']}"
        declared = {x["dst"] for x in conn.execute(
            "SELECT dst FROM edges WHERE src=? AND provenance='declared'", (rid,))}
        query = " ".join([r["title"], r["description"] or "", *data.get("design", [])])
        cands = [i for i, _ in rank(query, {i: t for i, t in docs.items() if i not in declared}, 12)]
        reach = set()
        for c in cands[:6]:
            reach |= nx.descendants(D, c) & set(trows)
        tcands = [i for i, _ in rank(query, {i: tdocs[i] for i in reach} or tdocs, 8)]
        if progress:
            progress(n, len(reqs), r["id"])
        if not cands:
            continue
        line = lambda i, rr: (f"- {i} [{rr[i]['kind']}] {sem[i]['summary']} | intent: {sem[i]['intent']}" if i in sem
                              else f"- {i} [{rr[i]['kind']}] (no summary)")
        user = (f"Requirement {r['id']}: {r['title']}\n{r['description'] or ''}\nDesign notes: {'; '.join(data.get('design', [])) or '-'}\n"
                f"Already linked by a human: {', '.join(sorted(declared)) or '-'}\n\n"
                "Candidate code:\n" + "\n".join(line(i, rows) for i in cands) +
                "\n\nCandidate tests:\n" + ("\n".join(f"- {i}" for i in tcands) or "-"))
        out, replies, err = ask_json(client, SYSTEM_LINK, user)
        for rep in replies:
            log_usage(conn, "link", client.label, rep, out is not None)
        if out is None:
            stats["failed"] += 1
            continue
        conn.execute("DELETE FROM inferences WHERE src=?", (rid,))
        for key, allowed in (("implements", set(cands)), ("verified_by", set(tcands))):
            for item in out.get(key) or []:
                try:
                    i, conf = item["id"], max(0.0, min(1.0, float(item.get("confidence", 0))))
                except (KeyError, TypeError, ValueError):
                    stats["dropped"] += 1
                    continue
                if i not in allowed or conf < min_confidence:
                    stats["dropped"] += 1
                    continue
                conn.execute("INSERT OR REPLACE INTO inferences VALUES(?,?,?,?,?,?)",
                             (rid, i, key, conf, str(item.get("rationale", ""))[:200], client.label))
                stats[key] += 1
        conn.commit()
    rq.link(conn)  # materialise as `inferred` edges
    return stats


# ------------------------------------------------------------------ 3. draft requirements
def _docs_text(repo, limit=6000) -> str:
    repo, out = Path(repo), []
    for name in ("README.md", "README.rst", "README.txt", "readme.md"):
        if (repo / name).exists():
            out.append((repo / name).read_text(encoding="utf-8", errors="replace")[:limit])
            break
    for p in sorted((repo / "docs").glob("*.md"))[:2] if (repo / "docs").is_dir() else []:
        out.append(p.read_text(encoding="utf-8", errors="replace")[:2000])
    return "\n\n".join(out)


def draft_requirements(repo, conn, client, out_path=None, min_summaries=20, max_summarize=60,
                       max_chars=14000, progress=None) -> dict:
    have = conn.execute("SELECT count(*) FROM semantics").fetchone()[0]
    if have < min_summaries:  # draft quality depends on summaries: make the most connected ones first
        summarize(repo, conn, client, limit=max_summarize, progress=progress)
    sem = {r["node_id"]: r for r in conn.execute("SELECT * FROM semantics")}
    deg = _degrees(conn)
    by_file = defaultdict(list)
    for r in conn.execute(f"SELECT * FROM nodes WHERE {CODE} AND is_test=0"):
        by_file[r["path"]].append(r)
    valid, lines, size = set(), [], 0
    for path in sorted(by_file):
        top = sorted(by_file[path], key=lambda r: (r["id"] not in sem, -deg[r["id"]]))[:8]
        block = [f"{path}:"] + [f"  {r['id']} | {sem[r['id']]['summary'] if r['id'] in sem else r['name']}" for r in top]
        if size + sum(len(x) for x in block) > max_chars:
            break
        valid |= {r["id"] for r in top}
        lines += block
        size += sum(len(x) for x in block)
    routes = [f"  {r['name']} -> {r['dst']}" for r in conn.execute(
        "SELECT n.name, e.dst FROM nodes n JOIN edges e ON e.src=n.id AND e.kind='calls' WHERE n.kind='route' LIMIT 60")]
    valid |= {r.split(" -> ")[1] for r in routes}
    user = (f"Project documentation:\n{_docs_text(repo) or '(none)'}\n\n"
            + ("URL routes:\n" + "\n".join(routes) + "\n\n" if routes else "")
            + "Code (symbol id | summary):\n" + "\n".join(lines))
    out, replies, err = ask_json(client, SYSTEM_DRAFT, user)
    for rep in replies:
        log_usage(conn, "draft", client.label, rep, out is not None)
    if out is None or not isinstance(out.get("requirements"), list):
        raise LLMError(err or "model returned no requirements")
    reqs, dropped = [], 0
    for item in out["requirements"]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        ids = [i for i in (item.get("implemented_by") or []) if i in valid]
        dropped += len(item.get("implemented_by") or []) - len(ids)
        reqs.append({"id": f"REQ-{len(reqs) + 1:03d}", "title": str(item["title"])[:140],
                     "status": "implemented" if ids and item.get("status") != "partial" else ("partial" if ids else "planned"),
                     "description": str(item.get("description", ""))[:400],
                     "design": [str(d)[:200] for d in (item.get("design") or [])][:5],
                     "implemented_by": ids[:6]})
    path = Path(out_path) if out_path else Path(repo) / "requirements.draft.yaml"
    text = (f"# DRAFT generated by {client.label}. Review, edit, then save as requirements.yaml.\n"
            + yaml.safe_dump({"requirements": reqs}, sort_keys=False, allow_unicode=True, width=100))
    path.write_text(text, encoding="utf-8")
    return {"path": str(path), "count": len(reqs), "dropped_invalid_ids": dropped, "yaml": text}
