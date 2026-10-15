"""Count and age bounds for retained cron run evidence."""

import os
import time

from cron.jobs import _prune_job_output


def _seed(directory, count):
    directory.mkdir(parents=True, exist_ok=True)
    names = [f"2026-06-25_10-00-{i:02d}.md" for i in range(count)]
    for name in names:
        (directory / name).write_text("x", encoding="utf-8")
    return names


def test_prune_keeps_newest_n(tmp_path):
    directory = tmp_path / "job"
    names = _seed(directory, 10)
    assert _prune_job_output(directory, keep=3) == 7
    assert sorted(p.name for p in directory.glob("*.md")) == names[-3:]


def test_prune_expires_old_outputs_even_below_count_cap(tmp_path):
    directory = tmp_path / "job"
    names = _seed(directory, 2)
    old = directory / names[0]
    old_time = time.time() - 49 * 3600
    os.utime(old, (old_time, old_time))
    assert _prune_job_output(directory, keep=10, retention_hours=48) == 1
    assert [p.name for p in directory.glob("*.md")] == [names[1]]


def test_nonpositive_count_preserves_external_cleanup_opt_out(tmp_path):
    directory = tmp_path / "job"
    names = _seed(directory, 2)
    old_time = time.time() - 49 * 3600
    os.utime(directory / names[0], (old_time, old_time))
    assert _prune_job_output(directory, keep=0, retention_hours=48) == 0
    assert sorted(p.name for p in directory.glob("*.md")) == names
