import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from tracegraph import llm, semantic as sem, requirements as rq, impact as imp
from tracegraph.graph import connect, index_repo
from tracegraph.llm import FakeLLM, LLMConfig, LLMError, make_client

SHOP = {
    "shop/rates.py": "RATES = {'IN': 0.18}\n\ndef rate_for(country):\n    '''Look up the tax rate for a country.'''\n    return RATES.get(country, 0.0)\n",
    "shop/billing.py": "from .rates import rate_for\n\ndef calculate_tax(amount, country):\n    return amount * rate_for(country)\n\ndef invoice_total(items, country):\n    return sum(items) + calculate_tax(sum(items), country)\n",
    "tests/test_shop.py": "from shop.billing import calculate_tax\n\ndef test_tax():\n    assert calculate_tax(100, 'IN') == 18\n",
    "requirements.yaml": "requirements:\n  - id: REQ-1\n    title: Customers are charged tax based on their country\n    description: Invoices include sales tax computed from the customer's country rate.\n    status: planned\n",
}


@pytest.fixture()
def repo(tmp_path):
    for p, c in SHOP.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
    index_repo(tmp_path, tmp_path / ".tracegraph" / "graph.db", requirements=tmp_path / "requirements.yaml")
    return tmp_path


def conn_of(repo):
    return connect(repo / ".tracegraph" / "graph.db")


class Responder:
    def __init__(self):
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        if system == sem.SYSTEM_SUMMARY:
            sym = user.split("\n")[0].removeprefix("Symbol: ")
            return json.dumps({"summary": f"does {sym.split('::')[-1]} things", "intent": "serves billing", "effects": ["none"]})
        if system == sem.SYSTEM_LINK:
            return "```json\n" + json.dumps({"implements": [
                {"id": "shop/billing.py::calculate_tax", "confidence": 0.9, "rationale": "computes tax"},
                {"id": "shop/billing.py::invoice_total", "confidence": 0.2, "rationale": "weak"},
                {"id": "made/up.py::nothing", "confidence": 0.99, "rationale": "hallucinated"}],
                "verified_by": [{"id": "tests/test_shop.py::test_tax", "confidence": 0.8, "rationale": "asserts tax"}]}) + "\n```"
        if system == sem.SYSTEM_DRAFT:
            return json.dumps({"requirements": [
                {"title": "Tax is calculated", "description": "d", "design": ["rates table"],
                 "implemented_by": ["shop/billing.py::calculate_tax", "ghost.py::x"], "status": "implemented"},
                {"title": "Nothing built yet", "implemented_by": []}]})
        return "{}"


# ------------------------------------------------------------- parsing & retrieval
def test_parse_json_handles_fences_and_prose():
    assert llm.parse_json('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        llm.parse_json("no json here")


def test_ask_json_retries_once_on_invalid_json():
    seq = iter(["oops", '{"ok": 1}'])
    data, replies, err = llm.ask_json(FakeLLM(lambda s, u: next(seq)), "s", "u")
    assert data == {"ok": 1} and len(replies) == 2 and err is None


def test_rank_matches_camel_case_and_stems():
    docs = {"a": "calculateTax rates", "b": "send email", "c": "tax"}
    assert sem.rank("calculating taxes", docs, 2)[0][0] in ("a", "c")
    assert "b" not in dict(sem.rank("calculating taxes", docs))


def test_spec_parsing_keeps_colons_in_model_names():
    assert llm.parse_spec("ollama:qwen2.5-coder:7b") == ("ollama", "qwen2.5-coder:7b")


# ------------------------------------------------------------- provider clients against stub servers
class Stub(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json(200, {"models": [{"name": "m2"}, {"name": "m1"}]})
        self._json(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.path == "/api/chat":
            return self._json(200, {"message": {"content": '{"ok": true}'}, "prompt_eval_count": 7, "eval_count": 3})
        if self.path == "/chat/completions":
            if "response_format" in body:  # emulate a server that rejects JSON mode
                return self._json(400, {"error": "unsupported response_format"})
            return self._json(200, {"choices": [{"message": {"content": '{"ok": 1}'}}], "usage": {"prompt_tokens": 11, "completion_tokens": 4}})
        if self.path == "/v1/messages":
            return self._json(200, {"content": [{"type": "text", "text": '{"ok": 2}'}], "usage": {"input_tokens": 5, "output_tokens": 2}})
        self._json(404, {})


@pytest.fixture()
def stub():
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Stub.seen.clear()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_ollama_client(stub):
    c = make_client(LLMConfig("ollama", "m1", stub))
    r = c.complete("sys", "usr")
    assert r.text == '{"ok": true}' and (r.prompt_tokens, r.completion_tokens) == (7, 3)
    body = Stub.seen[-1][2]
    assert body["format"] == "json" and body["stream"] is False and body["model"] == "m1"
    assert c.models() == ["m1", "m2"]


def test_openai_compat_falls_back_without_json_mode(stub, monkeypatch):
    monkeypatch.setenv("TRACEGRAPH_LLM_API_KEY", "k")
    r = make_client(LLMConfig("openai", "gpt-x", stub)).complete("sys", "usr")
    assert r.text == '{"ok": 1}' and r.prompt_tokens == 11
    assert [("response_format" in b) for _, _, b in Stub.seen] == [True, False]
    assert Stub.seen[-1][1]["authorization"] == "Bearer k"


def test_anthropic_client_sends_key_and_reads_usage(stub, monkeypatch):
    monkeypatch.setenv("TRACEGRAPH_LLM_API_KEY", "secret")
    r = make_client(LLMConfig("anthropic", "some-model", stub)).complete("sys", "usr")
    assert r.text == '{"ok": 2}' and r.completion_tokens == 2
    path, headers, body = Stub.seen[-1]
    assert headers["x-api-key"] == "secret" and body["system"] == "sys" and body["model"] == "some-model"


def test_errors_are_readable(monkeypatch):
    monkeypatch.delenv("TRACEGRAPH_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        make_client(LLMConfig("anthropic", "m")).complete("s", "u")
    with pytest.raises(LLMError, match="cannot reach"):
        make_client(LLMConfig("ollama", "m", "http://127.0.0.1:9")).complete("s", "u")
    with pytest.raises(LLMError, match="no model selected"):
        make_client(LLMConfig())


def test_config_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACEGRAPH_LLM", raising=False)
    llm.save_config(tmp_path, LLMConfig("ollama", "saved"))
    assert llm.resolve_config(tmp_path).model == "saved"
    monkeypatch.setenv("TRACEGRAPH_LLM", "openai:fromenv")
    assert llm.resolve_config(tmp_path).label == "openai:fromenv"
    assert llm.resolve_config(tmp_path, "anthropic:flag").label == "anthropic:flag"


# ------------------------------------------------------------- semantic tasks
def test_summarize_caches_by_source_hash_and_detects_stale(repo):
    c, r = conn_of(repo), Responder()
    client = FakeLLM(r)
    s1 = sem.summarize(repo, c, client, workers=1)
    assert s1["summarized"] == 3 and s1["failed"] == 0 and r.calls == 3  # rate_for, calculate_tax, invoice_total
    calls_after_first = r.calls
    s2 = sem.summarize(repo, c, client, workers=1)
    assert s2["summarized"] == 0 and s2["cached"] == 3 and r.calls == calls_after_first
    (repo / "shop/rates.py").write_text(SHOP["shop/rates.py"].replace("0.0", "0.5"))
    index_repo(repo, repo / ".tracegraph" / "graph.db")
    node = c.execute("SELECT * FROM nodes WHERE id='shop/rates.py::rate_for'").fetchone()
    assert sem.semantics_for(c, repo, node)["stale"] is True
    assert sem.summarize(repo, c, client, workers=1)["summarized"] == 1  # only the changed symbol
    assert c.execute("SELECT count(*) FROM llm_usage").fetchone()[0] == r.calls


def test_summarize_aborts_fast_when_provider_is_down(repo):
    def boom(s, u):
        raise LLMError("cannot reach http://x")
    with pytest.raises(LLMError):
        sem.summarize(repo, conn_of(repo), FakeLLM(boom), workers=1)


def test_infer_links_validates_filters_and_survives_reindex(repo):
    c = conn_of(repo)
    client = FakeLLM(Responder())
    sem.summarize(repo, c, client, workers=1)
    stats = sem.infer_links(repo, c, client, min_confidence=0.6)
    assert stats["implements"] == 1 and stats["verified_by"] == 1 and stats["dropped"] == 2  # weak + hallucinated
    q = "SELECT dst, provenance, confidence FROM edges WHERE src='REQ:REQ-1' AND kind='implements'"
    assert [tuple(r) for r in c.execute(q)] == [("shop/billing.py::calculate_tax", "inferred", 0.9)]
    index_repo(repo, repo / ".tracegraph" / "graph.db", requirements=repo / "requirements.yaml")  # re-index keeps them
    assert len(c.execute(q).fetchall()) == 1
    res = imp.impact(c, {"shop/rates.py::rate_for": []})
    req = res["requirements"][0]
    assert req["id"] == "REQ-1" and req["inferred"] is True and req["confidence"] == 0.9
    assert rq.report(c)[0]["inferred"] == 1


def test_draft_requirements_writes_loadable_yaml_and_drops_invalid_ids(repo):
    c = conn_of(repo)
    out = sem.draft_requirements(repo, c, FakeLLM(Responder()), min_summaries=0)
    assert out["count"] == 2 and out["dropped_invalid_ids"] == 1
    data = yaml.safe_load(open(out["path"]))["requirements"]
    assert data[0]["implemented_by"] == ["shop/billing.py::calculate_tax"] and data[1]["status"] == "planned"
    assert rq.load(c, out["path"]) == 2  # the draft is a valid requirements file


def test_search_by_intent_uses_summaries(repo):
    c = conn_of(repo)
    sem.summarize(repo, c, FakeLLM(lambda s, u: json.dumps({"summary": "applies sales tax to an invoice", "intent": "billing"})), workers=1)
    assert sem.search_by_intent(c, "sales tax on invoices", 3)[0]["summary"].startswith("applies sales tax")


# ------------------------------------------------------------- integration: agent API, MCP, server job, CLI
def test_agent_api_surfaces_intent_and_inferred_links(repo):
    from tracegraph.agent_api import Workspace
    c = conn_of(repo)
    client = FakeLLM(Responder())
    sem.summarize(repo, c, client, workers=1)
    sem.infer_links(repo, c, client)
    ws = Workspace(repo, requirements=repo / "requirements.yaml")
    ctx = ws.get_context("shop/rates.py::rate_for")
    assert ctx["intent"]["summary"] == "does rate_for things"
    assert ctx["requirements"][0]["inferred_link"] is True
    res = ws.impact_of_symbols(["shop/rates.py::rate_for"])
    assert res["affected"][0]["summary"].startswith("does ") and res["requirements"][0]["inferred_link"] is True
    assert ws.search_by_intent("calculate tax")["matches"][0]["id"] == "shop/billing.py::calculate_tax"
    (repo / "shop/rates.py").write_text(SHOP["shop/rates.py"].replace("0.0", "0.9"))
    assert ws.get_context("shop/rates.py::rate_for")["intent"]["stale"] is True


def test_mcp_exposes_search_by_intent(repo):
    import asyncio
    from tracegraph.agent_api import Workspace
    from tracegraph.mcp_server import build
    srv = build(Workspace(repo))
    lister = getattr(srv, "list_tools")
    tools = asyncio.run(lister())
    assert "search_by_intent" in {t.name for t in tools}


def test_server_llm_job_flow(repo, monkeypatch):
    import time
    from tracegraph.server import App
    app = App(repo)
    assert app.llm_config()["ready"] is False
    with pytest.raises(LLMError):
        app.llm_run(task="summarize")  # no model chosen yet
    monkeypatch.delenv("TRACEGRAPH_LLM", raising=False)
    app.llm_set(provider="ollama", model="m1")
    monkeypatch.setattr(llm, "make_client", lambda cfg: FakeLLM(Responder(), cfg.label))
    assert app.llm_run(task="summarize", limit=10) == {"started": "summarize"}
    for _ in range(100):
        job = app.llm_job()
        if job["state"] != "running":
            break
        time.sleep(0.05)
    assert job["state"] == "done" and job["result"]["summarized"] == 3
    assert app.llm_run(task="link")["started"] == "link"
    for _ in range(100):
        if app.llm_job()["state"] != "running":
            break
        time.sleep(0.05)
    assert app.llm_job()["result"]["implements"] == 1
    tr = app.trace(id="REQ-1")
    assert tr["implementations"][0]["inferred"] is True and tr["implementations"][0]["summary"]
    assert app.llm_config()["usage"]["calls"] >= 4
    assert app.status()["summaries"] == 3


def test_cli_search_and_config(repo, capsys, monkeypatch):
    from tracegraph.cli import main
    monkeypatch.delenv("TRACEGRAPH_LLM", raising=False)
    main(["llm", "config", "--repo", str(repo), "--llm", "ollama:qwen2.5-coder:7b"])
    assert "model=qwen2.5-coder:7b" in capsys.readouterr().out
    main(["search", "tax rate", "--repo", str(repo)])
    assert "shop/rates.py::rate_for" in capsys.readouterr().out
