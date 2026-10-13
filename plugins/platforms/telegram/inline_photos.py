"""Telegram inline-photo eligibility and legacy extraction, including code literals."""
import re
from typing import Any

from gateway.platforms.base import BasePlatformAdapter


class TelegramInlinePhotosMixin:
    _INLINE_PHOTO_RE = re.compile(r'!\[([^\]\n]*)\]\((https?://[^\s)]+)\)')

    @staticmethod
    def _photo_code_spans(content: str) -> list[tuple[int, int]]:
        """Protect fenced and inline code, without rejecting unrelated SKU literals."""
        spans = []
        fence = re.compile(r'(?m)^ {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)')
        position = 0
        while opening := fence.search(content, position):
            run = opening.group(1)
            closing = re.compile(r'(?m)^ {0,3}' + re.escape(run[0])
                                 + '{' + str(len(run)) + r',}[ \t]*(?:\n|$)')
            end = closing.search(content, opening.end())
            position = end.end() if end else len(content)
            spans.append((opening.start(), position))
        masked = list(content)
        for start, end in spans:
            masked[start:end] = ['\n' if c == '\n' else ' ' for c in content[start:end]]
        inline = re.compile(r'(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)')
        spans.extend((m.start(), m.end()) for m in inline.finditer(''.join(masked)))
        return sorted(spans)

    @classmethod
    def _photo_scan_text(cls, content: str) -> str:
        masked = list(content)
        for start, end in cls._photo_code_spans(content):
            masked[start:end] = ['\n' if c == '\n' else ' ' for c in content[start:end]]
        return ''.join(masked)

    @classmethod
    def _public_photo_matches(cls, content: str) -> list:
        """One URL criterion for native inline photos and attachment fallback."""
        from urllib.parse import parse_qs, urlsplit
        matches = []
        for match in cls._INLINE_PHOTO_RE.finditer(cls._photo_scan_text(content)):
            try:
                url = urlsplit(match.group(2))
                formats = parse_qs(url.query)
            except ValueError:
                continue
            if (url.hostname and not url.username and not url.password
                    and (url.path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
                         or any(value.lower() in {'jpg', 'jpeg', 'png', 'webp'}
                                for key in ('fmt', 'format') for value in formats.get(key, [])))):
                matches.append(match)
        return matches

    @staticmethod
    def extract_images(content: str) -> tuple[list[tuple[str, str]], str]:
        # Preserve code verbatim through legacy extraction as well: literals are not media.
        cls = TelegramInlinePhotosMixin
        protected = {}
        for index, (start, end) in reversed(list(enumerate(cls._photo_code_spans(content)))):
            token = f'\x00telegram_photo_code_{index}\x00'
            while token in content:
                token += '\x00'
            protected[token] = content[start:end]
            content = content[:start] + token + content[end:]
        matches = cls._public_photo_matches(content)
        images = [(m.group(2), m.group(1)) for m in matches]
        for match in reversed(matches):
            content = content[:match.start()] + content[match.end():]
        legacy_images, content = BasePlatformAdapter.extract_images(content)
        content = re.sub(r'\n{3,}', '\n\n', content).strip()
        for token, literal in protected.items():
            content = content.replace(token, literal)
        return images + legacy_images, content

    def _inline_photos(self, content: str) -> list:
        """Bounded mixed prose + public Markdown photos, not albums or other media."""
        scan = self._photo_scan_text(content or '')
        if not scan or re.search(r'MEDIA:|\[\[|<img\b|!\[[^\]]*\]\((?!https?://)', scan):
            return []
        matches = self._public_photo_matches(content)
        if (not matches or len(matches) > 10
                or len(matches) != len(list(self._INLINE_PHOTO_RE.finditer(scan)))
                or not self._INLINE_PHOTO_RE.sub('', scan).strip()):
            return []
        return matches

    def preserves_inline_images(self, content: str) -> bool:
        """Gateway capability: retain Markdown while keeping attachment fallback candidates."""
        return bool(self._inline_photos(content) and self._rich_eligible(content))

    def _rich_eligible(self, content: str) -> bool:
        """Final rich eligibility; draft and attachment controls remain at the caller."""
        return bool(
            self._rich_delivery_enabled()
            and not getattr(self, "_rich_send_disabled", False)
            and not re.search(r'MEDIA:|\[\[as_document\]\]', content or '')
            and content and content.strip()
            and (
                self._needs_rich_rendering(content)
                or bool(self._inline_photos(content))
                or self._exceeds_legacy_message_limit(content)
            )
            and self._rich_content_ok(content))

    def _rich_message_payload(self, content: str, *, skip_entity_detection: bool = False) -> dict[str, Any]:
        """Keep authored HTTPS image blocks in Telegram's native rich Markdown."""
        from plugins.platforms.telegram.adapter import _rich_normalize_linebreaks
        payload: dict[str, Any] = {"markdown": _rich_normalize_linebreaks(content)}
        if skip_entity_detection:
            payload["skip_entity_detection"] = True
        return payload
