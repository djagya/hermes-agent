"""Fork code-health ratchet: upstream sync merges split a tracking PR into fork segments."""
import subprocess

from scripts.ci import fork_health_ratchet as ratchet
from scripts.ci.fork_health_ratchet import Segment

UPSTREAM = {"u1", "u2"}


def _plan(line, on_base=()):
    return ratchet.plan(line, UPSTREAM.__contains__, set(on_base).__contains__)


def test_annotated_tags_count_as_their_peeled_commit():
    out = ("aaa\trefs/tags/v1\n"
           "tagobj\trefs/tags/v2\n"
           "ccc\trefs/tags/v2^{}\n")
    assert ratchet.parse_tag_commits(out) == {"aaa", "ccc"}


def test_without_a_sync_merge_the_stock_check_runs():
    line = [("c3", ["c2", "topic"]), ("c2", ["c1"]), ("c1", ["b"])]
    assert _plan(line) is None


def test_fork_work_after_the_newest_sync_blocks_and_older_work_is_advisory():
    line = [
        ("c5", ["c4"]),
        ("c4", ["c3", "topic"]),  # a fork topic PR merged after the import
        ("c3", ["c2", "u1"]),     # sync merge: second parent is an upstream release
        ("c2", ["c1"]),
        ("c1", ["b"]),
    ]
    assert _plan(line) == [Segment("b", "c2", False), Segment("c3", "c5", True)]


def test_a_base_branch_merge_is_skipped_without_ending_the_blocking_side():
    line = [
        ("c6", ["c5"]),
        ("c5", ["c4", "main2"]),  # main merged into the branch after the import
        ("c4", ["c3"]),
        ("c3", ["c2", "u1"]),
        ("c2", ["b"]),
    ]
    assert _plan(line, on_base={"main2"}) == [
        Segment("b", "c2", False), Segment("c3", "c4", True), Segment("c5", "c6", True)]


def test_only_the_newest_import_counts_as_the_blocking_boundary():
    line = [
        ("c4", ["c3"]),
        ("c3", ["c2", "u2"]),
        ("c2", ["c1"]),
        ("c1", ["c0", "u1"]),
        ("c0", ["b"]),
    ]
    assert _plan(line) == [
        Segment("b", "c0", False), Segment("c1", "c2", False), Segment("c3", "c4", True)]


def test_a_head_that_is_the_sync_merge_has_nothing_blocking():
    line = [("c2", ["c1", "u1"]), ("c1", ["b"])]
    assert _plan(line) == [Segment("b", "c1", False)]


def test_first_parent_line_stops_at_the_base_and_keeps_merge_parents(tmp_path):
    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", *args], cwd=tmp_path, text=True).strip()

    git("init", "-b", "main")
    git("commit", "--allow-empty", "-m", "common")
    git("switch", "-c", "upstream")
    git("commit", "--allow-empty", "-m", "upstream release")
    release = git("rev-parse", "HEAD")
    git("switch", "main")
    base = git("rev-parse", "HEAD")
    git("switch", "-c", "track")
    git("commit", "--allow-empty", "-m", "fork before import")
    before = git("rev-parse", "HEAD")
    git("merge", "--no-ff", "upstream", "-m", "merge: upstream release")
    sync = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "fork fix on the import")
    head = git("rev-parse", "HEAD")

    line = ratchet.first_parent_line(tmp_path, base, head)
    assert [c for c, _ in line] == [head, sync, before]
    assert line[1][1] == [before, release]
    assert ratchet.plan(line, {release}.__contains__, lambda _c: False) == [
        Segment(base, before, False), Segment(sync, head, True)]
