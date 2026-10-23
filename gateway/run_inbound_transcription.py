"""Voice-clip transcription for inbound messages (moved out of ``gateway/run_inbound.py``)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from gateway.pending_audio import PendingAudioClip, pending_audio_clips
from gateway.run_common import _UNSET

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


class GatewayInboundTranscriptionMixin:
    """STT enrichment and the once-only echo of pending voice transcripts."""

    async def _enrich_message_with_transcription(
        self, user_text: str, audio_paths: list[str], clip_memo: Optional[dict] = None
    ) -> tuple[str, list[str]]:
        """Transcribe voice clips with the configured STT provider and prepend the transcripts →
        ``(enriched_text, successful_transcripts)``; the transcripts (input order; empty if every clip
        failed or STT is disabled) let callers echo them back before the agent loop. ``clip_memo``
        (path → ``_transcribe_one_clip`` result) skips clips an earlier pass already transcribed."""
        from gateway.run import _probe_audio_duration
        audio_paths = list(dict.fromkeys(audio_paths))
        if not getattr(self.config, "stt_enabled", True):
            notes = []
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                duration_str = await _probe_audio_duration(abs_path)
                suffix = f" (duration: {duration_str})" if duration_str else ""
                notes.append(f"[The user sent a voice message: {abs_path}{suffix}]")
            return (self._prepend_media_prefix("\n\n".join(notes), user_text) if notes else user_text), []

        try:
            from tools.transcription_tools import (
                transcribe_audio, transcribe_audio_local_fallback
            )
        except ModuleNotFoundError as e:
            logger.error("Transcription module unavailable: %s", e)
            return self._prepend_media_prefix("[voice message could not be transcribed]", user_text), []

        enriched_parts = []
        successful_transcripts: list[str] = []
        for path in audio_paths:
            try:
                if clip_memo is not None and path in clip_memo:
                    transcript, note = clip_memo[path]
                else:
                    logger.debug("Transcribing user voice: %s", path)
                    transcript, note = await self._transcribe_one_clip(
                        path, transcribe_audio, transcribe_audio_local_fallback,
                    )
                    if clip_memo is not None:
                        clip_memo[path] = (transcript, note)
                if transcript is not None:
                    successful_transcripts.append(transcript)
                enriched_parts.append(note)
            except Exception as e:
                logger.error("Transcription error: %s", e)
                enriched_parts.append(self._untranscribed_audio_note(path))

        if enriched_parts:
            user_text = self._prepend_media_prefix("\n\n".join(enriched_parts), user_text)
        return user_text, successful_transcripts

    def _pending_event_audio_paths(self, event) -> list[str]:
        """Return STT-eligible paths from a pending voice message."""
        from gateway.run import _event_media_is_stt_input
        return [
            path for i, path in enumerate(getattr(event, "media_urls", None) or [])
            if _event_media_is_stt_input(event, i)
        ]

    async def _transcribe_pending_audio_event_once(
        self, event, user_text: Optional[str] = None
    ) -> tuple[str | None, list[str]]:
        """Cache pending audio per clip; merges may extend the event while STT is running.
        The interrupt monitor and pending drain share results without freezing the caption."""
        if not self._pending_event_audio_paths(event):
            return user_text if user_text is not None else (getattr(event, "text", None) or None), []
        if not hasattr(event, "_gateway_pending_stt_lock"):
            event._gateway_pending_stt_lock = asyncio.Lock()
        async with event._gateway_pending_stt_lock:
            clips = pending_audio_clips(event)
            results = {}
            while True:
                audio_paths = list(dict.fromkeys(self._pending_event_audio_paths(event)))
                missing = next((path for path in audio_paths if path not in results), None)
                if missing is None:
                    break
                clip = clips.setdefault(missing, PendingAudioClip())
                results[missing] = await clip.transcribe(
                    lambda: self._enrich_message_with_transcription("", [missing])
                )
                # A merge can append media/text during STT. Re-read the event before publishing;
                # keep completed clips so a caption merge cannot retranscribe or change an echo.
            prefix = "\n\n".join(results[path][0] for path in audio_paths)
            successful_transcripts = [
                transcript for path in audio_paths for transcript in results[path][1]
            ]
            current_text = getattr(event, "text", "") or ""
            event._gateway_pending_stt_text = (
                self._prepend_media_prefix(prefix, current_text) if prefix else current_text
            )
            event._gateway_pending_stt_transcripts = list(successful_transcripts)
            # Explicit overrides (including clarify's "") belong to this caller, never the cache.
            text = current_text if user_text is None else user_text
            return (
                self._prepend_media_prefix(prefix, text) if prefix else text,
                successful_transcripts,
            )

    async def _echo_pending_stt_transcripts_once(
        self, event, adapter, source, transcripts: list[str], *, metadata=None,
        log_context: str = "Transcript",
    ) -> None:
        """Claim echoes by clip identity, including transcripts already echoed by an absorbed event."""
        if not transcripts or not self._should_echo_stt_transcripts() or adapter is None:
            return
        clips = pending_audio_clips(event)
        unsent = [
            transcript
            for path in dict.fromkeys(self._pending_event_audio_paths(event))
            for transcript in clips[path].claim_echo()
        ]
        await self._echo_stt_transcripts(
            adapter, source, unsent, metadata=metadata, log_context=log_context,
        )

    async def _transcribe_and_echo_pending_voice(
        self, event, adapter, source, text: str, *, log_context: str, metadata=_UNSET
    ) -> tuple[str, list[str]]:
        """Transcribe a pending voice event and echo transcripts once → ``(enriched_text,
        transcripts)`` for ``agent.interrupt()`` or the pending-drain flow; ``(text, [])`` when there
        is no STT-eligible media (caller owns the ``_build_media_placeholder`` fallback)."""
        if not self._pending_event_audio_paths(event):
            return text, []
        try:
            if metadata is _UNSET:
                metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
            while True:
                if not self._pending_event_audio_paths(event):
                    return getattr(event, "text", "") or "", []
                enriched_text, transcripts = await self._transcribe_pending_audio_event_once(event)
                await self._echo_pending_stt_transcripts_once(
                    event, adapter, source, transcripts, metadata=metadata, log_context=log_context
                )
                # Sending an echo also yields to incoming merges. Refresh before interrupt/drain.
                if getattr(event, "_gateway_pending_stt_text", None) == enriched_text:
                    return enriched_text or text, transcripts
        except Exception as trans_exc:
            logger.warning("%s transcription failed: %s", log_context, trans_exc)
            return text, []

    def _start_pending_voice_stt(
        self, event, adapter, source, *, log_context: str, metadata=_UNSET,
    ) -> asyncio.Task | None:
        """Start this event's clip transcription as a tracked background task; never await it.

        Receipt/ordering fix: delivery proceeds while a long clip transcribes. Echo ownership is
        claimed inside the task (one echo per clip, gateway/pending_audio.py); the transcription
        result stays cached on the event so the follow-up turn's ``_prepare_inbound_message_text``
        joins the in-flight work instead of duplicating it. The task is registered on
        ``adapter._background_tasks`` (the canonical container; cancelled by shutdown) so it
        cannot outlive the gateway. Returns the task (test/observability handle)."""
        if not self._pending_event_audio_paths(event):
            return None

        async def _run() -> None:
            try:
                await self._transcribe_and_echo_pending_voice(
                    event, adapter, source, event.text or "", log_context=log_context,
                    metadata=metadata,
                )
            except asyncio.CancelledError:
                # A cancelled waiter must not cancel the shared clip task (asyncio.shield owns
                # that); this wrapper unwinding is all that happens here.
                raise
            except Exception as stt_exc:
                logger.warning("%s background transcription failed: %s", log_context, stt_exc)

        task = asyncio.create_task(_run())
        try:
            adapter._background_tasks.add(task)
            task.add_done_callback(adapter._background_tasks.discard)
        except AttributeError:
            pass
        return task

    _EMPTY_TEXT_PLACEHOLDER = "(The user sent a message with no text content)"

    @classmethod
    def _prepend_media_prefix(cls, prefix: str, user_text: str) -> str:
        """``prefix`` + the user's text; the Discord empty-content placeholder is dropped as redundant."""
        if user_text and user_text.strip() != cls._EMPTY_TEXT_PLACEHOLDER:
            return f"{prefix}\n\n{user_text}"
        return prefix

    @staticmethod
    def _untranscribed_audio_note(path: str) -> str:
        """One minimal neutral marker for every STT failure. Never mention "no STT provider" or setup
        steps — persisted in history they make the model keep volunteering STT-setup advice."""
        from tools.credential_files import to_agent_visible_cache_path
        agent_path = to_agent_visible_cache_path(os.path.abspath(path))
        return f"[voice message could not be transcribed automatically; the audio is available at: {agent_path}]"

    async def _transcribe_one_clip(self, path: str, transcribe_audio, transcribe_audio_local_fallback) -> tuple[Optional[str], str]:
        """``(transcript_or_None, note)`` for one clip via configured STT with local fallback."""
        result = await asyncio.to_thread(transcribe_audio, path, None, "gateway")
        if not result.get("success"):
            fallback = await asyncio.to_thread(transcribe_audio_local_fallback, path)
            if fallback.get("success"):
                logger.info("Configured STT failed for %s; recovered with local STT", path)
                result = fallback
        if not result["success"]:
            logger.info("Voice transcription failed for %s: %s", path, result.get("error", "unknown error"))
            return None, self._untranscribed_audio_note(path)
        transcript = result["transcript"]
        # STT may return success=True with an empty/whitespace transcript (silence, cut-off);
        # empty quotes make the agent reply to nothing and can loop, so emit a sentinel note.
        # See #41603.
        if not (transcript or "").strip():
            return None, (
                "[The user sent a voice message but it came through "
                "empty or inaudible — speech-to-text returned no "
                "words. Do not guess at the content; ask the user "
                "to resend or type it out.]"
            )
        # Plain quoted line: a "The user sent a voice message..." wrapper read as a meta-instruction
        # and made the LLM comment on voice mode instead.
        return transcript, f'"{transcript}"'
