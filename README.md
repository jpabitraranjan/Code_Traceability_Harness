# tracegraph (pilot)

Builds a line-level knowledge graph of a Python repo — requirements → design → symbols → code → tests —
so coding agents can ask "what does this do, who depends on it, what breaks if I change it?".

    pip install -e .            # add [quality] for ruff + pyright
    tracegraph index --repo . --requirements requirements.yaml --ruff
    tracegraph impact           # git diff -> changed symbols -> affected code, tests, requirements
    tracegraph impact --symbol pkg/mod.py:42 --depth 3
    tracegraph find calculate_tax          # look up symbol ids
    tracegraph show pkg/mod.py::Class.method
    tracegraph requirements     # status + implementation/test coverage
    tracegraph export --out graph.json

Pipeline: Tree-sitter (symbols, exact line ranges, calls, imports) → name resolution (scopes, imports,
re-exports, `self.`, inheritance) → SQLite → NetworkX traversal. Git supplies changed lines.
Every edge carries `provenance` (static | heuristic | declared) and `confidence`.

Known limits (by design for the pilot): no type inference (`obj.method()` on locals is heuristic, 0.25-0.5),
no framework magic (route decorators, fixtures, ORM), deleted symbols map to the module, Python only.
Next: Pyright-based call resolution, coverage.py test links, LLM intent layer, MCP server, web UI.

## MCP server (for coding agents)

    pip install -e ".[mcp]"
    tracegraph-mcp --repo /path/to/repo [--requirements requirements.yaml]

Claude Code:   `claude mcp add tracegraph -- tracegraph-mcp --repo /path/to/repo`
Other clients (Claude Desktop, Cursor, ...): add to the MCP config

    {"mcpServers": {"tracegraph": {"command": "tracegraph-mcp", "args": ["--repo", "/path/to/repo"]}}}

Tools: `find_symbol`, `get_context`, `impact_of_diff`, `impact_of_symbols`, `requirements_for`,
`list_requirements`, `reindex`. Queries re-index incrementally first, so the graph tracks the working tree.
Add `.tracegraph/` to the target repo's .gitignore.

## Web UI

    tracegraph ui --repo /path/to/repo        # opens http://127.0.0.1:8765 (local only, no extra dependencies)

Header: open a repo folder, pick a requirements file, **Index** (re-index; unchanged files are skipped).
- **A · Requirements**: searchable list with status, implementing symbols, tests reaching them, coverage.
- **B · Traceability**: requirement -> design decisions -> code -> tests; click any symbol for its source and neighbours.
- **C · Change impact**: load the git diff and/or add symbols, then *Analyze impact*: affected code, tests to run
  (copyable pytest command), affected requirements, and a graph (red changed, orange affected, green tests, blue requirements).
The graph uses Cytoscape.js from a CDN; offline, the lists still work.

## Django notes
`super().x()` / `self.x()` resolve through base classes (external bases become `ext::` nodes), `TestCase` subclasses count
as tests, `migrations/` is skipped, and `path()/re_path()/url()/router.register()` routes become `route` nodes linked to
their views. Still invisible: ORM managers (`Model.objects`), signals, admin registration, template/URL-name lookups.

## requirements.yaml
    requirements:
      - id: REQ-001
        title: Tax is computed from country rate
        status: implemented            # planned | partial | implemented
        design: ["Rates are a static table in shop/rates.py"]
        implemented_by: ["shop/billing.py::calculate_tax", "shop/rates.py"]   # fnmatch on symbol ids
        verified_by: ["tests/test_shop.py::test_tax"]
Auto-detected as `requirements.yaml` in the repo root.
