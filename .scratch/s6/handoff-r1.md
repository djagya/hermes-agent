# Handoff r1: s6 user bundles -> user-bundles.d, s6-overlay 3.2.3.2

State: done, not pushed. Branch fix/s6-user-bundles-d. Commits: cherry-pick of fe666c19fb (bump, checksums unchanged) + one commit moving the bundle.

Changes
- git mv docker/s6-rc.d/user -> docker/user-bundles.d/user (contents.d/{main-hermes,dashboard,profile-gateways}).
- Dockerfile: added `COPY docker/user-bundles.d/ /etc/s6-overlay/user-bundles.d/` after the s6-rc.d COPY. The old s6-rc.d/user is no longer copied (dir deleted).
- tests/tools/test_docker_s6_tree_ownership.py: membership assertion now reads docker/user-bundles.d/user/contents.d.
- tests/docker/test_user_flag_guard.py: new test_user_bundle_lives_in_user_bundles_d (docker run --entrypoint sh: no /etc/s6-overlay/s6-rc.d/user; contents.d lists the three services). Not runnable here (no Docker); runs in fork CI.
- optional-skills SKILL.md and website docs copy: "add a service" step 4/5 updated.

Doc quotes (upstream v3.2.3.2)
- CHANGELOG: "The `user` and `user2` bundles are now defined in `/etc/s6-overlay/user-bundles.d` rather than `/etc/s6-overlay/s6-rc.d`." and "If your user bundles are still defined in `/etc/s6-overlay/user-bundles.d` [sic, means s6-rc.d], then the container will still boot, printing a warning message."
- README: "create an empty file with the same name in the `/etc/s6-overlay/user-bundles.d/user/contents.d` directory"; S6_RUNTIME_BUNDLEDIR default `/etc/s6-overlay/user-bundles.d`.
- v3.2.3.0 README has zero mentions of user-bundles, so the new layout is NOT supported by 3.2.3.0 (bundle would be ignored). Not tested on the image.

Unverified: image build and tests/docker (need CI). Whether the non-root --user start now passes test_user_flag_guard is only provable in CI.
Dead ends: none.
