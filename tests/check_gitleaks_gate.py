#!/usr/bin/env python3
"""
Assert the secret-scanning gate actually fires. Behaviour, not configuration.

WHY THIS EXISTS

`.gitleaks.toml` has been in this repository since before this file, and nothing ever
ran it. The config shipped; no workflow executed it, so a public repo had a leak gate
in name only — which is what B14 was added to the fleet standard to catch.

Adding the workflow alone would not be enough. A scanner that has silently stopped
matching reports "no leaks found", and so does a genuinely clean repository: the same
green, from opposite causes. Every assertion below therefore plants a secret and
requires the gate to FIND it. The clean-repo check comes last and is only meaningful
because the planted cases established the gate can fire at all.

The custom `oqp-api-key` rule gets its own two-sided check. It is path-scoped to
`config.yml`, which is gitignored, so in a clean tree it never fires against anything —
its regex could rot indefinitely without a single green build noticing.

NOT NAMED test_*.py, DELIBERATELY. This is a standalone script with top-level side
effects, not a pytest module: collected by pytest it executes the scans at import time
and sys.exit()s mid-collection, which takes down the whole suite (verified — pytest
reports INTERNALERROR and runs no tests at all). The alternative, a conftest
collect_ignore entry, keeps the tempting `test_` prefix but adds a second list that has
to stay in step with this file's name. A name pytest never matches needs no second list.
Run it directly: `python tests/check_gitleaks_gate.py`.

NOT SKIPPABLE. If gitleaks is missing this fails rather than skipping. A gate check
that passes when it could not run reports the same thing as one that verified something.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / ".gitleaks.toml"


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def rules_fired(directory: Path) -> set[str]:
    """The set of gitleaks rule IDs that matched under `directory`.

    Returns rule IDs rather than a bare pass/fail because the two are not
    interchangeable. A file can be flagged by the default ruleset while the custom
    rule under test matched nothing — exit-code-only assertions read those as the
    same result, which is how the path-scope control below first gave a false
    negative and claimed a working rule was broken.
    """
    with tempfile.TemporaryDirectory() as report_dir:
        report = Path(report_dir) / "out.json"
        subprocess.run(
            [
                "gitleaks",
                "detect",
                "--source",
                str(directory),
                "--no-git",
                "--redact",
                "--config",
                str(CONFIG),
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ],
            capture_output=True,
            text=True,
        )
        if not report.is_file() or report.stat().st_size == 0:
            return set()
        return {f["RuleID"] for f in json.loads(report.read_text())}


def scan(directory: Path) -> bool:
    """True if gitleaks reports at least one leak, by any rule."""
    return bool(rules_fired(directory))


if shutil.which("gitleaks") is None:
    print("FATAL: gitleaks is not installed.")
    print("       This is a hard failure, not a skip. This repository is public and the")
    print("       scan is its only automated leak gate — see the module docstring.")
    sys.exit(2)

check(CONFIG.is_file(), ".gitleaks.toml exists")

# Every fixture below is ASSEMBLED AT RUNTIME rather than written as a literal, and that
# is load-bearing: spelled out in full, each one is detected by the very gate this file
# tests, so the repository scan would then fail on its own test fixtures. Splitting the
# strings breaks the rules' prefix anchors so this file scans clean, while the
# reassembled values are byte-identical at runtime and still fire. Do not "tidy" these
# back into literals.
#
# A synthetic GitHub PAT — not a real credential; accepted by the default ruleset purely
# on shape.
PAT = "ghp" + "_016C7f4d9aB2eF8c1D3a5B7e9F0a2C4d6E8f01"
# Shaped like the proxy's own API keys: the `key:` prefix plus 20+ key characters is
# what the custom oqp-api-key rule looks for.
OQP_KEY = "oqp" + "_live_7f3b9c2e4a8d6150b3e7"

print("\nthe default ruleset fires on a planted secret")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)

    # Control first. If this does not fire, every result below is meaningless — the
    # probe is broken rather than the tree being clean.
    (d / "c.txt").write_text(f"token={PAT}\n")
    check(scan(d), "CONTROL: a bare secret is detected (else the probe proves nothing)")
    (d / "c.txt").unlink()

    # On the same line as each credential name this codebase legitimately mentions.
    for name in [
        "OQP_KEY_OPENWEBUI",
        "OLLAMA_QUEUE_PROXY_TOKEN",
        "AUTHORIZATION",
    ]:
        (d / "c.txt").write_text(f"{name}={PAT}\n")
        check(scan(d), f"detected when co-located with {name}")
        (d / "c.txt").unlink()

print("\nthe custom oqp-api-key rule fires, and is correctly path-scoped")
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)

    body = f"auth:\n  keys:\n    - key: {OQP_KEY}\n      client_id: x\n"

    # Positive: a proxy API key in the file the rule targets.
    (d / "config.yml").write_text(body)
    check("oqp-api-key" in rules_fired(d), "oqp-api-key fires on a key in config.yml")

    # Negative control for the PATH SCOPE, not for the regex: identical content in a file
    # the rule does not target must not be reported BY THIS RULE. Asserted on the rule ID,
    # because the default ruleset's generic-api-key also matches this content — an
    # exit-code assertion here reports a leak either way and cannot tell a correctly
    # scoped rule from one that has silently widened to every file.
    (d / "config.yml").unlink()
    (d / "notes.txt").write_text(body)
    fired = rules_fired(d)
    check("oqp-api-key" not in fired, "oqp-api-key does NOT fire outside config.yml")
    check("generic-api-key" in fired, "CONTROL: the default ruleset still covers that file")

print("\nand the repository itself is clean")

# TRACKED FILES ONLY, and that is not a convenience. The fixtures above are assembled
# at runtime so this file's SOURCE scans clean — but CPython constant-folds
# `"ghp" + "_016C..."` at compile time and writes the joined literal into the .pyc, so
# a __pycache__ left behind by a previous run leaks exactly what the split protects.
# That is not hypothetical: it is what this check caught on its first run after pytest
# had imported this module under its old name.
#
# Scanning `git ls-files` is also the more faithful question. Untracked build spoil is
# not what is at risk of reaching a public remote; the tracked tree is. Suppressing
# .pyc in .gitleaks.toml would have worked too, but an allowlist entry silences the
# pattern in committed files as well, and this does not.
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "src", "tests", "config.example.yml"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    copied = 0
    for rel in filter(None, tracked):
        dest = d / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((REPO_ROOT / rel).read_bytes())
        copied += 1
    # A scan of an empty directory is clean, which would make the check below pass
    # while measuring nothing at all.
    check(copied > 0, f"CONTROL: tracked files were actually collected (got {copied})")
    check(not scan(d), "the tracked tree scans clean (src/, tests/, config.example.yml)")

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("all gitleaks gate checks passed")
