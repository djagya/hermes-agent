"""An OAuth server parked on an authentication failure logs "parking until credentials change".
The timed self-probe must honour that: with the same token/client files on disk it can only
replay the dead refresh token (400 at the token endpoint) and fail the non-interactive authorize
step again — every ``_PARKED_RETRY_INTERVAL``, hundreds of errors.log entries a day. A new token
file (``hermes mcp login`` from another process) or an explicit reconnect must still revive."""

import asyncio

import pytest


@pytest.mark.no_isolate
def test_auth_parked_oauth_server_probes_only_after_credentials_change(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from tools import mcp_tool
    from tools import mcp_tool_config as _config
    from tools.mcp_oauth import HermesTokenStorage, OAuthNonInteractiveError
    from tools.mcp_tool import MCPServerTask

    monkeypatch.setattr(mcp_tool, "_PARKED_RETRY_INTERVAL", 0.05)
    server_cfg = {"url": "https://mcp.example.com/mcp", "auth": "oauth"}
    monkeypatch.setattr(_config, "_load_mcp_config", lambda: {"srv": dict(server_cfg)})

    tokens = HermesTokenStorage("srv")._tokens_path()
    tokens.parent.mkdir(parents=True, exist_ok=True)
    tokens.write_text(
        '{"access_token": "A1", "token_type": "Bearer", "refresh_token": "R1"}'
    )

    state = {"transport_calls": 0, "deregistered": 0}

    async def _scenario():
        class _Task(MCPServerTask):
            def _is_http(self):
                return True

            async def _prepare_run(self, config):
                self._config = config
                self._auth_type = "oauth"
                return True

            def _deregister_tools(self):
                state["deregistered"] += 1

            async def _run_http(self, config):
                state["transport_calls"] += 1
                raise OAuthNonInteractiveError(
                    "MCP OAuth requires browser authorization"
                )

        task = _Task("srv")
        run_task = asyncio.ensure_future(task.run(dict(server_cfg)))

        async def _wait_for(pred, rounds=300):
            for _ in range(rounds):
                if pred():
                    return True
                await asyncio.sleep(0.01)
            return pred()

        assert await _wait_for(lambda: state["deregistered"] >= 1), (
            "server never parked"
        )
        assert not run_task.done(), "run task exited instead of parking"

        parked_at = state["transport_calls"]
        await asyncio.sleep(0.5)  # ~10 probe intervals
        assert state["transport_calls"] == parked_at, (
            "auth-parked self-probe retried with unchanged credentials "
            f"({state['transport_calls'] - parked_at} extra attempts)"
        )

        # Re-authorization from another process writes a new token file: the next probe revives.
        tokens.write_text(
            '{"access_token": "A2", "token_type": "Bearer", "refresh_token": "R2-new"}'
        )
        assert await _wait_for(lambda: state["transport_calls"] > parked_at), (
            "self-probe did not revive after the token file changed"
        )

        # Still failing -> re-parked against the NEW snapshot; quiet again.
        await _wait_for(lambda: state["deregistered"] >= 2)
        settled = state["transport_calls"]
        await asyncio.sleep(0.5)
        assert state["transport_calls"] == settled

        # An explicit reconnect (dashboard re-auth, manual refresh) is never gated.
        task._reconnect_event.set()
        assert await _wait_for(lambda: state["transport_calls"] > settled), (
            "explicit reconnect was gated by the credential snapshot"
        )

        task._shutdown_event.set()
        task._reconnect_event.set()
        try:
            await asyncio.wait_for(run_task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            run_task.cancel()

    asyncio.run(_scenario())
