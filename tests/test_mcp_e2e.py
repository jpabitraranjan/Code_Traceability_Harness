import asyncio
import json
import os
import sys

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from test_core import repo  # noqa: E402,F401


def test_every_mcp_tool_works_over_stdio(repo):
    async def go():
        # the MCP client forwards only HOME/PATH/TERM to the server: pass the runtime-coverage variables on explicitly,
        # otherwise `tracegraph coverage run` cannot see what this server executes (a no-op outside such a run)
        fwd = {k: os.environ[k] for k in ("COVERAGE_PROCESS_START", "PYTHONPATH", "TRACEGRAPH_TEST") if k in os.environ}
        params = StdioServerParameters(command=sys.executable, args=["-m", "tracegraph.mcp_server", "--repo", str(repo)], env=fwd)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()

                async def call(name, args):
                    res = await s.call_tool(name, args)
                    assert not getattr(res, "is_error", getattr(res, "isError", False)), (name, res)
                    return json.loads(res.content[0].text)

                assert (await call("find_symbol", {"query": "base"}))["matches"][0]["id"] == "pkg/calc.py::base"
                assert "called_by" in await call("get_context", {"target": "pkg/calc.py::base"})
                assert (await call("impact_of_symbols", {"targets": ["pkg/calc.py::base"]}))["tests_to_run"] == ["tests/test_svc.py::test_run"]
                assert "changed" in await call("impact_of_diff", {})
                assert "requirements" in await call("requirements_for", {"target": "pkg/calc.py::base"})
                assert "requirements" in await call("list_requirements", {})
                assert "matches" in await call("search_by_intent", {"query": "compute"})
                assert "files" in await call("reindex", {})
    asyncio.run(go())
