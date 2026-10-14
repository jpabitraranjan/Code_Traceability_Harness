"""Code-quality signals (Ruff, Pyright) attached to the innermost symbol of each finding."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .impact import symbol_at


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)


def _rel(repo: Path, f: str) -> str:
    try:
        return Path(f).resolve().relative_to(repo).as_posix()
    except ValueError:
        return f


def _store(conn, rows):
    conn.executemany("INSERT INTO diagnostics VALUES(?,?,?,?,?,?)", rows)
    conn.commit()


def run(conn, repo: Path, tools=("ruff",)) -> dict:
    conn.execute("DELETE FROM diagnostics")
    counts = {}
    for tool in tools:
        rows = []
        try:
            if tool == "ruff":
                exe = [shutil.which("ruff")] if shutil.which("ruff") else [sys.executable, "-m", "ruff"]
                out = _run([*exe, "check", "--output-format", "json", "--no-cache", "--exit-zero", "."], repo).stdout
                for d in json.loads(out or "[]"):
                    path, line = _rel(repo, d["filename"]), d["location"]["row"]
                    s = symbol_at(conn, path, line)
                    if s:
                        rows.append((s["id"], "ruff", d.get("code") or "", "warning", d["message"], line))
            elif tool == "pyright":
                exe = shutil.which("pyright")
                if not exe:
                    counts[tool] = "not installed"
                    continue
                out = _run([exe, "--outputjson"], repo).stdout
                for d in json.loads(out or "{}").get("generalDiagnostics", []):
                    path, line = _rel(repo, d["file"]), d["range"]["start"]["line"] + 1
                    s = symbol_at(conn, path, line)
                    if s:
                        rows.append((s["id"], "pyright", d.get("rule", ""), d["severity"], d["message"], line))
        except Exception as e:  # quality signals are best-effort
            counts[tool] = f"failed: {e.__class__.__name__}"
            continue
        _store(conn, rows)
        counts[tool] = len(rows)
    return counts
