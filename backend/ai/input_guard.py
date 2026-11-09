"""
input_guard.py — Input Guard & Prompt Injection Defence (Phase 1)
=================================================================
Validates, normalizes, and inspects user-provided goals before they reach
the planning LLM or ARDO orchestration pipeline.

Core Invariants:
1. User input is ALWAYS treated as untrusted data.
2. Legitimate intent, URLs, emails, code, and quoted literals are NEVER mangled or rewritten.
3. Multi-vector injection defenses: Unicode normalization, zero-width evasion stripping,
   Bidi control rejection, delimiter breakout neutralization, and structural encapsulation.
4. Deterministic verdicts: ALLOW, REVIEW, DENY with explicit audit reasons.
"""

from __future__ import annotations

import enum
import logging
import re
import unicodedata
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class InputGuardVerdict(str, enum.Enum):
    ALLOW = "allow"
    REVIEW = "review"
    DENY = "deny"


class InputGuardResult(BaseModel):
    """
    Structured result of input guard evaluation.
    """
    verdict: InputGuardVerdict = Field(..., description="allow, review, or deny")
    sanitized_goal: str = Field(..., description="Cleaned, normalized goal text with preserved literals")
    safe_prompt_block: str = Field(..., description="Goal encapsulated in strict untrusted tags")
    risk_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Computed risk score from 0.0 to 1.0")
    flagged_patterns: List[str] = Field(default_factory=list, description="List of matched injection patterns")
    reason: Optional[str] = Field(default=None, description="Explanation for review or deny decision")
    is_valid: bool = Field(default=True, description="True if verdict is allow or review")
    requires_review: bool = Field(default=False, description="True if verdict is review")


class InputGuard:
    """
    Validates and sanitizes incoming user goals for the ARDO planning engine.
    """

    DEFAULT_MIN_LENGTH: int = 3
    DEFAULT_MAX_LENGTH: int = 4000

    # Dangerous Bidirectional Override Unicode codepoints (used to visually disguise injection)
    BIDI_OVERRIDES = {
        "\u202a",  # Left-to-Right Embedding
        "\u202b",  # Right-to-Left Embedding
        "\u202c",  # Pop Directional Formatting
        "\u202d",  # Left-to-Right Override
        "\u202e",  # Right-to-Left Override
        "\u2066",  # Left-to-Right Isolate
        "\u2067",  # Right-to-Left Isolate
        "\u2068",  # First Strong Isolate
        "\u2069",  # Pop Directional Isolate
    }

    # Stealth characters commonly inserted to bypass simple keyword filters (e.g. "i\u200bgnore")
    STEALTH_ZERO_WIDTH = {
        "\u200b",  # Zero-width space
        "\u200c",  # Zero-width non-joiner
        "\u200d",  # Zero-width joiner
        "\ufeff",  # Zero-width no-break space / BOM
    }

    # High-confidence injection patterns (Imperative instruction overrides)
    INJECTION_PATTERNS_HIGH: List[Tuple[str, str]] = [
        (
            r"(?i)\b(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+|any\s+|prior\s+|previous\s+|above\s+|system\s+)*(?:instructions|directives|prompts|rules|commands|constraints)\b",
            "instruction_override",
        ),
        (
            r"(?i)\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|switch\s+to)\s+(?:a\s+|an\s+|in\s+)?(?:developer\s+mode|dan|unfiltered|jailbroken|unrestricted|god\s+mode|root\s+user)\b",
            "role_confusion_jailbreak",
        ),
        (
            r"(?i)\b(?:output|repeat|print|reveal|display|show|leak)\s+(?:your\s+|the\s+)?(?:system\s+prompt|initial\s+prompt|system\s+instruction|developer\s+instructions|base\s+prompt)\b",
            "system_prompt_exfiltration",
        ),
        (
            r"(?i)<\s*/?\s*(?:untrusted_user_goal|system|system_instruction|instructions?|admin|root|prompt)\s*>",
            "delimiter_breakout_attempt",
        ),
        (
            r"(?i)\[\s*/?\s*(?:INST|SYS|SYSTEM)\s*\]|<\|im_start\|>|<\|im_end\|>|<<SYS>>|<</SYS>>",
            "raw_token_delimiter_injection",
        ),
        (
            r"(?i)\b(?:stop\s+being\s+ardo|new\s+system\s+directive|operational\s+mode\s*:\s*unrestricted)\b",
            "identity_usurpation",
        ),
    ]

    # Medium-confidence patterns (Suspicious keywords that warrant review if combined)
    INJECTION_PATTERNS_MEDIUM: List[Tuple[str, str]] = [
        (
            r"(?i)\b(?:system\s+prompt|system\s+instructions|system_instruction|jailbreak|prompt\s+injection)\b",
            "meta_prompt_reference",
        ),
        (
            r"(?i)\b(?:do\s+anything\s+now|never\s+say\s+no|bypass\s+safety\s+filter)\b",
            "unrestricted_intent",
        ),
        (
            r"(?i)\b(?:base64\s*:\s*[A-Za-z0-9+/=]{20,})\b",
            "obfuscated_payload",
        ),
    ]

    QUOTE_PATTERNS: List[re.Pattern] = [
        re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"'),
        re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'"),
        re.compile(r"`([^`\\]*(?:\\.[^`\\]*)*)`"),
        re.compile(r"[\u201c\u201d]([^\u201c\u201d]*)[\u201c\u201d]"),
        re.compile(r"[\u2018\u2019]([^\u2018\u2019]*)[\u2018\u2019]"),
    ]

    @classmethod
    def get_quoted_spans(cls, text: str) -> List[Tuple[int, int]]:
        """
        Extracts start and end index tuples for all balanced quoted strings.
        """
        spans: List[Tuple[int, int]] = []
        for pattern in cls.QUOTE_PATTERNS:
            for m in pattern.finditer(text):
                spans.append((m.start(), m.end()))
        return sorted(spans)

    @classmethod
    def is_span_inside_quotes(cls, span: Tuple[int, int], quoted_spans: List[Tuple[int, int]]) -> bool:
        """
        Returns True if the target span falls completely within any quoted span.
        """
        start, end = span
        return any(q_start <= start and end <= q_end for q_start, q_end in quoted_spans)

    @classmethod
    def clean_control_characters(cls, text: str) -> Tuple[str, bool, bool]:
        """
        Cleans control characters while preserving valid whitespace (\\n, \\r, \\t),
        quotes, URLs, and code.
        
        Returns:
            (cleaned_text, has_bidi_override, has_null_byte)
        """
        has_bidi = any(ch in text for ch in cls.BIDI_OVERRIDES)
        has_null = "\x00" in text

        # Strip stealth zero-width characters used for evasion
        chars = []
        for ch in text:
            if ch in cls.STEALTH_ZERO_WIDTH:
                continue
            if ch in cls.BIDI_OVERRIDES:
                continue
            # Keep standard printable characters and common whitespace
            category = unicodedata.category(ch)
            if category.startswith("C") and ch not in ("\n", "\r", "\t"):
                continue
            chars.append(ch)

        return "".join(chars), has_bidi, has_null

    @classmethod
    def wrap_in_untrusted_block(cls, sanitized_text: str) -> str:
        """
        Encloses user input in strict structural delimiters.
        Escapes any inadvertent or attempted literal closing tags inside the text.
        """
        # Neutralize any literal closing tag within text so it cannot break out
        neutralized = re.sub(
            r"(?i)</\s*untrusted_user_goal\s*>",
            "&lt;/untrusted_user_goal&gt;",
            sanitized_text,
        )
        return f"<untrusted_user_goal>\n{neutralized}\n</untrusted_user_goal>"

    @classmethod
    def validate_goal(
        cls,
        raw_goal: str,
        min_length: Optional[int] = None,
        max_length: Optional[int] = None,
    ) -> InputGuardResult:
        """
        Validates an incoming user goal.
        
        Evaluates length, Unicode integrity, prompt injection heuristics, and
        returns a strongly-typed InputGuardResult.
        """
        min_len = min_length if min_length is not None else cls.DEFAULT_MIN_LENGTH
        max_len = max_length if max_length is not None else cls.DEFAULT_MAX_LENGTH

        if raw_goal is None:
            return InputGuardResult(
                verdict=InputGuardVerdict.DENY,
                sanitized_goal="",
                safe_prompt_block="",
                risk_score=1.0,
                flagged_patterns=["empty_input"],
                reason="Goal input cannot be None.",
                is_valid=False,
            )

        # 1. Unicode Normalization (NFKC - Canonical Decomposition followed by Canonical Composition)
        normalized = unicodedata.normalize("NFKC", raw_goal)

        # 2. Control Character & Stealth Character Inspection
        cleaned, has_bidi, has_null = cls.clean_control_characters(normalized)
        stripped = cleaned.strip()

        # Reject immediate null byte tampering
        if has_null:
            return InputGuardResult(
                verdict=InputGuardVerdict.DENY,
                sanitized_goal=stripped,
                safe_prompt_block=cls.wrap_in_untrusted_block(stripped),
                risk_score=1.0,
                flagged_patterns=["null_byte_injection"],
                reason="Input contains forbidden null bytes (\\x00).",
                is_valid=False,
            )

        # Reject dangerous bidirectional overrides (used in homograph/order disguise attacks)
        if has_bidi:
            return InputGuardResult(
                verdict=InputGuardVerdict.DENY,
                sanitized_goal=stripped,
                safe_prompt_block=cls.wrap_in_untrusted_block(stripped),
                risk_score=0.95,
                flagged_patterns=["bidi_override_injection"],
                reason="Input contains dangerous bidirectional Unicode overrides.",
                is_valid=False,
            )

        # 3. Length Validation
        if len(stripped) < min_len:
            return InputGuardResult(
                verdict=InputGuardVerdict.DENY,
                sanitized_goal=stripped,
                safe_prompt_block=cls.wrap_in_untrusted_block(stripped),
                risk_score=0.8,
                flagged_patterns=["input_too_short"],
                reason=f"Goal is too short (minimum {min_len} characters required).",
                is_valid=False,
            )

        if len(stripped) > max_len:
            return InputGuardResult(
                verdict=InputGuardVerdict.DENY,
                sanitized_goal=stripped[:max_len],
                safe_prompt_block=cls.wrap_in_untrusted_block(stripped[:max_len]),
                risk_score=0.8,
                flagged_patterns=["input_too_long"],
                reason=f"Goal exceeds maximum allowed length of {max_len} characters.",
                is_valid=False,
            )

        # 4. Pattern & Structural Analysis
        flagged: List[str] = []
        risk_score: float = 0.0
        quoted_spans = cls.get_quoted_spans(stripped)
        has_unquoted_high_injection = False
        has_quoted_high_injection = False

        # Check High-Severity Injection Patterns
        for pattern, tag in cls.INJECTION_PATTERNS_HIGH:
            for m in re.finditer(pattern, stripped):
                # Delimiter breakouts and raw token delimiter injections are NEVER protected by quotes
                if tag in ("delimiter_breakout_attempt", "raw_token_delimiter_injection"):
                    if tag not in flagged:
                        flagged.append(tag)
                    has_unquoted_high_injection = True
                    risk_score = max(risk_score, 0.95)
                elif cls.is_span_inside_quotes((m.start(), m.end()), quoted_spans):
                    quoted_tag = f"quoted_{tag}"
                    if quoted_tag not in flagged:
                        flagged.append(quoted_tag)
                    has_quoted_high_injection = True
                else:
                    if tag not in flagged:
                        flagged.append(tag)
                    has_unquoted_high_injection = True
                    risk_score = max(risk_score, 0.9)

        if has_unquoted_high_injection:
            risk_score = max(risk_score, 0.9)
        elif has_quoted_high_injection:
            # High severity pattern matched ONLY within quotes (e.g. Search Google for "ignore all previous instructions")
            # Legitimate goal with quoted literals -> classified as REVIEW, not DENY
            risk_score = max(risk_score, 0.5)

        # Check Medium-Severity Suspicious Patterns
        medium_matches = 0
        for pattern, tag in cls.INJECTION_PATTERNS_MEDIUM:
            if re.search(pattern, stripped):
                if tag not in flagged:
                    flagged.append(tag)
                medium_matches += 1

        if not has_unquoted_high_injection and not has_quoted_high_injection:
            if medium_matches == 1 and risk_score < 0.9:
                risk_score = max(risk_score, 0.4)
            elif medium_matches > 1 and risk_score < 0.9:
                risk_score = max(risk_score, 0.7)

        # 5. Determine Verdict
        if risk_score >= 0.8:
            verdict = InputGuardVerdict.DENY
            reason = f"Security block: Suspected prompt injection or policy evasion attempt ({', '.join(flagged)})."
            is_valid = False
            requires_review = False
        elif risk_score >= 0.4:
            verdict = InputGuardVerdict.REVIEW
            if has_quoted_high_injection:
                reason = f"Review required: Goal contains quoted prompt instruction literals ({', '.join(flagged)}). Clarification required before execution."
            else:
                reason = f"Review advised: Suspicious meta-prompt keywords detected ({', '.join(flagged)}). Clarification required before execution."
            is_valid = True
            requires_review = True
        else:
            verdict = InputGuardVerdict.ALLOW
            reason = "Input validated successfully."
            is_valid = True
            requires_review = False

        safe_block = cls.wrap_in_untrusted_block(stripped)

        return InputGuardResult(
            verdict=verdict,
            sanitized_goal=stripped,
            safe_prompt_block=safe_block,
            risk_score=round(risk_score, 2),
            flagged_patterns=flagged,
            reason=reason,
            is_valid=is_valid,
            requires_review=requires_review,
        )
