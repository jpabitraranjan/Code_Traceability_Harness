import pytest

from tracegraph import impact as imp
from tracegraph.graph import connect, index_repo
from tracegraph.server import App

FILES = {
    "app/models.py": "from django.db import models\n\nclass Entry(models.Model):\n    def clean(self):\n        return 1\n"
                     "    def save(self, *a):\n        self.clean()\n        super().save(*a)\n",
    "app/forms.py": "from django import forms\n\nclass F(forms.ModelForm):\n    def clean(self):\n        return super().clean()\n",
    "app/views.py": "from django.views import View\n\ndef home(request):\n    return 1\n\nclass Board(View):\n    def get(self, r):\n        return home(r)\n",
    "app/urls.py": "from django.urls import path, include\nfrom . import views\n"
                   "urlpatterns = [path('', views.home), path('b/', views.Board.as_view()), path('x/', include('o.urls'))]\n",
    "app/tests.py": "from django.test import TestCase\nfrom .views import home\n\nclass HomeTests(TestCase):\n    def test_home(self):\n        home(None)\n",
    "app/migrations/0001_initial.py": "def forwards():\n    pass\n",
    "rec.py": "def fact(n):\n    return n * fact(n - 1)\n",
}


@pytest.fixture()
def repo(tmp_path):
    for p, c in FILES.items():
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
    index_repo(tmp_path, tmp_path / "g.db")
    return tmp_path


def calls(repo):
    return {(r["src"], r["dst"], r["provenance"]) for r in
            connect(repo / "g.db").execute("SELECT src,dst,provenance FROM edges WHERE kind='calls'")}


def test_super_resolves_to_external_base_not_heuristic_guess(repo):
    e = calls(repo)
    assert ("app/forms.py::F.clean", "ext::django.forms.ModelForm.clean", "static") in e
    assert ("app/forms.py::F.clean", "app/models.py::Entry.clean", "heuristic") not in e
    assert not any(p == "heuristic" and s == d for s, d, p in e)


def test_static_recursion_is_kept(repo):
    assert ("rec.py::fact", "rec.py::fact", "static") in calls(repo)


def test_testcase_methods_are_tests_and_migrations_skipped(repo):
    c = connect(repo / "g.db")
    tests = {r["id"] for r in c.execute("SELECT id FROM nodes WHERE is_test=1 AND kind IN ('function','method')")}
    assert tests == {"app/tests.py::HomeTests.test_home"}
    assert not list(c.execute("SELECT 1 FROM nodes WHERE path LIKE '%migrations%'"))


def test_routes_link_to_views_and_show_in_impact(repo):
    e = calls(repo)
    assert any(s.startswith("app/urls.py::route:") and d == "app/views.py::home" for s, d, p in e)
    assert any(s.startswith("app/urls.py::route:b/") and d == "app/views.py::Board" for s, d, p in e)
    res = imp.impact(connect(repo / "g.db"), {"app/views.py::home": []})
    assert "route" in {i["kind"] for i in res["impacted"]}
    assert [t["pytest_id"] for t in res["tests"]] == ["app/tests.py::HomeTests::test_home"]


def test_ui_backend_flow(repo):
    app = App(repo)
    st = app.status()
    assert st["routes"] == 2 and st["tests"] == 1
    out = app.impact(targets=["app/views.py::home"])
    assert {n["role"] for n in out["graph"]["nodes"]} >= {"changed", "impacted", "test"}
    assert "error" in app.impact()
    assert "error" in app.trace(id="NOPE")
