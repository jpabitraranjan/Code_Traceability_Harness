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

## LLM layer (intent / semantic analysis)

Pick any model, like Ollama. No extra dependencies; keys come from the environment only.

    tracegraph llm config --llm ollama:qwen2.5-coder:7b          # saved to .tracegraph/llm.json
    #   openai:<model> (set OPENAI_API_KEY; --llm-url for LM Studio / vLLM / Groq / any OpenAI-compatible server)
    #   anthropic:<model> (set ANTHROPIC_API_KEY)
    tracegraph llm models            # what the provider offers
    tracegraph llm test
    tracegraph llm summarize --limit 200     # 1-line summary + intent + side effects per symbol; cached by source hash
    tracegraph llm draft                     # writes requirements.draft.yaml from README + code + routes: review it, then rename
    tracegraph llm link                      # proposes requirement -> code/test links (kept as `inferred`, with confidence)
    tracegraph llm usage                     # tokens + time per task: the indexing cost for your benchmark
    tracegraph search "where are timetable clashes checked"   # find code by intent

Design rules: static facts and model output are stored separately. LLM links are `inferred` with a confidence and
a rationale, never mixed into declared links; invalid or hallucinated ids are dropped; summaries are flagged
`stale` when the code changed since they were written; re-indexing keeps them. Agents get `intent` in
`get_context`, one-line summaries on affected symbols, and a `search_by_intent` MCP tool.
In the web UI use the LLM bar (provider, model, Save, List models, Test, Summarize code, Infer links, Draft requirements).
Tested against stub servers for each API shape and a deterministic fake model, not against live providers.

## Type-aware resolution (Pyright)

Name matching cannot tell which `total()` is meant in `order.total()`; Pyright can, because it infers the type of `order`.

    tracegraph resolve --repo .                 # ask Pyright about calls left heuristic/unresolved (needs: pip install pyright, Node.js)
    tracegraph resolve --repo . --all           # also cross-check the static edges; prints agree/disagree counts
    tracegraph resolve --repo . --python /path/to/venv/bin/python    # interpreter whose packages Pyright should see
    tracegraph index --repo . --resolve         # index, then resolve in one step     (UI: "Resolve types" button)

How it works: the parser records the exact line/column of each callee name; for uncertain sites the Pyright language
server is asked `textDocument/definition`. Repo targets become `typed` edges (confidence 0.95, counted as trusted in
impact results) and replace the name-matching guesses at that site; static edges are never overridden. Library targets
become `ext::<module>.<name>` (builtins/typing are skipped as noise). Answers are cached per file hash: unchanged files
keep them across re-indexing, edited files drop them and fall back to heuristics until you resolve again.
Limits: Pyright only sees third-party packages installed in the interpreter it finds (use `--python`), dynamic code
(`getattr`, ORM managers without stubs) stays unresolved, and external names lack the class (`module.method`).

## Excluding folders: .tracegraphignore
`env/`, `venv/`, `node_modules/`, `migrations/` and similar are skipped automatically. For anything else (a sample project
nested inside the repo, vendored code, generated files) add `<repo>/.tracegraphignore`, one glob per line:

    # nested sample project
    pythonProjects/
    vendor/
    *_pb2.py
    docs/examples/**

`dir/` matches directories only; a pattern containing `/` is matched against the repo-relative path, otherwise against
the file or folder name. Re-run `tracegraph index`; files that are now ignored are removed from the graph.

## Reading `tracegraph resolve --all`
`agree` / `disagree` compare our static edges with Pyright only where both point at code in the repo; `external_only`
counts sites where Pyright says "library code" (names differ, so they cannot be compared); `builtin_calls` are answers
that landed in builtins/typing (e.g. `tuple.index`): any name-matching guess at those sites is removed.

## Runtime coverage: which tests actually execute which code

Static analysis cannot see through subprocesses, dynamic dispatch or framework glue; execution can.

    pip install coverage                                      # (in requirements.txt)
    tracegraph index --repo .                                 # the index must be current
    tracegraph coverage run --repo . -- pytest -q             # default command is `pytest -q`
    tracegraph coverage run --repo . -- python manage.py test # unittest / Django style (see below)
    tracegraph coverage status --repo .

What you get:
- `covers` edges (test -> symbol, provenance `runtime`) and, per test, the exact executed lines. They survive re-indexing.
- `impact` tags every test with its evidence: `static` (call-graph reachable), `ran-symbol` (executed the changed
  function), `ran-line` (executed the exact lines the diff touched; measured against the pre-change line numbers, so run
  coverage on a clean commit). A test that ran the function but none of the changed lines is flagged "not the changed lines".
  Agents get `tests_that_executed_changed_code`: run those first.
- `tracegraph requirements` (and the UI) lists implementing code that **no test ever executed**, which call-graph
  reachability alone overstates.

How it works / limits:
- pytest: a small plugin (copied into `.tracegraph/cov/boot`, no install needed) gives every test its own coverage
  context; subprocesses are measured too and attributed to the test that started them (`COVERAGE_PROCESS_START` +
  a `sitecustomize` hook). Other runners use coverage's `dynamic_context = test_function`: only code running inside
  test functions is attributed (setUp/fixtures are not), and subprocesses are not attributed to a test.
- Launchers that sanitise the child's environment (the MCP stdio client forwards only HOME/PATH/TERM) drop the coverage
  variables: pass `COVERAGE_PROCESS_START`, `PYTHONPATH`, `TRACEGRAPH_TEST` through explicitly (see tests/test_mcp_e2e.py).
- Line numbers belong to the file version measured. A manifest of file hashes is written when the tests start; if a
  file was edited afterwards its line data is kept for old-side matching but symbol links are not guessed.
- Parametrised tests share one context; coverage must be installed in the interpreter that runs the tests (`--python`).
