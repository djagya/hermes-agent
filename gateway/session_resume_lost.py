"""dlz: a person's chat turn cut off by a restart resumes inside the session authority (D02-B).

Upstream's authority never replays started work: boot fences a lost turn ``unknown`` and pauses the
chat until the person sends /discard (and the request is gone), and a turn the drain interrupted
settles ``interrupted`` with nothing resuming it. The fork's ``gateway.resume_interrupted: continue``
(PR #149) promised the opposite: after a restart Sera carries on with the request. This module is
that promise on the authority's own FIFO, for the same sessions the legacy auto-resume picks
(``resume_pending``, fresh, owner still authorized, adapter connected; see
``GatewayRunner._schedule_resume_pending_sessions``):

* an ``unknown`` head goes back to ``queued`` in place (its FIFO position, payload and media are
  kept, so followers saved during the restart still run after it);
* a turn the drain settled ``interrupted`` (nothing pending behind it) is re-admitted once under
  ``dlz-resume:<reason>:<admission id>``, text only (its media were released at settlement).

Either way the turn runs with ``run_resume``'s continue note quoting the original request
(``resumed_turn``, read by ``TurnRunner._prepare_turn_message``). Only person-owned native
(messaging) inputs from the previous owner epoch qualify, once per request: a resumed turn that is
cut off again pauses as upstream does. Same duplicate-side-effect risk as the legacy resume: Sera
sees the partial transcript and the note. ``resume_interrupted: ask`` keeps upstream's pause.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar

logger = logging.getLogger(__name__)

RESUME_META_PREFIX = 'dlz.resume_lost.v1.'
RESUME_REQUEST_PREFIX = 'dlz-resume:'
# The continue note for the admission the drain is executing now (None: an ordinary turn).
resumed_turn: ContextVar = ContextVar('dlz_resumed_turn', default=None)


def _resumable(row, epoch) -> bool:
    from gateway.session_unattended_unknown import is_unattended
    native = row['payload'].get('native_text_v1')
    return (isinstance(native, dict) and 'automation' not in native and 'webhook_delivery' not in native
            and row['intent'] == 'queue' and not is_unattended(row) and row['owner_epoch'] == epoch - 1
            and not str(row['request_id']).startswith(RESUME_REQUEST_PREFIX))


def resume_lost_turn(db, *, epoch: int, session_ids, reason: str) -> str | None:
    """Put the previous owner's lost turn of this conversation back on its FIFO; the admission id
    it now runs as, or None when nothing qualifies (upstream's pause/settlement then stands)."""
    from hermes_state_runtime import (
        RuntimeStoreError, _epoch, _retire_admission_workers, _row, admit_session_input)
    from hermes_state_runtime_workers import worker_states
    ids = tuple(dict.fromkeys(sid for sid in session_ids if sid))
    if not ids:
        return None
    marks = ','.join('?' * len(ids))

    def requeue(conn):
        _epoch(conn, epoch)
        raws = conn.execute(f"SELECT * FROM session_admissions WHERE target_session_id IN ({marks}) "
                            "AND status IN ('queued','started','unknown') ORDER BY seq", ids).fetchall()
        lost = [raw for raw in raws if raw['status'] == 'unknown']
        if not raws:
            return 'idle'
        if len(lost) != 1 or any(raw['status'] == 'started' for raw in raws):
            return None
        raw = lost[0]
        key = RESUME_META_PREFIX + raw['admission_id']
        if not _resumable(_row(raw), epoch) or conn.execute(
                'SELECT 1 FROM state_meta WHERE key=?', (key,)).fetchone():
            return None
        _retire_admission_workers(conn, raw, raw['owner_epoch'])
        if 'unknown' in worker_states(conn, raw['target_session_id']):
            raise RuntimeStoreError('unknown_execution')  # someone else's lost work: roll back, keep the pause
        conn.execute("UPDATE session_admissions SET status='queued', owner_epoch=?, generation=NULL "
                     "WHERE admission_id=? AND status='unknown'", (epoch, raw['admission_id']))
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, json.dumps({'reason': reason})))
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?',
                     (raw['target_session_id'],))
        return raw['admission_id']

    try:
        outcome = db._execute_write(requeue)
    except RuntimeStoreError as exc:
        logger.info('Lost turn on %s left paused: %s', ids[0], exc.reason)
        return None
    if outcome != 'idle':
        return outcome
    # Nothing pending: the drain settled the cut-off turn ``interrupted`` (or it finished).
    with db._read_ctx() as conn:
        last = conn.execute(f"SELECT * FROM session_admissions WHERE target_session_id IN ({marks}) "
                            "ORDER BY seq DESC LIMIT 1", ids).fetchone()
        last = _row(last) if last is not None else None
    if last is None or last['status'] != 'terminal' or last['outcome'] != 'interrupted' or not _resumable(last, epoch):
        return None
    payload = json.loads(json.dumps(last['payload']))
    payload['native_text_v1']['media'] = []  # released at settlement; the transcript keeps their hints
    try:
        row = admit_session_input(db, epoch=epoch, principal_id=last['principal_id'],
                                  session_id=last['target_session_id'],
                                  request_id=f"{RESUME_REQUEST_PREFIX}{reason}:{last['admission_id']}",
                                  payload=payload)
    except RuntimeStoreError as exc:
        logger.info('Interrupted turn on %s not resumed: %s', ids[0], exc.reason)
        return None
    return row['admission_id'] if row['status'] == 'queued' else None


def resume_note(db, row) -> str | None:
    """The continue note for an admission this module put back, else None."""
    request_id = str(row.get('request_id') or '')
    reason = None
    if request_id.startswith(RESUME_REQUEST_PREFIX):
        reason = request_id[len(RESUME_REQUEST_PREFIX):].rsplit(':', 1)[0]
    else:
        stored = db._read_one('SELECT value FROM state_meta WHERE key=?', (RESUME_META_PREFIX + row['admission_id'],))
        if stored is None:
            return None
        reason = json.loads(stored[0]).get('reason')
    from gateway.run_resume import build_resume_recovery_note, interrupted_request_text
    original = interrupted_request_text([{'role': 'user', 'content': row['payload'].get('text') or ''}])
    return build_resume_recovery_note(reason, '', interactive=False, original_request=original)


def resume_held_session(runner, authorities, entry) -> bool:
    """``_schedule_resume_pending_sessions`` for a conversation the authority holds: requeue its
    lost turn when ``resume_interrupted: continue``. True when one was put back (the boot's native
    recovery then drains it)."""
    if getattr(getattr(runner, 'config', None), 'resume_interrupted', 'continue') != 'continue':
        return False
    if len(authorities) != 1:
        return False  # two stores claim the conversation: fail closed
    authority = authorities[0]
    from gateway.session_authorities import admission_owner
    try:
        admission_id = resume_lost_turn(
            authority.db, epoch=authority.epoch, reason=entry.resume_reason or 'restart_timeout',
            session_ids=(entry.session_id, admission_owner(authority, entry.session_id)))
    except Exception:
        logger.warning('Resume of the lost turn on %s failed; upstream pause stands', entry.session_key, exc_info=True)
        return False
    if admission_id:
        logger.info('Resuming lost turn %s on %s (resume_interrupted: continue)', admission_id, entry.session_key)
    return bool(admission_id)
