"""Minimal fake LSP server for tests: answers textDocument/definition by finding `def <name>` / `class <name>` in
the repo, and (like Pyright) asks the client for workspace/configuration first."""
import json, os, re, sys
from pathlib import Path
from urllib.parse import unquote, urlparse

root, docs = None, {}

def send(o):
    d = json.dumps(o).encode(); sys.stdout.buffer.write(f"Content-Length: {len(d)}\r\n\r\n".encode() + d); sys.stdout.buffer.flush()

def read():
    h = {}
    line = sys.stdin.buffer.readline()
    if not line: return None
    while line.strip():
        k, _, v = line.decode().partition(":"); h[k.lower()] = v.strip(); line = sys.stdin.buffer.readline()
    return json.loads(sys.stdin.buffer.read(int(h["content-length"])))

PREFER = os.environ.get("FAKE_PREFER", "")  # "B.total": pick this def when several match
while (m := read()) is not None:
    meth, i = m.get("method"), m.get("id")
    if meth == "initialize":
        root = Path(unquote(urlparse(m["params"]["rootUri"]).path))
        send({"jsonrpc": "2.0", "id": 900, "method": "workspace/configuration", "params": {"items": [{"section": "python"}]}})
        send({"jsonrpc": "2.0", "id": i, "result": {"capabilities": {"definitionProvider": True}}})
    elif meth == "textDocument/didOpen":
        td = m["params"]["textDocument"]; docs[td["uri"]] = td["text"]
        if os.environ.get("FAKE_DIAG"):
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": td["uri"], "diagnostics": [
                {"message": 'Import "django.shortcuts" could not be resolved', "severity": 1}]}})
    elif meth == "textDocument/definition":
        p = m["params"]; line = docs[p["textDocument"]["uri"]].split("\n")[p["position"]["line"]]
        ident = re.match(r"\w+", line[p["position"]["character"]:])
        hits = []
        if ident and os.environ.get("FAKE_BUILTIN") == ident.group(0):  # answer with a bundled builtins stub
            hits.append({"uri": "file:///v/site-packages/pyright/dist/dist/typeshed-fallback/stdlib/builtins.pyi",
                         "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}})
        elif ident:
            for f in sorted(root.rglob("*.py")):
                cls = None
                for n, l in enumerate(f.read_text().split("\n")):
                    mc = re.match(r"class (\w+)", l)
                    if mc: cls = mc.group(1)
                    md = re.match(r"(\s*)(def|class) " + ident.group(0) + r"\b", l)
                    if md and (not PREFER or f"{cls}.{ident.group(0)}" == PREFER or md.group(2) == "class"):
                        hits.append({"uri": "file://" + str(f), "range": {"start": {"line": n, "character": len(md.group(0)) - len(ident.group(0))}, "end": {"line": n, "character": 0}}})
        send({"jsonrpc": "2.0", "id": i, "result": hits or None})
    elif meth == "shutdown":
        send({"jsonrpc": "2.0", "id": i, "result": None})
    elif meth == "exit":
        break
