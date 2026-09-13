"""The version is one fact. These tests keep it recorded in one place.

It was previously recorded in three, and they drifted apart unnoticed across four
releases (vikunja#840):

    tag      pyproject   __version__   FastAPI version=
    v0.3.2   0.3.2       0.3.1         0.2.0
    v0.5.0   0.5.0       0.4.0         0.2.0

The third column is the damaging one: FastAPI's `version=` is `info.version` in
/openapi.json, so the published v0.5.0 told every API consumer it was 0.2.0 — a build
predating key scopes, /queue/summary and the dashboard.

`test_release_workflow.py` already checks pyproject against the CHANGELOG. These cover
the two in-code copies, which nothing checked.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from fastapi import FastAPI

from ollama_queue_proxy import __version__
from ollama_queue_proxy.main import app

ROOT = Path(__file__).resolve().parents[1]

# Every module under src/, so a NEW file carrying a version literal is caught too —
# pinning this to the two known offenders would let the next one through.
SRC = ROOT / "src" / "ollama_queue_proxy"


def _pyproject_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def test_the_package_version_matches_pyproject():
    """Read from the FILE, not importlib.metadata.

    An editable install's metadata can lag pyproject.toml until the package is
    reinstalled. Reading metadata here would fail on a stale install rather than on real
    drift, which is a flapping test, not a gate.
    """
    assert __version__ == _pyproject_version(), (
        f"__init__.py says {__version__}, pyproject.toml says {_pyproject_version()}"
    )


def test_the_api_advertises_the_package_version():
    """The value an OpenAPI consumer actually reads."""
    assert app.version == __version__
    assert app.openapi()["info"]["version"] == __version__


def test_the_openapi_check_is_not_vacuous():
    """CONTROL: prove the assertion above discriminates.

    A FastAPI app defaults to version "0.1.0". If `app.version` were somehow always
    equal to `__version__` by construction, the test above would pass while measuring
    nothing — so confirm a DIFFERENT app reports a different version through the same
    code path.
    """
    other = FastAPI(version="9.9.9-not-ours")
    assert other.openapi()["info"]["version"] == "9.9.9-not-ours"
    assert other.version != __version__


def test_no_module_carries_its_own_version_literal():
    """`__init__.py` holds the only version literal in src/.

    This is what stops the fix regressing: re-adding `version="0.2.0"` to main.py, or a
    literal in any new module, fails here even though the app would still start.
    """
    pattern = re.compile(r"""version\s*=\s*["']\d+\.\d+\.\d+""")
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "version literal outside __init__.py:\n" + "\n".join(offenders)


def test_the_literal_scan_is_not_vacuous():
    """CONTROL: the scan above passes trivially if it never reads any file, or if the
    pattern cannot match a realistic literal."""
    modules = [p for p in SRC.rglob("*.py") if p.name != "__init__.py"]
    assert len(modules) > 5, f"only found {len(modules)} modules to scan"
    pattern = re.compile(r"""version\s*=\s*["']\d+\.\d+\.\d+""")
    assert pattern.search('    version="0.2.0",')
    assert pattern.search("app = FastAPI(version='1.2.3')")
