"""Runtime smoke tests for Docker --user flag guard.

Build the real image and verify the actual runtime behavior:

  1. docker run --user <arbitrary-uid> is rejected with actionable guidance
  2. --user <hermes-uid> (10000) is allowed (supported non-root start)

Root start (the default) is covered by test_main_invocation.py.

Every container is named by the ``container_name`` fixture, which ``docker rm -f``s it on teardown:
a run that outlives its timeout only loses the ``docker run`` client (subprocess.run kills that), the
container itself keeps running, and a non-root start that hangs in s6 ``rc.init`` stayed up for days.
"""
from __future__ import annotations

import subprocess


def test_arbitrary_user_uid_rejected(
    built_image: str,
    container_name: str,
) -> None:
    """docker run --user 1000 must be rejected with actionable guidance."""
    r = subprocess.run(
        ["docker", "run", "--rm", "--name", container_name, "--user", "1000:1000",
         built_image, "echo", "should_not_reach"],
        capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert r.returncode != 0, (
        f"container started with arbitrary --user UID unexpectedly: {r.stdout}"
    )
    assert "should_not_reach" not in r.stdout, (
        f"container ran despite --user rejection: {r.stdout}"
    )
    combined = r.stdout + r.stderr
    # Must mention the remediation env vars
    assert "HERMES_UID" in combined or "PUID" in combined, (
        f"rejection message missing remediation guidance: {combined[-500:]}"
    )




def test_user_pinned_to_hermes_uid_works(
    built_image: str,
    container_name: str,
) -> None:
    """docker run --user 10000:10000 (the hermes UID) must be allowed.

    This is the supported non-root start from #34648 / #34837.
    """
    r = subprocess.run(
        ["docker", "run", "--rm", "--name", container_name, "--user", "10000:10000",
         built_image, "sh", "-c", "echo OK"],
        capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert r.returncode == 0, (
        f"--user 10000:10000 (hermes UID) was rejected: {r.stderr[-500:]}"
    )
    assert "OK" in r.stdout


def test_user_bundle_lives_in_user_bundles_d(built_image: str, container_name: str) -> None:
    """s6-overlay 3.2.3.2 reads the user bundle from user-bundles.d.

    A leftover /etc/s6-overlay/s6-rc.d/user makes rc.init write user/type,
    which fails for a non-root --user start and hangs the container.  Inspect
    the static image (entrypoint bypassed) so the check does not depend on boot.
    """
    r = subprocess.run(
        ["docker", "run", "--rm", "--name", container_name, "--entrypoint", "sh", built_image, "-c",
         "test ! -e /etc/s6-overlay/s6-rc.d/user "
         "&& ls /etc/s6-overlay/user-bundles.d/user/contents.d"],
        capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert r.returncode == 0, (
        f"s6-rc.d/user exists or user bundle missing: {r.stdout} {r.stderr[-500:]}"
    )
    assert sorted(r.stdout.split()) == ["dashboard", "main-hermes", "profile-gateways"]
