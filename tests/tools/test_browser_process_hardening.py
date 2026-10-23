"""The harness interpreter gets the undumpable sitecustomize first on PYTHONPATH (Linux only)."""

import os

from tools import browser_process_hardening as hardening


def test_linux_harness_env_gets_the_hardening_site_first(monkeypatch):
    monkeypatch.setattr(hardening, "_linux", lambda: True)
    env = {"PYTHONPATH": "/site"}
    hardening.harden_harness_env(env)
    first, rest = env["PYTHONPATH"].split(os.pathsep, 1)
    assert rest == "/site"
    source = open(os.path.join(first, "sitecustomize.py"), encoding="utf-8-sig").read()
    assert "prctl(4, 0" in source
    compile(source, "sitecustomize.py", "exec")


def test_other_platforms_are_left_alone(monkeypatch):
    monkeypatch.setattr(hardening, "_linux", lambda: False)
    env = {"PYTHONPATH": "/site"}
    hardening.harden_harness_env(env)
    assert env == {"PYTHONPATH": "/site"}
    assert hardening.make_undumpable() is False
