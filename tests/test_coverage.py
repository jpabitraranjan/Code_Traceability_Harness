import subprocess
from pathlib import Path

import pytest

pytest.importorskip("coverage")
from tracegraph import covlink, impact as imp, requirements as rq  # noqa: E402
from tracegraph.agent_api import Workspace  # noqa: E402
from tracegraph.cli import main  # noqa: E402
from tracegraph.graph import connect, index_repo  # noqa: E402

FILES = {
    "pkg/__init__.py": "",
    "pkg/calc.py": ('def add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\n\n'
                    'def branchy(flag):\n    if flag:\n        return "yes"\n    else:\n        return "no"\n\n'
                    'def unused():\n    return 0\n'),
    "pkg/cli.py": 'from pkg.calc import mul\n\nif __name__ == "__main__":\n    print(mul(2, 3))\n',
    "tests/test_calc.py": ('import subprocess\nimport sys\n\nfrom pkg.calc import add, branchy\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n'
                           'def test_branch_yes():\n    assert branchy(True) == "yes"\n\n\nclass TestCli:\n    def test_cli(self):\n'
                           '        out = subprocess.run([sys.executable, "-m", "pkg.cli"], capture_output=True, text=True)\n'
                           '        assert out.stdout.strip() == "6"\n'),
    "requirements.yaml": ("requirements:\n  - id: REQ-1\n    title: Multiplication works from the command line\n"
                          "    implemented_by: ['pkg/calc.py::mul']\n"
                          "  - id: REQ-2\n    title: Unused helper\n    implemented_by: ['pkg/calc.py::unused']\n"),
}


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    for p, c in FILES.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".tracegraph/\n__pycache__/\n.pytest_cache/\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    index_repo(tmp_path, tmp_path / ".tracegraph" / "graph.db", requirements=tmp_path / "requirements.yaml")
    return tmp_path


def conn_of(repo):
    return connect(repo / ".tracegraph" / "graph.db")


def covers(repo):
    return {(r["src"], r["dst"]) for r in conn_of(repo).execute("SELECT src, dst FROM edges WHERE kind='covers'")}


def test_pack_unpack_roundtrip():
    assert covlink.pack([5, 1, 2, 3, 9, 10]) == "1-3,5,9-10" and covlink.unpack("1-3,5,9-10") == [1, 2, 3, 5, 9, 10]


def test_runtime_links_see_through_a_subprocess_that_static_analysis_cannot(repo):
    c = conn_of(repo)
    assert imp.impact(c, {"pkg/calc.py::mul": []})["tests"] == []  # static: the test only spawns a process
    stats = covlink.run(repo, c, ["pytest", "-q", "-p", "no:cacheprovider"])
    assert stats["exit_code"] == 0 and stats["mode"] == "pytest" and stats["tests_with_data"] == 3
    e = covers(repo)
    assert ("tests/test_calc.py::TestCli.test_cli", "pkg/calc.py::mul") in e      # executed in the subprocess
    assert ("tests/test_calc.py::test_add", "pkg/calc.py::add") in e
    assert not any(d == "pkg/calc.py::unused" for _, d in e)                        # nothing ever ran it
    res = imp.impact(c, {"pkg/calc.py::mul": []})
    t = res["tests"][0]
    assert t["pytest_id"] == "tests/test_calc.py::TestCli::test_cli" and t["evidence"] == ["runtime-symbol"]
    assert res["runtime"]["available"] is True
    rows = {r["id"]: r for r in rq.report(c)}  # requirement coverage now sees the test too
    assert "tests/test_calc.py::TestCli.test_cli" in rows["REQ-1"]["tests_reaching"] and rows["REQ-1"]["runtime_tests"]
    assert rows["REQ-1"]["unexecuted"] == [] and rows["REQ-2"]["unexecuted"] == ["pkg/calc.py::unused"]  # never ran
    index_repo(repo, repo / ".tracegraph" / "graph.db")  # runtime edges survive re-indexing
    assert ("tests/test_calc.py::TestCli.test_cli", "pkg/calc.py::mul") in covers(repo)


def test_exact_changed_lines_separate_tests_that_ran_them_from_ones_that_only_ran_the_function(repo):
    c = conn_of(repo)
    covlink.run(repo, c, ["pytest", "-q", "-p", "no:cacheprovider"])
    calc = repo / "pkg/calc.py"
    calc.write_text(FILES["pkg/calc.py"].replace('return "no"', 'return "nope"').replace("a * b", "a * b + 0"))
    index_repo(repo, repo / ".tracegraph" / "graph.db")
    res = imp.impact_for_diff(conn_of(repo), repo)
    by = {t["pytest_id"]: t for t in res["tests"]}
    assert set(by) == {"tests/test_calc.py::TestCli::test_cli", "tests/test_calc.py::test_branch_yes"}  # test_add ran neither
    assert "runtime-line" in by["tests/test_calc.py::TestCli::test_cli"]["evidence"]
    miss = by["tests/test_calc.py::test_branch_yes"]
    assert miss["evidence"] == ["static", "runtime-symbol"] and miss["line_check"] == "miss"  # ran branchy(), never the `else` line
    assert res["tests"][0]["pytest_id"] == "tests/test_calc.py::TestCli::test_cli"      # measured hits sort first
    out = Workspace(repo).impact_of_diff()
    assert out["tests_that_executed_changed_code"] == ["tests/test_calc.py::TestCli::test_cli"]


def test_unittest_style_runs_use_function_contexts(repo):
    (repo / "tests/test_ut.py").write_text("import unittest\nfrom pkg.calc import add\n\n\nclass UT(unittest.TestCase):\n"
                                           "    def test_x(self):\n        self.assertEqual(add(1, 1), 2)\n")
    index_repo(repo, repo / ".tracegraph" / "graph.db")
    stats = covlink.run(repo, conn_of(repo), ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_ut.py"])
    assert stats["mode"] == "function" and stats["exit_code"] == 0
    assert ("tests/test_ut.py::UT.test_x", "pkg/calc.py::add") in covers(repo)


def test_data_for_edited_files_is_kept_at_line_level_but_not_trusted_for_symbols(repo):
    covlink.run(repo, conn_of(repo), ["pytest", "-q", "-p", "no:cacheprovider"])
    (repo / "pkg/calc.py").write_text("# shifted\n" + FILES["pkg/calc.py"])
    index_repo(repo, repo / ".tracegraph" / "graph.db")
    st = covlink.import_data(conn_of(repo), repo)  # re-import coverage that predates the edit
    assert st["files_skipped_stale"] >= 1
    c = conn_of(repo)
    assert not any(d.startswith("pkg/calc.py") for _, d in covers(repo))                      # symbol ranges moved: not guessed
    old = c.execute("SELECT file_hash FROM covlines WHERE path='pkg/calc.py' LIMIT 1").fetchone()["file_hash"]
    assert old != covlink.sha(repo / "pkg/calc.py")                                             # but remembered as the OLD version


def test_run_refuses_an_out_of_date_index(repo):
    (repo / "pkg/calc.py").write_text(FILES["pkg/calc.py"] + "\n# edit\n")
    with pytest.raises(covlink.CoverageError, match="tracegraph index"):
        covlink.run(repo, conn_of(repo), ["pytest"])
    with pytest.raises(covlink.CoverageError, match="don't know how to run"):
        covlink.normalize_command(["tox"])


def test_cli_coverage_commands(repo, capsys):
    main(["coverage", "run", "--repo", str(repo), "--", "pytest", "-q", "-p", "no:cacheprovider"])
    assert "tests_with_data=3" in capsys.readouterr().out
    main(["coverage", "status", "--repo", str(repo)])
    assert "available=True" in capsys.readouterr().out
