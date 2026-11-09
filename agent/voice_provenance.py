"""dlz: native voice provenance (1180cfa066) through compaction and gateway replay.

``display_metadata.inbound_voice`` is the producer-owned binding on a voice turn's own row; Morning
Pages binds audio by it and matches its ``platform_message_id`` against the row's. A compaction
carrier that absorbs an unfinished voice request keeps that request's binding under its own key, so
it never mixes with a carrier row's own binding, and a later restatement rebuilt from the carrier
gets it back. Both keys are durable-only (``display_metadata`` never reaches the wire).
"""
from __future__ import annotations

import copy
from typing import Any, Mapping, MutableMapping, Optional

INBOUND_VOICE = "inbound_voice"
INFLIGHT_INBOUND_VOICE = "compaction_inflight_inbound_voice"


def _binding(msg: Any, key: str) -> Optional[dict]:
    metadata = msg.get("display_metadata") if isinstance(msg, Mapping) else None
    voice = metadata.get(key) if isinstance(metadata, Mapping) else None
    return copy.deepcopy(voice) if isinstance(voice, dict) else None


def _set_metadata_key(msg: MutableMapping[str, Any], key: str, value: Optional[dict]) -> None:
    metadata = msg.get("display_metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    if value is None:
        if key not in metadata:
            return
        metadata.pop(key)
    else:
        metadata[key] = value
    if metadata:
        msg["display_metadata"] = metadata
    else:
        msg.pop("display_metadata", None)


def fold_inflight_voice(carrier: MutableMapping[str, Any], inflight: Mapping[str, Any], *,
                        inflight_is_carrier: bool) -> None:
    """The carrier absorbed *inflight*'s request: record that request's binding, or clear a stale one."""
    key = INFLIGHT_INBOUND_VOICE if inflight_is_carrier else INBOUND_VOICE
    _set_metadata_key(carrier, INFLIGHT_INBOUND_VOICE, _binding(inflight, key))


def restate_inflight_voice(replay: MutableMapping[str, Any], carrier: Mapping[str, Any]) -> None:
    """A plain restatement rebuilt from a merged carrier gets the absorbed request's binding back."""
    voice = _binding(carrier, INFLIGHT_INBOUND_VOICE)
    if voice is None:
        return
    _set_metadata_key(replay, INBOUND_VOICE, voice)
    if voice.get("platform_message_id"):
        replay["platform_message_id"] = str(voice["platform_message_id"])


def voice_replay_fields(msg: Mapping[str, Any]) -> dict:
    """The voice provenance a gateway replay entry keeps from its stored row (else ``{}``)."""
    kept = {key: voice for key in (INBOUND_VOICE, INFLIGHT_INBOUND_VOICE)
            if (voice := _binding(msg, key)) is not None}
    if not kept:
        return {}
    fields: dict = {"display_metadata": kept}
    if INBOUND_VOICE in kept and msg.get("platform_message_id") is not None:
        fields["platform_message_id"] = msg["platform_message_id"]
    return fields
