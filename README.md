# tracegraph (pilot)

Builds a line-level knowledge graph of a Python repo — requirements → design → symbols → code → tests —
so coding agents can ask "what does this do, who depends on it, what breaks if I change it?".

    pip install -e .            # add [quality] for ruff + pyright
    tracegraph index --repo . --requirements requirements.yaml --ruff
    tracegraph impact           # git diff -> changed symbols -> affected code, tests, requirements
    tracegraph impact --symbol pkg/mod.py:42 --depth 3
    tracegraph show pkg/mod.py::Class.method
    tracegraph requirements     # status + implementation/test coverage
    tracegraph export --out graph.json

Pipeline: Tree-sitter (symbols, exact line ranges, calls, imports) → name resolution (scopes, imports,
re-exports, `self.`, inheritance) → SQLite → NetworkX traversal. Git supplies changed lines.
Every edge carries `provenance` (static | heuristic | declared) and `confidence`.

Known limits (by design for the pilot): no type inference (`obj.method()` on locals is heuristic, 0.25-0.5),
no framework magic (route decorators, fixtures, ORM), deleted symbols map to the module, Python only.
Next: Pyright-based call resolution, coverage.py test links, LLM intent layer, MCP server, web UI.
