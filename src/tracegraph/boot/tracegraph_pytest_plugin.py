"""pytest plugin injected by `tracegraph coverage run`: one coverage context per test (setup + call + teardown)."""
import os

import pytest

try:
    import coverage
except ImportError:  # coverage is not installed: do nothing
    coverage = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    cov = coverage.Coverage.current() if coverage else None
    os.environ["TRACEGRAPH_TEST"] = item.nodeid  # inherited by subprocesses the test starts
    if cov is not None:
        cov.switch_context(item.nodeid)
    yield
    os.environ.pop("TRACEGRAPH_TEST", None)
    if cov is not None:
        cov.switch_context("")
