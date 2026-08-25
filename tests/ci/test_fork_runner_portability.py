from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
LARGE_RUNNER_FALLBACKS = {
    "ubuntu-latest-96-core": "ubuntu-latest",
    "ubuntu-latest-32-core": "ubuntu-latest",
    "windows-latest-32-core": "windows-latest",
}
UPSTREAM_GUARD = "github.repository == 'NousResearch/hermes-agent'"


def test_upstream_only_large_runners_have_standard_fork_fallbacks() -> None:
    """Fork PRs must not queue forever on NousResearch-only runner labels."""

    guarded_occurrences: dict[str, int] = dict.fromkeys(LARGE_RUNNER_FALLBACKS, 0)

    workflows = sorted({*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")})
    for workflow in workflows:
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for large_runner, standard_runner in LARGE_RUNNER_FALLBACKS.items():
                if large_runner not in line:
                    continue

                guarded_occurrences[large_runner] += 1
                assert UPSTREAM_GUARD in line, (
                    f"{workflow.relative_to(ROOT)}:{line_number} uses "
                    f"{large_runner} without an upstream-repository guard"
                )
                assert f"|| '{standard_runner}'" in line, (
                    f"{workflow.relative_to(ROOT)}:{line_number} does not fall "
                    f"back from {large_runner} to {standard_runner} for forks"
                )

    assert guarded_occurrences == {
        "ubuntu-latest-96-core": 1,
        "ubuntu-latest-32-core": 6,
        "windows-latest-32-core": 1,
    }
