"""Imported at interpreter start (via PYTHONPATH) by `tracegraph coverage run`: measures subprocesses and attributes
what they execute to the test that started them."""
import os
import sys

try:
    import coverage

    coverage.process_startup()
    _cov = coverage.Coverage.current()  # process_startup() may return None even when it started measuring
    _test = os.environ.get("TRACEGRAPH_TEST")
    if _cov is not None and _test:
        _cov.switch_context(_test)
except Exception:
    pass

try:  # we shadow any existing sitecustomize on the path: run it too
    import importlib.machinery
    import importlib.util

    _here = os.path.dirname(os.path.abspath(__file__))
    _spec = importlib.machinery.PathFinder.find_spec(
        "sitecustomize", [p for p in sys.path if os.path.abspath(p or ".") != _here])
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
except Exception:
    pass
