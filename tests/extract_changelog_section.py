"""Extract one version's section from CHANGELOG.md, for the release workflow's body.

Invoked as `python tests/extract_changelog_section.py <version> [changelog]`, printing
the section to stdout. Lives in tests/ so that CI lints and tests it — the same
arrangement as check_gitleaks_gate.py, which is also a workflow helper rather than a
test module.

WHY A SCRIPT RATHER THAN A FEW LINES OF SHELL IN THE WORKFLOW

The release path cannot be exercised before a tag is cut, so everything about it is
asserted rather than observed until the moment it matters. Shell inlined in a YAML step
is testable only by reading it. A script is importable, so the extraction that runs at
tag time is the extraction the test suite runs against the real CHANGELOG.md on every
push — which turns "we believe the notes will be found" into something CI checks.

It EXITS NON-ZERO on a missing or empty section rather than printing nothing. A release
whose body is silently empty is the same class of failure as the one this build exists
to fix: the tag ships, the artefact is fine, and the thing a human reads is absent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


# Matches the heading for `version`, then everything up to the next `## [` heading or
# end of file. `^## \[` is anchored per-line so a "## [" appearing inside a code block
# body would end the section early — no entry in this file does that, and an
# over-eager terminator truncates notes rather than inventing them.
def extract(text: str, version: str) -> str:
    pattern = rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)"
    match = re.search(pattern, text, re.S | re.M)
    if match is None:
        raise LookupError(f"CHANGELOG.md has no '## [{version}]' section")
    body = match.group(1).strip()
    if not body:
        raise LookupError(f"CHANGELOG.md section '## [{version}]' is empty")
    return body


def main(argv: list[str]) -> int:
    if not 2 <= len(argv) <= 3:
        print(f"usage: {argv[0]} <version> [changelog-path]", file=sys.stderr)
        return 2
    version = argv[1].lstrip("v")
    path = Path(argv[2]) if len(argv) == 3 else Path("CHANGELOG.md")
    try:
        print(extract(path.read_text(encoding="utf-8"), version))
    except (LookupError, OSError) as exc:
        # `::error::` renders in the Actions log and on the job summary.
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
