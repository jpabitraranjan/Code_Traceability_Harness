"""MCP server exposing tracegraph to coding agents (stdio).

  tracegraph-mcp --repo /path/to/repo [--requirements requirements.yaml] [--db graph.db] [--no-auto-index]
Works with mcp 1.x (FastMCP) and 2.x (MCPServer).
"""
from __future__ import annotations

import argparse

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .agent_api import Workspace

INSTRUCTIONS = (
    "Code knowledge graph of the repository (symbols with exact line ranges, call/inheritance edges, tests, "
    "requirements). Use it BEFORE editing: get_context to understand a symbol and its neighbours, "
    "impact_of_diff after editing to learn which code, tests and requirements may be affected. "
    "Symbol ids look like 'pkg/mod.py::Class.method'; 'pkg/mod.py:42' (a line) also works. "
    "Edges with heuristic=true or low confidence are name-matched guesses, not proofs."
)


def build(ws: Workspace):
    mcp = _Server("tracegraph", instructions=INSTRUCTIONS)

    @mcp.tool()
    def find_symbol(query: str, limit: int = 10) -> dict:
        """Find symbols by name or id fragment (e.g. 'calculate_tax'). Returns ids to use with the other tools."""
        return ws.find_symbol(query, limit)

    @mcp.tool()
    def get_context(target: str, include_source: bool = True, max_edges: int = 20) -> dict:
        """Everything about one symbol: its source with line numbers, what it calls, who calls it (with call-site lines),
        inheritance, tests that reach it, requirements it implements or supports, and lint/type findings.
        target: symbol id, bare name, or 'path.py:LINE'."""
        return ws.get_context(target, include_source, max_edges)

    @mcp.tool()
    def impact_of_diff(base: str = "HEAD", depth: int = 6, min_confidence: float = 0.0) -> dict:
        """After (or before committing) edits: maps the git diff vs `base` to changed symbols, then returns the code,
        tests (with a ready pytest command) and requirements that may be affected, ordered by distance."""
        return ws.impact_of_diff(base, depth, min_confidence)

    @mcp.tool()
    def impact_of_symbols(targets: list[str], depth: int = 6, min_confidence: float = 0.0) -> dict:
        """What-if analysis: what would be affected if these symbols changed? Does not need an actual edit."""
        return ws.impact_of_symbols(targets, depth, min_confidence)

    @mcp.tool()
    def requirements_for(target: str) -> dict:
        """Which requirements does this code implement, or support through its callers? Use to recover intent."""
        return ws.requirements_for(target)

    @mcp.tool()
    def list_requirements(status: str | None = None) -> dict:
        """All requirements with status, implementing symbols, tests reaching them, and design notes.
        status filter: planned | partial | implemented."""
        return ws.list_requirements(status)

    @mcp.tool()
    def reindex() -> dict:
        """Force a re-index. Normally unnecessary: queries refresh the index automatically."""
        return ws.reindex()

    return mcp


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tracegraph-mcp", description=__doc__)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--db")
    ap.add_argument("--requirements")
    ap.add_argument("--no-auto-index", action="store_true")
    a = ap.parse_args(argv)
    build(Workspace(a.repo, a.db, a.requirements, auto_index=not a.no_auto_index)).run()  # stdio


if __name__ == "__main__":
    main()
