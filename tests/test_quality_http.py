import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tracegraph import quality
from tracegraph.graph import connect, index_repo
from tracegraph.server import App, Handler

from test_core import repo  # noqa: F401


def test_ruff_findings_attach_to_the_symbol_that_contains_them(tmp_path):
    (tmp_path / "x.py").write_text("import os\n\n\ndef f():\n    import sys\n    return 1\n")
    index_repo(tmp_path, tmp_path / ".tracegraph" / "graph.db")
    conn = connect(tmp_path / ".tracegraph" / "graph.db")
    counts = quality.run(conn, tmp_path, tools=("ruff",))
    assert counts["ruff"] >= 2
    owners = {r["node_id"] for r in conn.execute("SELECT node_id FROM diagnostics")}
    assert owners == {"x.py::<module>", "x.py::f"}  # unused import at module level vs inside f


def test_http_layer_serves_the_ui_and_json_errors(repo):
    Handler.app = App(repo)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        assert b"tracegraph" in urllib.request.urlopen(base + "/").read()
        assert json.load(urllib.request.urlopen(base + "/api/status"))["symbols"] >= 3
        post = lambda path, body: urllib.request.urlopen(urllib.request.Request(
            base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))
        assert json.load(post("/api/impact", {"targets": ["pkg/calc.py::base"]}))["result"]["changed"]
        for path, body, code in (("/api/impact", {}, 400), ("/api/nope", {}, 404)):
            with pytest.raises(urllib.error.HTTPError) as e:
                post(path, body)
            assert e.value.code == code and "error" in json.load(e.value)
    finally:
        srv.shutdown()
