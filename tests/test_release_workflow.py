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

import re
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


# ---------------------------------------------------------------------------
# The GitHub Release itself (vikunja#789)
# ---------------------------------------------------------------------------
#
# All eight releases before 0.5.0 were hand-made: the workflow published the image and
# stopped. The failure was invisible in the way that lets a bug survive eight
# repetitions — the tag existed, the image was correct, and only the thing a human reads
# was missing. These assertions are the structural half; the extraction itself is
# exercised for real further down.


def _publish_steps(wf: dict) -> list[dict]:
    return wf["jobs"]["publish"]["steps"]


def _step_names(wf: dict) -> list[str]:
    return [s.get("name", "") for s in _publish_steps(wf)]


def _release_step(wf: dict) -> dict:
    for step in _publish_steps(wf):
        if "action-gh-release" in str(step.get("uses", "")):
            return step
    raise AssertionError(
        "release.yml has no release-creation step. Publishing the image without "
        "creating the Release is vikunja#789, the bug this job exists to have fixed."
    )


def test_publish_creates_a_github_release(workflow):
    assert _release_step(workflow)


def test_publish_can_write_contents(workflow):
    """Creating a Release needs `contents: write`, and the job declared `read`. Without
    this the step fails at tag time — the one moment the path is exercised."""
    assert workflow["jobs"]["publish"]["permissions"]["contents"] == "write"


def test_the_workflow_default_stays_read_only(workflow):
    """The write is raised on the publish job ALONE. A workflow-level `contents: write`
    would hand it to the verify matrix too, which builds untrusted-tag code and has no
    business writing to the repository."""
    assert workflow["permissions"]["contents"] == "read"


def test_the_release_is_created_after_the_attestation(workflow):
    """Ordering is the gate. Created earlier, a failed attestation would leave a
    published Release advertising an image nothing signed — and the Release would
    still be there afterwards."""
    names = _step_names(workflow)
    attest = next(i for i, n in enumerate(names) if "Attest" in n)
    release = next(
        i
        for i, s in enumerate(_publish_steps(workflow))
        if "action-gh-release" in str(s.get("uses", ""))
    )
    assert release > attest, f"release step at {release} must follow attestation at {attest}"


def test_no_publish_step_tolerates_its_own_failure(workflow):
    """A tag that publishes a container and silently skips the Release is the bug being
    fixed. `continue-on-error` would reproduce it with extra steps."""
    for step in _publish_steps(workflow):
        assert not step.get("continue-on-error"), step.get("name")


def test_release_notes_come_from_the_changelog_not_from_commits(workflow):
    """This repo's CHANGELOG entries are written deliberately and say why each change
    was made. `generate_release_notes: true` would replace them with a commit list."""
    step = _release_step(workflow)
    assert not step.get("with", {}).get("generate_release_notes")
    assert "body_path" in step.get("with", {})


def test_every_action_in_the_workflow_is_pinned_to_a_commit_sha(workflow):
    """A moving tag on a third-party action is a supply-chain write into a job that
    holds `contents: write` and `packages: write`."""
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            uses = step.get("uses")
            if not uses:
                continue
            ref = uses.split("@", 1)[1]
            assert re.fullmatch(r"[0-9a-f]{40}", ref), f"{uses} is not pinned to a commit SHA"


def test_the_pinning_check_is_not_vacuous(workflow):
    """CONTROL: the loop above passes trivially if it never finds a `uses:` step."""
    seen = [s["uses"] for j in workflow["jobs"].values() for s in j["steps"] if s.get("uses")]
    assert len(seen) >= 5, seen


# ---------------------------------------------------------------------------
# The extraction, run for real rather than described
# ---------------------------------------------------------------------------
#
# The release path cannot be exercised before a tag is cut, so the plan's own note says
# a structural assertion is the best available proxy. It is not quite: the extraction is
# a committed script, so the code that runs at tag time can be imported and run here,
# against the real CHANGELOG.md, on every push.


def _extract():
    import importlib.util

    script = Path(__file__).resolve().parent / "extract_changelog_section.py"
    assert script.is_file(), f"{script} is missing"
    spec = importlib.util.spec_from_file_location("_extract_changelog", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_workflow_invokes_the_script_that_exists(workflow):
    """Binds the YAML to the file. Renaming one without the other fails only at tag
    time, which is the failure mode this whole file exists to move earlier."""
    run_steps = " ".join(s.get("run", "") for s in _publish_steps(workflow))
    assert "tests/extract_changelog_section.py" in run_steps
    assert (Path(__file__).resolve().parent / "extract_changelog_section.py").is_file()


def test_the_current_version_has_a_changelog_section():
    """THE assertion that stops the release step failing at tag time. Read from
    pyproject.toml rather than importlib.metadata, which reports whatever was last
    installed into the environment and can be stale."""
    import tomllib

    root = Path(__file__).resolve().parent.parent
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    body = _extract().extract((root / "CHANGELOG.md").read_text(), version)
    assert body.strip(), f"CHANGELOG.md has no usable section for version {version}"


def test_a_section_stops_at_the_next_version():
    text = "## [Unreleased]\n\n## [2.0.0]\n\nsecond\n\n## [1.0.0]\n\nfirst\n"
    assert _extract().extract(text, "2.0.0") == "second"
    assert _extract().extract(text, "1.0.0") == "first"


def test_the_last_section_runs_to_end_of_file():
    """The oldest entry has no following heading to stop at."""
    assert _extract().extract("## [1.0.0]\n\nonly\n", "1.0.0") == "only"


def test_a_dated_heading_is_matched():
    """Every real entry in this CHANGELOG carries a date after the version."""
    assert _extract().extract("## [1.2.3] - 2026-01-01\n\nbody\n", "1.2.3") == "body"


def test_a_missing_version_is_an_error_rather_than_empty_notes():
    """A Release whose body is silently empty is the same class of failure as no
    Release at all: the tag ships and the thing a human reads is absent."""
    with pytest.raises(LookupError, match=r"no '## \[9.9.9\]'"):
        _extract().extract("## [1.0.0]\n\nbody\n", "9.9.9")


def test_an_empty_section_is_an_error():
    with pytest.raises(LookupError, match="empty"):
        _extract().extract("## [1.0.0]\n\n## [0.9.0]\n\nbody\n", "1.0.0")


def test_a_version_prefix_does_not_match_a_longer_version():
    """`1.0.0` must not match `1.0.0-rc1`, and the regex escapes the dots so `1.0.0`
    cannot match `1X0X0`."""
    with pytest.raises(LookupError):
        _extract().extract("## [1.0.0-rc1]\n\nbody\n", "1.0.0")


def test_the_cli_strips_a_leading_v(tmp_path):
    """The workflow passes GITHUB_REF_NAME verbatim, which is `v0.5.0` for a tag push,
    while the CHANGELOG heading is `## [0.5.0]`."""
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [1.0.0]\n\nbody\n")
    assert _extract().main(["prog", "v1.0.0", str(cl)]) == 0


def test_the_cli_exits_non_zero_on_a_missing_section(tmp_path, capsys):
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("## [1.0.0]\n\nbody\n")
    assert _extract().main(["prog", "v2.0.0", str(cl)]) == 1
    assert "::error::" in capsys.readouterr().err
