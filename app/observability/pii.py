"""PII redaction applied before anything is written to logs, traces or memory (rubric 4.4).

Two layers, so redaction never silently stops working:

1. Cloud DLP (`google-cloud-dlp`), when APPLYPILOT_USE_DLP=true. Best recall for
   free text (names, street addresses, etc.).
2. Deterministic regex (always on, also the fallback if DLP is unavailable or
   errors). Covers emails, phone numbers, SSNs, credit cards (Luhn-checked),
   API keys / bearer tokens, and LinkedIn/GitHub profile URLs.

On top of both, callers can pass *known values* (e.g. the candidate's real name
and email from session state) which are always redacted verbatim, so the PII
we know about can never leak even if a pattern misses it.

Redacted values become `[REDACTED_<TYPE>]`, e.g. `[REDACTED_EMAIL]`.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from functools import cache
from typing import Any

_log = logging.getLogger(__name__)

# Order matters: more specific patterns first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("API_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),  # Google API key
    ("API_KEY", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b")),
    ("TOKEN", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    (
        "TOKEN",
        re.compile(
            r"\bey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"
        ),
    ),  # JWT
    (
        "LINKEDIN",
        re.compile(r"(?i)(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_\-]+/?"),
    ),
    (
        "GITHUB",
        re.compile(r"(?i)(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9_\-]+/?"),
    ),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ \-]?){13,19}\b")),
    (
        "PHONE",
        re.compile(
            r"(?<!\w)(?:\+?\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}(?!\w)"
        ),
    ),
]

# Cloud DLP infoTypes -> our redaction labels.
_DLP_INFO_TYPES = {
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "US_SOCIAL_SECURITY_NUMBER": "SSN",
    "CREDIT_CARD_NUMBER": "CREDIT_CARD",
    "PERSON_NAME": "PERSON_NAME",
    "STREET_ADDRESS": "STREET_ADDRESS",
    "GCP_API_KEY": "API_KEY",
    "AUTH_TOKEN": "TOKEN",
}

# Keys whose values are always fully redacted when scrubbing dicts.
SENSITIVE_KEYS = {
    "email",
    "phone",
    "full_name",
    "linkedin",
    "linkedin_url",
    "github",
    "candidate_contact",
    "authorization",
    "api_key",
    "token",
    "password",
    "secret",
}


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact_with_regex(text: str) -> str:
    """Deterministic redaction; safe to call on any string, never raises."""
    for label, pattern in _PATTERNS:
        if label == "CREDIT_CARD":
            text = pattern.sub(
                lambda m: (
                    "[REDACTED_CREDIT_CARD]" if _luhn_ok(m.group(0)) else m.group(0)
                ),
                text,
            )
        else:
            text = pattern.sub(f"[REDACTED_{label}]", text)
    return text


@cache
def _dlp_client():
    from google.cloud import dlp_v2

    return dlp_v2.DlpServiceClient()


def redact_with_dlp(text: str, project_id: str) -> str:
    """Redact with Cloud DLP's de-identify API (replace with infoType label)."""
    client = _dlp_client()
    response = client.deidentify_content(
        request={
            "parent": f"projects/{project_id}/locations/global",
            "inspect_config": {
                "info_types": [{"name": n} for n in _DLP_INFO_TYPES],
                "min_likelihood": "LIKELY",
            },
            "deidentify_config": {
                "info_type_transformations": {
                    "transformations": [
                        {
                            "info_types": [{"name": n}],
                            "primitive_transformation": {
                                "replace_config": {
                                    "new_value": {"string_value": f"[REDACTED_{label}]"}
                                }
                            },
                        }
                        for n, label in _DLP_INFO_TYPES.items()
                    ]
                }
            },
            "item": {"value": text},
        },
        timeout=5.0,
    )
    return response.item.value


_PERMANENT_DLP_ERRORS = {
    "PermissionDenied",
    "Unauthenticated",
    "NotFound",
    "FailedPrecondition",
}


class PIIScrubber:
    """Redacts PII from strings and nested structures before storage."""

    def __init__(self, use_dlp: bool | None = None, project_id: str | None = None):
        self.use_dlp = (
            use_dlp
            if use_dlp is not None
            else os.getenv("APPLYPILOT_USE_DLP", "false").lower() in ("1", "true")
        )
        # Agent Engine sets GOOGLE_CLOUD_PROJECT to the project NUMBER, which the
        # DLP API rejects ("Malformed parent field"); prefer an explicit ID.
        self.project_id = (
            project_id
            or os.getenv("APPLYPILOT_PROJECT_ID")
            or os.getenv("GOOGLE_CLOUD_PROJECT", "")
        )
        self._dlp_failed = False

    def scrub_text(self, text: str, known_values: Iterable[str] = ()) -> str:
        if not text:
            return text
        for value in sorted(
            {v for v in known_values if v and len(v) >= 3}, key=len, reverse=True
        ):
            # "Maya Lin" also matches "maya_lin", "maya-lin", "maya.lin" (file names, URLs).
            pattern = r"[\s_.\-]+".join(re.escape(w) for w in value.split())
            text = re.sub(pattern, "[REDACTED_KNOWN_PII]", text, flags=re.I)
        if self.use_dlp and self.project_id and not self._dlp_failed and len(text) > 20:
            try:
                text = redact_with_dlp(text, self.project_id)
            except Exception as exc:  # never let redaction break the request
                # Permanent problems (API disabled, no permission): stop calling
                # DLP for this process. Anything else is per-request: retry next time.
                permanent = type(exc).__name__ in _PERMANENT_DLP_ERRORS
                self._dlp_failed = self._dlp_failed or permanent
                _log.warning(
                    "Cloud DLP %s, using regex redaction for this text: %s %s",
                    "disabled" if permanent else "call failed",
                    type(exc).__name__,
                    str(getattr(exc, "message", ""))[:200],
                )
        return redact_with_regex(text)  # always run: belt and braces

    def scrub(
        self, value: Any, known_values: Iterable[str] = (), _depth: int = 0
    ) -> Any:
        """Recursively scrub dicts/lists/strings. Sensitive keys are fully masked."""
        known = tuple(known_values)
        if _depth > 8:
            return "[TRUNCATED]"
        if isinstance(value, str):
            return self.scrub_text(value, known)
        if isinstance(value, dict):
            return {
                k: (
                    "[REDACTED]"
                    if str(k).lower() in SENSITIVE_KEYS and value[k]
                    else self.scrub(value[k], known, _depth + 1)
                )
                for k in value
            }
        if isinstance(value, (list, tuple)):
            return [self.scrub(v, known, _depth + 1) for v in value]
        return value


@cache
def get_scrubber() -> PIIScrubber:
    """Scrubber for storage pipelines (long-term memory): Cloud DLP + regex."""
    return PIIScrubber()


@cache
def get_log_scrubber() -> PIIScrubber:
    """Scrubber for the logging hot path: regex + known values only.

    Every log line passes through this synchronously, so it must be fast and
    never make a network call. DLP's free-text detection is used on the memory
    pipeline, where it runs in a background worker thread.
    """
    return PIIScrubber(use_dlp=False)


def known_pii_from_state(state: Any) -> list[str]:
    """Real contact values we hold for the candidate; always redacted verbatim."""
    try:
        from app.tools.session_tools import candidate_contact

        contact = candidate_contact(state)
    except Exception:
        return []
    return [str(v) for v in contact.values() if v]
