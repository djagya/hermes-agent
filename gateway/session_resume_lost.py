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
* a turn the drain settled ``interrupted`` is re-admitted under ``dlz-resume:<reason>:<admission id>``
  ahead of the inputs saved behind it, text only (its media were released at settlement), and only
  when its stored result names its own gateway's restart/shutdown as the cause (``owner_stopped``):
  a person's /stop or cancel is never resumed.

Either way the turn runs with ``run_resume``'s continue note quoting the original request
(``resumed_turn``, read by ``TurnRunner._prepare_turn_message``). Only person-owned native
(messaging) inputs from the previous owner epoch qualify, once per request on both paths (one
``state_meta`` marker written in the same transaction): a resumed turn cut off again gets upstream's
pause or settlement. Boot runs the store write off the owner loop (``resume_held_sessions``).
Same duplicate-side-effect risk as the legacy resume: Sera sees the partial transcript and the note.
``resume_interrupted: ask`` keeps upstream's pause.
"""
from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar

logger = logging.getLogger(__name__)

RESUME_META_PREFIX = 'dlz.resume_lost.v1.'
RESUME_REQUEST_PREFIX = 'dlz-resume:'
# The continue note for the admission the drain is executing now (None: an ordinary turn).
resumed_turn: ContextVar = ContextVar('dlz_resumed_turn', default=None)


def _resumable(conn, row, epoch) -> bool:
    """A person's native messaging input from the previous owner epoch, never resumed before."""
    from gateway.session_unattended_unknown import is_unattended
    native = row['payload'].get('native_text_v1')
    return (isinstance(native, dict) and 'automation' not in native and 'webhook_delivery' not in native
            and row['intent'] == 'queue' and not is_unattended(row) and row['owner_epoch'] == epoch - 1
            and not str(row['request_id']).startswith(RESUME_REQUEST_PREFIX)
            and conn.execute('SELECT 1 FROM state_meta WHERE key=?',
                             (RESUME_META_PREFIX + row['admission_id'],)).fetchone() is None)


def _owner_stopped(conn, admission_id) -> bool:
    """The settled turn's stored result says its own gateway's restart/shutdown interrupted it:
    the persisted cause. A person's /stop or cancel stores another cause and is never resumed."""
    from gateway.session_bot_outcome import owner_stopped
    from hermes_state_terminal import RESULT_PREFIX, load_result
    saved = conn.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + admission_id,)).fetchone()
    stored = load_result(saved[0]) if saved else None
    return isinstance(stored, dict) and isinstance(stored.get('result'), dict) and owner_stopped(stored['result'])


def resume_lost_turn(db, *, epoch: int, session_ids, reason: str) -> str | None:
    """Put the previous owner's cut-off turn of this conversation back at the head of its FIFO, in one
    write transaction; the admission id it now runs as, or None when nothing qualifies (upstream's
    pause/settlement then stands). Synchronous: callers on the owner loop run it off-loop."""
    from hermes_state_keys import admission_fingerprint
    from hermes_state_runtime import (
        ADMISSION_QUEUE_MAX, RuntimeStoreError, _epoch, _json, _retire_admission_workers, _row)
    from hermes_state_runtime_workers import worker_states
    ids = tuple(dict.fromkeys(sid for sid in session_ids if sid))
    if not ids:
        return None
    marks = ','.join('?' * len(ids))

    def write(conn):
        _epoch(conn, epoch)
        pending = conn.execute(f"SELECT * FROM session_admissions WHERE target_session_id IN ({marks}) "
                               "AND status IN ('queued','started','unknown') ORDER BY seq", ids).fetchall()
        if any(raw['status'] == 'started' for raw in pending):
            return None
        lost = [raw for raw in pending if raw['status'] == 'unknown']
        if len(lost) > 1:
            return None
        if lost:  # an owner loss (crash, kill, a drain that never settled it): requeue in place
            raw = lost[0]
            if not _resumable(conn, _row(raw), epoch):
                return None
            _retire_admission_workers(conn, raw, raw['owner_epoch'])
            if 'unknown' in worker_states(conn, raw['target_session_id']):
                raise RuntimeStoreError('unknown_execution')  # someone else's lost work: keep the pause
            conn.execute("UPDATE session_admissions SET status='queued', owner_epoch=?, generation=NULL "
                         "WHERE admission_id=? AND status='unknown'", (epoch, raw['admission_id']))
            resumed = raw
            admission_id = raw['admission_id']
        else:  # the drain interrupted it: the settled turn before the inputs saved behind it
            before = pending[0]['seq'] if pending else None
            raw = conn.execute(f"SELECT * FROM session_admissions WHERE target_session_id IN ({marks}) "
                               "AND status='terminal'" + (" AND seq<?" if before is not None else "")
                               + " ORDER BY seq DESC LIMIT 1", (*ids, *([before] if before is not None else []))).fetchone()
            if (raw is None or raw['outcome'] != 'interrupted' or not _resumable(conn, _row(raw), epoch)
                    or not _owner_stopped(conn, raw['admission_id']) or len(pending) >= ADMISSION_QUEUE_MAX):
                return None
            payload = _row(raw)['payload']
            payload['native_text_v1']['media'] = []  # released at settlement; the transcript keeps their hints
            encoded = _json(payload)
            target = raw['target_session_id']
            admission_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,"
                "lineage_json,payload_json,payload_digest,intent,status,owner_epoch) VALUES(?,?,?,?,?,?,?,'queue','queued',?)",
                (admission_id, f"{RESUME_REQUEST_PREFIX}{reason}:{raw['admission_id']}", raw['principal_id'], target,
                 json.dumps([target]), encoded, admission_fingerprint(
                     canonical_target=target, payload={'input': json.loads(encoded), 'intent': 'queue'}), epoch))
            # Ahead of its saved followers: they move behind it in their own order (same rows, ids
            # and payloads; only their FIFO sequence numbers change).
            columns = [c[1] for c in conn.execute('PRAGMA table_info(session_admissions)') if c[1] != 'seq']
            listed = ','.join(columns)
            for follower in pending:
                values = [follower[c] for c in columns]
                conn.execute('DELETE FROM session_admissions WHERE seq=?', (follower['seq'],))
                conn.execute(f"INSERT INTO session_admissions({listed}) VALUES({','.join('?' * len(columns))})", values)
            resumed = raw
        # Once per request on both paths: a resumed turn cut off again keeps upstream's behaviour.
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                     (RESUME_META_PREFIX + resumed['admission_id'], json.dumps({'reason': reason, 'as': admission_id})))
        conn.execute('UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?',
                     (resumed['target_session_id'],))
        return admission_id

    try:
        return db._execute_write(write)
    except RuntimeStoreError as exc:
        logger.info('Lost turn on %s left as upstream recovered it: %s', ids[0], exc.reason)
        return None


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


def queue_held_session(runner, authorities, entry) -> bool:
    """``_schedule_resume_pending_sessions`` for a conversation the authority holds, on the boot pass:
    note it for ``resume_held_sessions`` (no store write on the loop here). True when noted."""
    if getattr(getattr(runner, 'config', None), 'resume_interrupted', 'continue') != 'continue':
        return False
    if len(authorities) != 1:
        return False  # two stores claim the conversation: fail closed
    queued = runner.__dict__.setdefault('_dlz_lost_turns', [])
    queued.append((authorities[0], entry))
    return True


async def resume_held_sessions(runner) -> int:
    """Boot, before the native recovery drains FIFOs: put each noted conversation's cut-off turn back,
    off the owner loop (a held SQLite writer must not stall every platform). Returns how many."""
    from functools import partial
    from gateway.session_authorities import admission_owner
    from gateway.session_runtime_workers import tracked_write
    noted, runner._dlz_lost_turns = list(runner.__dict__.get('_dlz_lost_turns') or ()), []
    resumed = 0

    def recover(authority, entry):
        return resume_lost_turn(authority.db, epoch=authority.epoch, reason=entry.resume_reason or 'restart_timeout',
                                session_ids=(entry.session_id, admission_owner(authority, entry.session_id)))
    for authority, entry in noted:
        try:
            admission_id = await tracked_write(authority, partial(recover, authority, entry))
        except Exception:
            logger.warning('Resume of the lost turn on %s failed; upstream recovery stands', entry.session_key,
                           exc_info=True)
            continue
        if admission_id:
            resumed += 1
            logger.info('Resuming lost turn %s on %s (resume_interrupted: continue)', admission_id, entry.session_key)
    return resumed
