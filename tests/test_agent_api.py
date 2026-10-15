from tracegraph.agent_api import Workspace
from test_core import FILES, repo  # noqa: F401  (reuse the fixture)


def test_get_context_and_ambiguity(repo):
    ws = Workspace(repo)
    c = ws.get_context("pkg/calc.py::base")
    assert c["source"].startswith("1: def base")
    assert {x["id"] for x in c["called_by"]} == {"pkg/calc.py::compute", "pkg/svc.py::Svc.helper"}
    assert c["tests_reaching_this"][0]["pytest_id"] == "tests/test_svc.py::test_run"
    amb = ws.get_context("helper_that_does_not_exist")
    assert "error" in amb


def test_impact_of_diff_is_compact_and_has_run_command(repo):
    (repo / "pkg/calc.py").write_text(FILES["pkg/calc.py"].replace("x * 2", "x * 3"))
    r = Workspace(repo).impact_of_diff()
    assert r["changed"][0]["id"] == "pkg/calc.py::base"
    assert r["run_command"] == "pytest tests/test_svc.py::test_run"


def test_find_symbol_prefers_exact_name(repo):
    assert Workspace(repo).find_symbol("base")["matches"][0]["id"] == "pkg/calc.py::base"
