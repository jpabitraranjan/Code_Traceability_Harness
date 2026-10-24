"""Pluggable LLM clients, stdlib only. Providers: ollama | openai (any OpenAI-compatible server) | anthropic.

Select with  --llm provider:model  (e.g. ollama:qwen2.5-coder:7b, openai:gpt-4o-mini, anthropic:<model>),
env TRACEGRAPH_LLM / TRACEGRAPH_LLM_URL, or the saved config in <repo>/.tracegraph/llm.json.
API keys are read from the environment only (OPENAI_API_KEY, ANTHROPIC_API_KEY, or TRACEGRAPH_LLM_API_KEY).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

PROVIDERS = ("ollama", "openai", "anthropic")
DEFAULT_URLS = {"ollama": "http://localhost:11434", "openai": "https://api.openai.com/v1",
                "anthropic": "https://api.anthropic.com"}
KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
TIMEOUT = float(os.environ.get("TRACEGRAPH_LLM_TIMEOUT", "180"))


class LLMError(RuntimeError):
    pass


class HTTPFail(LLMError):
    def __init__(self, code, detail):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code


@dataclass
class Reply:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


@dataclass
class LLMConfig:
    provider: str = ""
    model: str = ""
    base_url: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.provider and self.model)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


# ------------------------------------------------------------------ config
def parse_spec(spec: str) -> tuple[str, str]:
    provider, _, model = spec.partition(":")  # model names may contain ':' (ollama tags)
    return provider.strip().lower(), model.strip()


def _config_path(repo) -> Path:
    return Path(repo) / ".tracegraph" / "llm.json"


def save_config(repo, cfg: LLMConfig) -> None:
    if cfg.provider and cfg.provider not in PROVIDERS:
        raise LLMError(f"unknown provider '{cfg.provider}' (choose: {', '.join(PROVIDERS)})")
    p = _config_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=1))


def resolve_config(repo, spec: str | None = None, url: str | None = None) -> LLMConfig:
    """Precedence: explicit spec/url > environment > saved config."""
    cfg = LLMConfig()
    try:
        cfg = LLMConfig(**json.loads(_config_path(repo).read_text()))
    except (OSError, ValueError, TypeError):
        pass
    if os.environ.get("TRACEGRAPH_LLM"):
        cfg.provider, cfg.model = parse_spec(os.environ["TRACEGRAPH_LLM"])
    if os.environ.get("TRACEGRAPH_LLM_URL"):
        cfg.base_url = os.environ["TRACEGRAPH_LLM_URL"]
    if spec:
        cfg.provider, cfg.model = parse_spec(spec)
    if url:
        cfg.base_url = url
    return cfg


# ------------------------------------------------------------------ http
def _http(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise HTTPFail(e.code, e.read().decode("utf-8", "replace")[:300])
    except urllib.error.URLError as e:
        raise LLMError(f"cannot reach {url}: {e.reason}")
    except TimeoutError:
        raise LLMError(f"timed out after {TIMEOUT:.0f}s waiting for {url}")


def _key(provider):
    return os.environ.get("TRACEGRAPH_LLM_API_KEY") or os.environ.get(KEY_ENV.get(provider, ""), "")


# ------------------------------------------------------------------ clients
class Client:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.base = (cfg.base_url or DEFAULT_URLS[cfg.provider]).rstrip("/")
        self.label = cfg.label

    def complete(self, system: str, user: str, json_mode: bool = True) -> Reply:
        raise NotImplementedError

    def models(self) -> list[str]:
        raise NotImplementedError


class Ollama(Client):
    def complete(self, system, user, json_mode=True):
        body = {"model": self.cfg.model, "stream": False, "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["format"] = "json"
        t = time.time()
        try:
            d = _http(f"{self.base}/api/chat", body)
        except HTTPFail as e:
            hint = f" (try: ollama pull {self.cfg.model})" if e.code == 404 else ""
            raise LLMError(f"{e}{hint}")
        return Reply(d.get("message", {}).get("content", ""), d.get("prompt_eval_count", 0),
                     d.get("eval_count", 0), time.time() - t)

    def models(self):
        return sorted(m["name"] for m in _http(f"{self.base}/api/tags").get("models", []))


class OpenAICompat(Client):
    def _headers(self):
        k = _key("openai")
        return {"Authorization": f"Bearer {k}"} if k else {}

    def complete(self, system, user, json_mode=True):
        if "api.openai.com" in self.base and not _key("openai"):
            raise LLMError("set OPENAI_API_KEY (or TRACEGRAPH_LLM_API_KEY) in the environment")
        base = {"model": self.cfg.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        variants = [{**base, "temperature": 0, **({"response_format": {"type": "json_object"}} if json_mode else {})}, base]
        t = time.time()
        for i, body in enumerate(variants):  # some servers/models reject optional params: retry minimal body once
            try:
                d = _http(f"{self.base}/chat/completions", body, self._headers())
                break
            except HTTPFail as e:
                if e.code == 400 and i + 1 < len(variants):
                    continue
                raise
        u = d.get("usage") or {}
        return Reply(d["choices"][0]["message"]["content"] or "", u.get("prompt_tokens", 0),
                     u.get("completion_tokens", 0), time.time() - t)

    def models(self):
        return sorted(m["id"] for m in _http(f"{self.base}/models", headers=self._headers()).get("data", []))


class Anthropic(Client):
    def _headers(self):
        if not _key("anthropic"):
            raise LLMError("set ANTHROPIC_API_KEY (or TRACEGRAPH_LLM_API_KEY) in the environment")
        return {"x-api-key": _key("anthropic"), "anthropic-version": "2023-06-01"}

    def complete(self, system, user, json_mode=True):
        body = {"model": self.cfg.model, "max_tokens": 2048, "system": system,
                "messages": [{"role": "user", "content": user}]}
        t = time.time()
        d = _http(f"{self.base}/v1/messages", body, self._headers())
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage") or {}
        return Reply(text, u.get("input_tokens", 0), u.get("output_tokens", 0), time.time() - t)

    def models(self):
        return sorted(m["id"] for m in _http(f"{self.base}/v1/models", headers=self._headers()).get("data", []))


class FakeLLM:
    """Deterministic stand-in for tests/offline demos: fn(system, user) -> text."""
    def __init__(self, fn, label="fake:test"):
        self.fn, self.label = fn, label

    def complete(self, system, user, json_mode=True):
        return Reply(self.fn(system, user), 10, 5, 0.0)

    def models(self):
        return ["test"]


def make_client(cfg: LLMConfig):
    if not cfg.ready:
        raise LLMError("no model selected: use --llm provider:model, e.g. ollama:qwen2.5-coder:7b")
    if cfg.provider not in PROVIDERS:
        raise LLMError(f"unknown provider '{cfg.provider}' (choose: {', '.join(PROVIDERS)})")
    return {"ollama": Ollama, "openai": OpenAICompat, "anthropic": Anthropic}[cfg.provider](cfg)


# ------------------------------------------------------------------ json + usage
def parse_json(text: str):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("no JSON object in reply")
    return json.loads(text[a:b + 1])


def ask_json(client, system: str, user: str):
    """Returns (data|None, [Reply...], error|None). Retries once if the model's reply is not valid JSON."""
    replies, err = [], None
    for attempt in range(2):
        prompt = user if attempt == 0 else user + "\n\nYour previous reply was not valid JSON. Reply with ONE valid JSON object only."
        try:
            rep = client.complete(system, prompt)
        except LLMError as e:
            return None, replies, str(e)
        replies.append(rep)
        try:
            return parse_json(rep.text), replies, None
        except ValueError:
            err = "model did not return valid JSON"
    return None, replies, err


def log_usage(conn, task, model, rep: Reply, ok: bool) -> None:
    conn.execute("INSERT INTO llm_usage VALUES(?,?,?,?,?,?,?)",
                 (task, model, rep.prompt_tokens, rep.completion_tokens, rep.seconds, int(ok), time.time()))
    conn.commit()


def usage_summary(conn) -> dict:
    rows = conn.execute("""SELECT task, model, count(*) calls, sum(prompt_tokens) p, sum(completion_tokens) c,
                           sum(seconds) s, sum(1-ok) failed FROM llm_usage GROUP BY task, model""").fetchall()
    return {"by_task": [{"task": r["task"], "model": r["model"], "calls": r["calls"], "prompt_tokens": r["p"] or 0,
                         "completion_tokens": r["c"] or 0, "seconds": round(r["s"] or 0, 1), "failed": r["failed"] or 0}
                        for r in rows],
            "calls": sum(r["calls"] for r in rows), "prompt_tokens": sum(r["p"] or 0 for r in rows),
            "completion_tokens": sum(r["c"] or 0 for r in rows), "seconds": round(sum(r["s"] or 0 for r in rows), 1)}


def ping(client) -> dict:
    t = time.time()
    rep = client.complete('Reply with exactly this JSON: {"ok": true}', "ping")
    return {"model": client.label, "reply": rep.text.strip()[:80], "seconds": round(time.time() - t, 2)}
