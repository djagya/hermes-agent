"""dlz D02-B: a person's chat turn cut off by a restart resumes on the authority's FIFO.

Real ``SessionStore`` SQLite and real owner epochs (the GW-10 tests' shape): an ``unknown`` head
goes back to ``queued`` in place, ahead of the follower saved during the restart, once; a turn the
drain settled ``interrupted`` is re-admitted text-only; both run with the continue note.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionEntry, SessionStore
from gateway.session_authority import initialize_session_authority
from gateway.session_resume_lost import RESUME_REQUEST_PREFIX, resume_lost_turn, resume_note
from hermes_state_runtime import (
    admit_session_input, claim_session_input, get_session_admission, list_session_admissions,
    settle_session_input)

PERSON = 'messaging:["telegram","dm","u1"]'


def _native(text, media=()):
    return {'text': text, 'native_text_v1': {'event': {'message_id': text}, 'media': list(media), 'route': 's'}}


def _interrupted(cause):
    """The stored result of a turn its own gateway's restart interrupted, or a person's /stop."""
    message = 'Gateway restarting' if cause == 'drain_interrupted' else 'explicit stop requested'
    return {'result': {'final_response': '', 'messages': [], 'interrupted': True, 'interrupt_message': message},
            'usage': {}}


async def _owners(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(_session_db=store._db, session_store=store, _draining=False,
                             _adapter_for_source=lambda source: None)
    store._db.create_session('s', source='telegram')

    async def owner(n):
        return await initialize_session_authority(runner, profile_id='p', instance_id=f'owner-{n}')
    return store, owner


@pytest.mark.asyncio
async def test_lost_person_turn_requeues_in_place_once_with_continue_note(tmp_path, monkeypatch):
    store, owner = await _owners(tmp_path, monkeypatch)
    try:
        first = await owner(1)
        head = admit_session_input(first.db, epoch=first.epoch, principal_id=PERSON, session_id='s',
                                   request_id='m1', payload=_native('HEAD'))['admission_id']
        follower = admit_session_input(first.db, epoch=first.epoch, principal_id=PERSON, session_id='s',
                                       request_id='m2', payload=_native('FOLLOWER'))['admission_id']
        assert claim_session_input(first.db, epoch=first.epoch, session_id='s')['admission_id'] == head
        second = await owner(2)  # the restart fenced the head unknown: upstream pauses here
        assert get_session_admission(second.db, admission_id=head)['status'] == 'unknown'

        assert resume_lost_turn(second.db, epoch=second.epoch, session_ids=['s'], reason='restart_timeout') == head
        assert [r['status'] for r in list_session_admissions(second.db, session_id='s')] == ['queued', 'queued']
        claimed = claim_session_input(second.db, epoch=second.epoch, session_id='s')
        assert claimed['admission_id'] == head  # FIFO position kept: before the follower
        note = resume_note(second.db, claimed)
        assert 'gateway restart' in note and 'CONTINUE' in note and '<interrupted_request>\nHEAD\n' in note
        assert resume_note(second.db, get_session_admission(second.db, admission_id=follower)) is None

        third = await owner(3)  # cut off again: once per request, then upstream's pause
        assert resume_lost_turn(third.db, epoch=third.epoch, session_ids=['s'], reason='restart_timeout') is None
        assert get_session_admission(third.db, admission_id=head)['status'] == 'unknown'
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('cause', ['drain_interrupted', 'person_stop'])
async def test_a_resumed_turn_that_settles_interrupted_is_never_resumed_again(tmp_path, monkeypatch, cause):
    """Crash, in-place resume, then the resumed turn is stopped by a person or by another drain
    timeout: the next boot leaves it (the once-only marker holds on the interrupted path too)."""
    store, owner = await _owners(tmp_path, monkeypatch)
    try:
        first = await owner(1)
        head = admit_session_input(first.db, epoch=first.epoch, principal_id=PERSON, session_id='s',
                                   request_id='m1', payload=_native('HEAD'))['admission_id']
        claim_session_input(first.db, epoch=first.epoch, session_id='s')
        second = await owner(2)
        assert resume_lost_turn(second.db, epoch=second.epoch, session_ids=['s'], reason='restart_timeout') == head
        claimed = claim_session_input(second.db, epoch=second.epoch, session_id='s')
        settle_session_input(second.db, epoch=second.epoch, admission_id=head, generation=claimed['generation'],
                             outcome='interrupted', result=_interrupted(cause))
        third = await owner(3)
        assert resume_lost_turn(third.db, epoch=third.epoch, session_ids=['s'], reason='restart_timeout') is None
        assert list_session_admissions(third.db, session_id='s') == []
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_drain_runs_the_resumed_head_with_the_note_then_the_follower(tmp_path, monkeypatch):
    import gateway.session_envelope as envelope
    import gateway.session_ingress as ingress
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    from gateway.session_authority import LiveSession
    from gateway.session_contract import SessionRef
    from gateway.session_resume_lost import resumed_turn
    from tests.waiting import async_wait_until

    source = SessionSource(platform=Platform.TELEGRAM, chat_id='c')
    executed = []

    async def answer(event):
        from gateway.session_results import execution_result
        executed.append((event.text, resumed_turn.get()))
        execution_result.get()['result'] = {'final_response': 'ok', 'completed': True, 'messages': []}
        return 'ok'

    async def route_ok(*_args, **_kwargs):
        return source, 's'
    # The envelope's route/auth re-check and event rebuild need a live adapter; not under test here.
    monkeypatch.setattr(envelope, 'check_native_route', route_ok)
    monkeypatch.setattr(ingress, 'restore_native', lambda payload, runner=None: MessageEvent(text=payload['text'], source=source))
    store, owner = await _owners(tmp_path, monkeypatch)
    try:
        first = await owner(1)
        first.runner._handle_message = answer
        for request_id, text in (('m1', 'HEAD'), ('m2', 'FOLLOWER')):
            admit_session_input(first.db, epoch=first.epoch, principal_id=PERSON, session_id='s',
                                request_id=request_id, payload=_native(text))
        claim_session_input(first.db, epoch=first.epoch, session_id='s')
        second = await owner(2)
        assert resume_lost_turn(second.db, epoch=second.epoch, session_ids=['s'], reason='restart_timeout')
        second.sessions['s'] = LiveSession(source, 's')
        second._schedule(SessionRef('p', 's'))
        live = second.sessions['s']
        await async_wait_until(lambda: live.task is not None and live.task.done(), what='drain finished')
        assert [text for text, _ in executed] == ['HEAD', 'FOLLOWER']
        assert '<interrupted_request>\nHEAD\n' in executed[0][1] and executed[1][1] is None
        assert [(r['status'], r['outcome']) for r in list_session_admissions(
            second.db, session_id='s', pending_only=False)] == [('terminal', 'completed')] * 2
    finally:
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('cause', ['drain_interrupted', 'person_stop'])
async def test_drain_interrupted_person_turn_is_readmitted_ahead_of_its_follower(tmp_path, monkeypatch, cause):
    store, owner = await _owners(tmp_path, monkeypatch)
    try:
        first = await owner(1)
        head = admit_session_input(first.db, epoch=first.epoch, principal_id=PERSON, session_id='s',
                                   request_id='m1', payload=_native('HEAD', [{'path': '/x', 'sha256': 'a', 'size': 1}]))
        claimed = claim_session_input(first.db, epoch=first.epoch, session_id='s')
        follower = admit_session_input(first.db, epoch=first.epoch, principal_id=PERSON, session_id='s',
                                       request_id='m2', payload=_native('FOLLOWER'))['admission_id']
        settle_session_input(first.db, epoch=first.epoch, admission_id=head['admission_id'],
                             generation=claimed['generation'], outcome='interrupted', result=_interrupted(cause))
        second = await owner(2)

        resumed = resume_lost_turn(second.db, epoch=second.epoch, session_ids=['s'], reason='shutdown_timeout')
        if cause == 'person_stop':  # a person's /stop is never resumed
            assert resumed is None
            assert [r['admission_id'] for r in list_session_admissions(second.db, session_id='s')] == [follower]
            return
        row = get_session_admission(second.db, admission_id=resumed)
        assert row['status'] == 'queued' and row['request_id'].startswith(RESUME_REQUEST_PREFIX)
        assert row['payload']['text'] == 'HEAD' and row['payload']['native_text_v1']['media'] == []
        assert 'gateway shutdown' in resume_note(second.db, row)
        assert [r['admission_id'] for r in list_session_admissions(second.db, session_id='s')] == [resumed, follower]
        assert claim_session_input(second.db, epoch=second.epoch, session_id='s')['admission_id'] == resumed
        assert resume_lost_turn(second.db, epoch=second.epoch, session_ids=['s'], reason='shutdown_timeout') is None
    finally:
        store.close_all_db_handles()


def test_turn_runner_runs_the_resumed_turn_with_the_note_not_as_a_new_message():
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_resume_lost import resumed_turn
    from gateway.turn_context import TurnContext

    turn = object.__new__(TurnRunner)
    turn._ctx = TurnContext(message='HEAD', session_key='k', history=[
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'partial'}])
    turn._runner = SimpleNamespace(session_store=SimpleNamespace(_entries={}))
    token = resumed_turn.set('[System note: The previous turn was interrupted by a gateway restart; CONTINUE]')
    try:
        persisted, _ = turn._prepare_turn_message([{'role': 'tool', 'tool_call_id': 'c1', 'content': 'partial'}])
    finally:
        resumed_turn.reset(token)
    assert turn._ctx.message == persisted and persisted.endswith('CONTINUE]')
    assert 'NEW message' not in turn._ctx.message


@pytest.mark.asyncio
async def test_boot_resume_requeues_off_the_loop_not_as_a_synthetic_turn(tmp_path, monkeypatch):
    import threading
    from gateway import session_resume_lost
    from gateway.session_resume_lost import resume_held_sessions
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    store, owner = await _owners(tmp_path, monkeypatch)
    try:
        first = await owner(1)
        head = admit_session_input(first.db, epoch=first.epoch, principal_id=PERSON, session_id='s',
                                   request_id='m1', payload=_native('HEAD'))['admission_id']
        claim_session_input(first.db, epoch=first.epoch, session_id='s')
        authority = await owner(2)
        runner, adapter = make_restart_runner()
        entry = SessionEntry(session_key='agent:main:telegram:dm:held', session_id='s', created_at=datetime.now(),
                             updated_at=datetime.now(), origin=make_restart_source(chat_id='held'),
                             platform=Platform.TELEGRAM, chat_type='dm', resume_pending=True,
                             resume_reason='restart_timeout', last_resume_marked_at=datetime.now())
        runner.session_store._entries = {entry.session_key: entry}
        adapter.handle_message = AsyncMock()
        on_loop, real = [], session_resume_lost.resume_lost_turn

        def recording(*args, **kwargs):
            on_loop.append(threading.current_thread() is threading.main_thread())
            return real(*args, **kwargs)
        monkeypatch.setattr(session_resume_lost, 'resume_lost_turn', recording)
        with patch('gateway.session_authorities.all_authorities', return_value=[authority]), \
                patch('gateway.session_authorities.holds_admission', return_value=True):
            assert runner._schedule_resume_pending_sessions() == 0  # no synthetic turn
            assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 0  # reconnect: pause
            assert on_loop == []  # the scheduling pass writes nothing on the loop
            assert await resume_held_sessions(runner) == 1
        assert on_loop == [False]
        assert get_session_admission(authority.db, admission_id=head)['status'] == 'queued'
        adapter.handle_message.assert_not_called()
    finally:
        store.close_all_db_handles()
