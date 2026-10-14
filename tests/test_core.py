import subprocess
from pathlib import Path

import pytest

from tracegraph import impact as imp
from tracegraph.graph import connect, index_repo

FILES = {
    "pkg/__init__.py": "from .calc import compute\n",
    "pkg/calc.py": "def base(x):\n    return x * 2\n\ndef compute(x):\n    return base(x) + 1\n",
    "pkg/svc.py": ("import pkg\nfrom pkg.calc import base\n\nclass Svc:\n    def run(self, x):\n"
                   "        return self.helper(x)\n    def helper(self, x):\n        return pkg.compute(x) + base(x)\n"),
    "tests/test_svc.py": "from pkg.svc import Svc\n\ndef test_run():\n    assert Svc().run(1) == 5\n",
}


def sh(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    for p, c in FILES.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
    sh(tmp_path, "init", "-q")
    sh(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    sh(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    index_repo(tmp_path, tmp_path / ".tg.db")
    return tmp_path


def edges(conn, kind="calls"):
    return {(r["src"], r["dst"]) for r in conn.execute("SELECT src,dst FROM edges WHERE kind=?", (kind,))}


def test_resolves_self_reexport_and_import_alias(repo):
    e = edges(connect(repo / ".tg.db"))
    assert ("pkg/svc.py::Svc.run", "pkg/svc.py::Svc.helper") in e
    assert ("pkg/svc.py::Svc.helper", "pkg/calc.py::compute") in e  # via package re-export
    assert ("pkg/svc.py::Svc.helper", "pkg/calc.py::base") in e      # from-import
    assert ("pkg/calc.py::compute", "pkg/calc.py::base") in e


def test_symbol_at_is_line_precise(repo):
    c = connect(repo / ".tg.db")
    assert imp.symbol_at(c, "pkg/calc.py", 2)["id"] == "pkg/calc.py::base"
    assert imp.symbol_at(c, "pkg/calc.py", 5)["id"] == "pkg/calc.py::compute"


def test_diff_drives_impact_and_tests(repo):
    (repo / "pkg/calc.py").write_text(FILES["pkg/calc.py"].replace("x * 2", "x * 3"))
    c = connect(repo / ".tg.db")
    index_repo(repo, repo / ".tg.db")
    seeds = imp.seeds_from_diff(c, repo)
    assert list(seeds) == ["pkg/calc.py::base"]
    res = imp.impact(c, seeds)
    ids = {i["id"] for i in res["impacted"]}
    assert {"pkg/calc.py::compute", "pkg/svc.py::Svc.helper", "pkg/svc.py::Svc.run"} <= ids
    assert [t["pytest_id"] for t in res["tests"]] == ["tests/test_svc.py::test_run"]


def test_incremental_index_skips_unchanged(repo):
    assert index_repo(repo, repo / ".tg.db")["reparsed"] == 0
    (repo / "pkg/calc.py").write_text(FILES["pkg/calc.py"] + "\n# touched\n")
    assert index_repo(repo, repo / ".tg.db")["reparsed"] == 1
