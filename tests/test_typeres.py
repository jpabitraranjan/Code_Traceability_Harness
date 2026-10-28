import os
import sys
from pathlib import Path

import pytest

from tracegraph import typeres
from tracegraph.graph import connect, index_repo
from tracegraph.server import App

FAKE = [sys.executable, str(Path(__file__).with_name("fake_langserver.py"))]
REPO = {
    "m/a.py": "class A:\n    def total(self):\n        return 1\n",
    "m/b.py": "class B:\n    def total(self):\n        return 2\n",
    "m/use.py": "from m.b import B\n\ndef make():\n    return B()\n\ndef run():\n    o = make()\n    return o.total()\n",
}


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PREFER", "B.total")
    for p, c in REPO.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
    index_repo(tmp_path, tmp_path / ".tracegraph" / "graph.db")
    return tmp_path


def edges(repo):
    c = connect(repo / ".tracegraph" / "graph.db")
    return {(r["dst"], r["provenance"], r["confidence"]) for r in c.execute(
        "SELECT dst, provenance, confidence FROM edges WHERE kind='calls' AND src='m/use.py::run' AND dst LIKE '%total'")}


def test_ext_naming_and_utf16():
    P = Path
    assert typeres.ext_name(P("/v/site-packages/pyright/dist/dist/typeshed-fallback/stdlib/builtins.pyi"), "get") == typeres.NOISE
    assert typeres.ext_name(P("/v/site-packages/pyright/dist/dist/typeshed-fallback/stdlib/os/path.pyi"), "join") == "ext::os.path.join"
    assert typeres.ext_name(P("/v/site-packages/pyright/dist/dist/typeshed-fallback/stubs/requests/requests/api.pyi"), "get") == "ext::requests.api.get"
    assert typeres.ext_name(P("/v/lib/python3.12/site-packages/django/db/models/query.py"), "filter") == "ext::django.db.models.query.filter"
    assert typeres.ext_name(P("/usr/lib/python3.12/json/__init__.py"), "loads") == "ext::json.loads"
    assert typeres.ext_name(P("/home/me/proj/x.py"), "f") is None
    assert typeres.utf16_col("é = f()", 5) == 4 and typeres.utf16_col("😀 = f()", 7) == 5 and typeres.utf16_col("x = f()", 4) == 4


def test_ambiguous_method_call_is_narrowed_to_the_typed_target(repo):
    before = edges(repo)
    assert {d for d, p, c in before} == {"m/a.py::A.total", "m/b.py::B.total"} and all(p == "heuristic" for _, p, _ in before)
    c = connect(repo / ".tracegraph" / "graph.db")
    st = typeres.resolve(repo, c, server=FAKE)
    assert st["resolved_in_repo"] == 1 and st["heuristic_edges_after"] == 0
    assert edges(repo) == {("m/b.py::B.total", "typed", 0.95)}  # A.total no longer a false dependency


def test_answers_are_cached_and_dropped_when_the_file_changes(repo):
    db = repo / ".tracegraph" / "graph.db"
    c = connect(db)
    typeres.resolve(repo, c, server=FAKE)
    assert typeres.resolve(repo, c, server=FAKE)["sites_asked"] == 0
    index_repo(repo, db)  # unchanged file: typed edge survives a re-index
    assert edges(repo) == {("m/b.py::B.total", "typed", 0.95)}
    (repo / "m/use.py").write_text("# moved\n" + REPO["m/use.py"])
    index_repo(repo, db)  # line numbers shifted: the old answer must not be applied to the wrong site
    assert all(p == "heuristic" for _, p, _ in edges(repo))
    assert typeres.resolve(repo, c, server=FAKE)["resolved_in_repo"] == 1  # asked again, answered again


def test_cross_check_reports_disagreement_without_overriding_static(repo, monkeypatch):
    monkeypatch.setenv("FAKE_PREFER", "A.total")  # fake "Pyright" now disagrees with the static edge for run()->B()
    (repo / "m/use.py").write_text("from m.b import B\n\ndef run():\n    return B.total(None)\n")  # static: B.total
    db = repo / ".tracegraph" / "graph.db"
    index_repo(repo, db)
    c = connect(db)
    static = {r["dst"] for r in c.execute("SELECT dst FROM edges WHERE src='m/use.py::run' AND kind='calls'")}
    st = typeres.resolve(repo, c, include_static=True, server=FAKE)
    assert st["disagree"] >= 1 and st["disagreements"]
    assert static == {r["dst"] for r in c.execute("SELECT dst FROM edges WHERE src='m/use.py::run' AND kind='calls'")}


def test_missing_pyright_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(typeres.shutil, "which", lambda n: None)
    with pytest.raises(typeres.TypeResError, match="pip install pyright"):
        typeres.find_server()


def test_server_resolve_job_and_impact_labels(repo, monkeypatch):
    import time
    from tracegraph import impact as imp
    monkeypatch.setattr(typeres, "find_server", lambda: FAKE)
    app = App(repo)
    assert app.resolve() == {"started": "resolve"}
    for _ in range(200):
        if app.llm_job()["state"] != "running":
            break
        time.sleep(0.05)
    assert app.llm_job()["state"] == "done" and app.status()["typed_calls"] == 1
    res = imp.impact(connect(repo / ".tracegraph" / "graph.db"), {"m/b.py::B.total": []})
    assert res["impacted"][0]["id"] == "m/use.py::run" and res["impacted"][0]["provenance"] == "static"  # typed counts as trusted


def test_resolve_refuses_a_stale_or_empty_index_instead_of_reporting_nothing_to_do(repo):
    c = connect(repo / ".tracegraph" / "graph.db")
    c.execute("UPDATE files SET hash = '2' || substr(hash, 4)")  # looks like an index from an older version
    c.commit()
    with pytest.raises(typeres.TypeResError, match="tracegraph index"):
        typeres.resolve(repo, c, server=FAKE)
    c.execute("DELETE FROM files")
    with pytest.raises(typeres.TypeResError):
        typeres.resolve(repo, c, server=FAKE)


def test_cli_does_not_create_an_index_in_the_wrong_folder(tmp_path, capsys):
    from tracegraph.cli import main
    with pytest.raises(SystemExit) as e:
        main(["resolve", "--repo", str(tmp_path)])
    assert "tracegraph index --repo" in str(e.value) and not (tmp_path / ".tracegraph").exists()
    main(["llm", "config", "--repo", str(tmp_path)])  # config needs no index
    assert "provider=" in capsys.readouterr().out


def test_explains_missing_packages_when_pyright_cannot_see_them(repo, monkeypatch):
    monkeypatch.setenv("FAKE_DIAG", "1")
    st = typeres.resolve(repo, connect(repo / ".tracegraph" / "graph.db"), server=FAKE)
    assert st["unresolved_imports"] == ["django"] and "--python" in st["hint"]


def test_builtin_answer_removes_the_name_matching_guess(repo, monkeypatch):
    monkeypatch.setenv("FAKE_BUILTIN", "total")  # Pyright says: this .total() is a builtin, not our A.total / B.total
    db = repo / ".tracegraph" / "graph.db"
    st = typeres.resolve(repo, connect(db), server=FAKE)
    assert st["builtin_calls"] == 1 and st["heuristic_edges_after"] == 0 and edges(repo) == set()
    index_repo(repo, db)  # and it stays gone after a re-index of unchanged files
    assert edges(repo) == set()


def test_cross_check_does_not_call_library_vs_builtin_a_disagreement(repo, monkeypatch):
    monkeypatch.setenv("FAKE_BUILTIN", "write")
    (repo / "m/out.py").write_text("import sys\n\ndef out():\n    sys.stdout.buffer.write(b'x')\n")
    db = repo / ".tracegraph" / "graph.db"
    index_repo(repo, db)
    st = typeres.resolve(repo, connect(db), include_static=True, server=FAKE)
    assert st["disagree"] == 0 and st["external_only"] >= 1
