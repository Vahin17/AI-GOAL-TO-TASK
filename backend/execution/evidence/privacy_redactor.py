"""
privacy_redactor.py — Privacy-Aware Evidence Redaction Engine (Phase 3 Objective B)
===================================================================================
Detects and redacts sensitive information (PII, credentials, financial details)
before evidence is persisted, logged, or surfaced to user-facing API consumers.

Supported Redaction Patterns:
1. Passwords, secrets, tokens, API keys in key-value pairs or text.
2. Credit card numbers (13-19 digits with optional delimiters, masked or replaced).
3. Social Security Numbers / National IDs (e.g. 123-45-6789).
4. Email addresses.
5. Phone numbers.
6. Bearer tokens and JSON Web Tokens (JWTs).

Design Invariant:
Redacted evidence maintains semantic and structural validity for downstream task
verification (e.g. preserving structure while masking raw secret values).

Known Limitations (Honest Documentation):
- Heuristics cannot guarantee 100% detection for arbitrary unstructured or obfuscated secrets.
- Image-based OCR / pixel-level visual redaction is bounded by browser metadata. When
  sensitive input actions (e.g. password fields) occur, full screenshots are replaced
  with privacy-safe placeholders to prevent visual data leakage.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Union

logger = logging.getLogger(__name__)

# ── Regex Patterns for Sensitive Data ───────────────────────────────────────

# Credit Card: 13-19 digits, optionally separated by spaces or hyphens
_CREDIT_CARD_REGEX = re.compile(
    r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13}|3(?:0[0-5]|[68][0-9])[0-9]{11}|6(?:011|5[0-9]{2})[0-9]{12}|(?:2131|1800|35\d{3})\d{11}|(?:[0-9]{4}[-\s]?){3}[0-9]{4})\b"
)

# Social Security Number (US standard): XXX-XX-XXXX
_SSN_REGEX = re.compile(r"\b(?!000|666)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")

# Email address
_EMAIL_REGEX = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"
)

# Phone number: requires delimiters (dash, dot, space) or parenthesized area code or country code prefix
_PHONE_REGEX = re.compile(
    r"(?:\+\d{1,3}[-.\s]?)?(?:\(\d{3}\)[-.\s]?|\b\d{3}[-.\s])\d{3}[-.\s]\d{4}\b"
)

# JWT / Bearer Token
_JWT_REGEX = re.compile(
    r"\beyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*\b"
)

# Generic high-entropy secret assignments: api_key=..., token=..., password=...
_SECRET_KEY_VALUE_REGEX = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?token|auth[_-]?code|private[_-]?key|client[_-]?secret)\b\s*[:=]\s*['\"]?([^'\"\s,;]+)['\"]?"
)

SENSITIVE_KEY_NAMES: Set[str] = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "access_token", "auth_code", "authorization", "private_key",
    "client_secret", "credit_card", "card_number", "cvv", "cvc",
    "ssn", "social_security", "pin", "session_token",
}


class PrivacyRedactor:
    """
    Authoritative privacy redactor for text, structured dictionaries,
    and browser evidence artifacts.
    """

    @classmethod
    def redact_text(cls, text: Optional[str], mask_cards: bool = True) -> str:
        """
        Redacts sensitive tokens, credit cards, emails, SSNs, and phone numbers from raw text.
        """
        if not text:
            return ""

        redacted = text

        # 1. Bearer / JWT Tokens
        redacted = _JWT_REGEX.sub("[REDACTED_JWT]", redacted)

        # 2. Key-value secret patterns (e.g. password=mysecret123)
        def _kv_replacer(match):
            key = match.group(1)
            return f"{key}=[REDACTED_SECRET]"
        redacted = _SECRET_KEY_VALUE_REGEX.sub(_kv_replacer, redacted)

        # 3. SSN
        redacted = _SSN_REGEX.sub("[REDACTED_SSN]", redacted)

        # 4. Credit Cards: mask middle digits (e.g. ****-****-****-1234) or replace
        def _card_replacer(match):
            raw = match.group(0).replace(" ", "").replace("-", "")
            if len(raw) >= 13 and (cls._luhn_check(raw) or len(raw) == 16):
                if mask_cards:
                    last4 = raw[-4:]
                    return f"****-****-****-{last4}"
                return "[REDACTED_CARD]"
            return match.group(0)

        redacted = _CREDIT_CARD_REGEX.sub(_card_replacer, redacted)

        # 5. Email addresses
        def _email_replacer(match):
            return "[REDACTED_EMAIL]"

        redacted = _EMAIL_REGEX.sub(_email_replacer, redacted)

        # 6. Phone numbers
        redacted = _PHONE_REGEX.sub("[REDACTED_PHONE]", redacted)

        return redacted

    @classmethod
    def redact_dict(cls, data: Any) -> Any:
        """
        Recursively traverses and redacts dictionary keys and values.
        """
        if isinstance(data, dict):
            clean_dict = {}
            # Detect whether this dictionary describes an interaction with a sensitive input
            sel = str(data.get("selector") or "").lower()
            is_sensitive_selector = any(
                sens in sel for sens in ("password", "passwd", "pwd", "pass", "card", "cvv", "cvc", "secret", "ssn", "pin")
            )

            for k, v in data.items():
                k_lower = str(k).lower().strip()
                if is_sensitive_selector and k_lower in ("value", "text", "input"):
                    clean_dict[k] = "[REDACTED_SECRET]"
                elif any(sens in k_lower for sens in SENSITIVE_KEY_NAMES):
                    clean_dict[k] = "[REDACTED_SECRET]"
                else:
                    clean_dict[k] = cls.redact_dict(v)
            return clean_dict
        elif isinstance(data, list):
            return [cls.redact_dict(item) for item in data]
        elif isinstance(data, str):
            return cls.redact_text(data)
        else:
            return data

    @classmethod
    def redact_evidence_item(cls, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Processes a complete browser evidence item:
        - Redacts action inputs, selectors with passwords, and typed values.
        - Redacts extracted text and logs.
        - If the action touched a sensitive password or payment field,
          replaces screenshots with a redacted privacy placeholder.
        """
        clean = dict(cls.redact_dict(item))

        # Check if action was interacting with sensitive inputs
        action = clean.get("action") or {}
        selector = str(action.get("selector") or "").lower()
        action_type = str(action.get("action_type") or "").lower()

        is_sensitive_input = any(
            k in selector for k in ("password", "passwd", "pwd", "pass", "card", "cvv", "cvc", "secret", "ssn", "pin")
        )

        result = clean.get("result") or {}
        if is_sensitive_input:
            action["value"] = "[REDACTED_SECRET]"
            if "screenshot_base64" in result and result["screenshot_base64"]:
                result["screenshot_base64"] = "[REDACTED_SCREENSHOT_PRIVACY_PROTECTION]"
                result["screenshot_redacted"] = True

        clean["action"] = action
        clean["result"] = result
        return clean

    @staticmethod
    def _luhn_check(card_number: str) -> bool:
        """Standard Luhn algorithm to avoid redacting non-card digit sequences."""
        digits = [int(c) for c in card_number if c.isdigit()]
        if len(digits) < 13 or len(digits) > 19:
            return False
        total = 0
        reverse_digits = digits[::-1]
        for i, d in enumerate(reverse_digits):
            if i % 2 == 1:
                doubled = d * 2
                total += doubled - 9 if doubled > 9 else doubled
            else:
                total += d
        return total % 10 == 0

