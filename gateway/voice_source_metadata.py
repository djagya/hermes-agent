"""Retain inbound voice identity in durable transcript metadata, not LLM prose.

Only native voice messages are in this contract. Audio documents remain out of
scope. Multiple clips are retained as a list; consumers must reject ambiguity.
"""
from pathlib import Path


def inbound_voice_metadata(event):
    if str(getattr(getattr(event, 'message_type', None), 'value',
                   getattr(event, 'message_type', ''))).lower() != 'voice':
        return {}
    message_id = getattr(event, 'message_id', None)
    paths = getattr(event, 'media_urls', None) or []
    if not message_id or not paths:
        return {}
    # Never promote remote URLs or model-authored paths into this binding.
    if not all(isinstance(p, str) and Path(p).is_absolute() for p in paths):
        return {}
    return {'inbound_voice': {'schema': 1,
                             'platform_message_id': str(message_id),
                             'audio_paths': list(dict.fromkeys(paths))}}


def user_row_display_metadata(event, owner=None):
    """Failed-turn user row metadata: the voice binding plus the input owner, if any."""
    metadata = inbound_voice_metadata(event)
    if owner:
        metadata['gateway_input_owner'] = owner
    return metadata
