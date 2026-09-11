"""The release workflow must verify every architecture it publishes.

WHY THIS IS A TEST AND NOT A COMMENT

The 2026-09-11 security audit found (Finding 1, Medium) that release.yml scanned and
smoke-tested an amd64-only build and then pushed a separate multi-arch image, so the
arm64 layers shipped having met neither gate — under a provenance attestation implying
they had. The fix makes verification a matrix and gates `publish` behind it.

That fix has a weak point: it holds only while the verify matrix and the publish
`platforms:` list say the same thing. Adding `linux/arm/v7` to one and not the other
silently restores the exact hole that was just closed, and nothing about the workflow
would look wrong — a release would go green with an unverified architecture in the
manifest.

A comment saying "these must match" documents the invariant. This enforces it, in the
ordinary test suite, with no Docker and no registry access.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def workflow() -> dict:
    assert WORKFLOW.is_file(), f"{WORKFLOW} is missing"
    return yaml.safe_load(WORKFLOW.read_text())


def _verify_platforms(wf: dict) -> set[str]:
    return set(wf["jobs"]["verify"]["strategy"]["matrix"]["platform"])


def _publish_platforms(wf: dict) -> set[str]:
    steps = wf["jobs"]["publish"]["steps"]
    push = next(s for s in steps if s.get("id") == "push")
    return {p.strip() for p in push["with"]["platforms"].split(",") if p.strip()}


def test_every_published_platform_is_verified(workflow):
    published = _publish_platforms(workflow)
    verified = _verify_platforms(workflow)
    unverified = published - verified
    assert not unverified, (
        f"release.yml publishes {sorted(unverified)} without verifying it. Every platform "
        "in the publish step's `platforms:` must appear in the verify matrix, or it ships "
        "having met neither Trivy nor the smoke test — with a provenance attestation "
        "implying otherwise. This is audit finding 1 of ollama-queue-proxy-fleet-standard-2026-09."
    )


def test_no_platform_is_verified_without_being_published(workflow):
    """The other direction. Not a security hole, but it burns several minutes of
    emulated build time per release on an image nobody receives, and it means the
    two lists have drifted — which is the condition the test above exists to catch."""
    stray = _verify_platforms(workflow) - _publish_platforms(workflow)
    assert not stray, f"release.yml verifies {sorted(stray)} but never publishes it"


def test_the_check_is_not_vacuous(workflow):
    """CONTROL. If either list parsed as empty, both assertions above would pass
    trivially — an empty set is a subset of everything. That failure mode is exactly
    what a YAML restructure would cause, and it would look like a clean run."""
    assert len(_publish_platforms(workflow)) >= 2, "expected a genuine multi-arch publish list"
    assert len(_verify_platforms(workflow)) >= 2, "expected a genuine multi-arch verify matrix"


def test_publish_is_gated_on_verify(workflow):
    """Matching platform lists are worthless if publish can run anyway. `needs:` is
    what makes the verify jobs a gate rather than two jobs running alongside the push."""
    needs = workflow["jobs"]["publish"]["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "verify" in needs, "publish must not be reachable without verify succeeding"


def test_verify_stops_on_the_first_failing_platform(workflow):
    """With fail-fast disabled, a failing arm64 job would not stop an in-flight amd64
    job — but more importantly the intent here is that ANY architecture failing kills
    the release, so the strict setting is the one that matches the invariant."""
    assert workflow["jobs"]["verify"]["strategy"]["fail-fast"] is True


def test_scan_and_smoke_test_both_run_in_the_verify_job(workflow):
    """Guards against the gates being moved out of the matrix at some later date,
    which would reintroduce the finding while leaving the matrix itself intact."""
    steps = workflow["jobs"]["verify"]["steps"]
    uses = " ".join(s.get("uses", "") for s in steps)
    names = " ".join(s.get("name", "") for s in steps)
    assert "trivy-action" in uses, "the vulnerability scan must run per-platform"
    assert "Smoke test" in names, "the smoke test must run per-platform"
