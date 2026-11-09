"""
test_phase10_gemini_provider.py — Unit Tests for GeminiProvider Reliability (Phase 10)
=====================================================================================
Validates:
1. gemini-3.5-flash is used as the configured primary model.
2. 429 does not blindly cycle through fallback models.
3. Quota-exhaustion 429 produces a clear, final failure without model hopping.
4. Temporary 429 rate-limit performs bounded backoff and succeeds on retry.
5. 503 performs a small bounded retry with backoff on the primary model.
6. Repeated 503 responses eventually fail cleanly (GeminiServiceUnavailableError).
7. 400 (Bad Request) fails immediately and is not retried.
8. 401/403 (Auth Failure) fails immediately and is not retried.
9. 404 (Model Not Found) falls back to an approved fallback model.
10. Fallback attempts are bounded (no infinite chain).
11. No infinite retry or model-hopping loop is possible.
12. Retry-After header and retryDelay payload fields are respected when available.
13. Sensitive information (API keys) are redacted and not exposed in errors/logs.
14. Successful Gemini response parsing functions seamlessly.
15. generate_plan and query interface contracts remain compatible.
"""

import asyncio
import io
import json
import urllib.error
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from ai.gemini_provider import (
    GeminiProvider,
    GeminiProviderError,
    GeminiAuthenticationError,
    GeminiInvalidRequestError,
    GeminiQuotaExceededError,
    GeminiRateLimitError,
    GeminiServiceUnavailableError,
    GeminiModelNotFoundError,
    _redact_sensitive,
    _parse_error_body,
)
from schemas.models import PlanGraphResponse


def make_http_error(code: int, msg: str = "", body_dict: dict = None, headers: dict = None) -> urllib.error.HTTPError:
    """Helper to construct realistic urllib HTTPError instances."""
    raw_body = json.dumps(body_dict or {}).encode("utf-8") if body_dict else b""
    fp = io.BytesIO(raw_body)
    err = urllib.error.HTTPError(
        url="https://generativelanguage.googleapis.com/v1beta/models/mock:generateContent?key=AIzaSySecret12345",
        code=code,
        msg=msg or f"HTTP {code}",
        hdrs=headers or {},
        fp=fp,
    )
    return err


def make_gemini_success_response(text: str) -> bytes:
    """Helper to construct realistic Google AI Studio REST response payload."""
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": text}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


class TestGeminiProviderReliability:

    def test_1_primary_configured_model(self):
        """Req 1: gemini-3.5-flash is configured as the default primary model."""
        provider = GeminiProvider(api_key="test_key")
        assert provider.model_name == "gemini-3.5-flash"
        assert provider.max_retries == 2

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_2_429_does_not_blindly_cycle_fallback_models(self, mock_urlopen, mock_sleep):
        """Req 2: A 429 error retries only on the primary model, never cycling to fallbacks."""
        # Return 429 on all calls
        http_err = make_http_error(
            429,
            body_dict={"error": {"message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}},
        )
        mock_urlopen.side_effect = http_err

        provider = GeminiProvider(
            api_key="test_key",
            model_name="gemini-3.5-flash",
            max_retries=2,
            fallback_models=["gemini-2.5-flash"],
        )

        with pytest.raises((GeminiRateLimitError, GeminiQuotaExceededError)):
            asyncio.run(provider.query("test prompt"))

        # Primary model tried (1 initial + 2 retries = 3 calls total)
        assert mock_urlopen.call_count == 3
        # Ensure all 3 calls were made to gemini-3.5-flash and never gemini-2.5-flash
        for call_args in mock_urlopen.call_args_list:
            req = call_args[0][0]
            assert "gemini-3.5-flash" in req.full_url
            assert "gemini-2.5-flash" not in req.full_url

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_3_quota_exhaustion_produces_immediate_clear_failure(self, mock_urlopen, mock_sleep):
        """Req 3: Permanent quota exhaustion fails immediately without retrying or model hopping."""
        http_err = make_http_error(
            429,
            body_dict={
                "error": {
                    "message": "You have exceeded your current quota. Please check your plan.",
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [{"@type": "ErrorInfo", "reason": "DAILY_QUOTA_EXCEEDED"}],
                }
            },
        )
        mock_urlopen.side_effect = http_err

        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)

        with pytest.raises(GeminiQuotaExceededError) as exc_info:
            asyncio.run(provider.query("test prompt"))

        assert "quota exhausted" in str(exc_info.value).lower()
        # Exactly 1 call made (no wasted retries or model switches)
        assert mock_urlopen.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_4_temporary_rate_limit_bounded_retry_succeeds(self, mock_urlopen, mock_sleep):
        """Req 4: Temporary 429 rate-limit performs bounded backoff and succeeds on retry."""
        http_err = make_http_error(
            429,
            body_dict={
                "error": {
                    "message": "Rate limit exceeded. Please try again shortly.",
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [{"@type": "RetryInfo", "retryDelay": "1.5s"}],
                }
            },
        )
        success_resp = MagicMock()
        success_resp.read.return_value = make_gemini_success_response("Recovered response")
        success_resp.__enter__.return_value = success_resp

        # First call: 429, Second call: Success
        mock_urlopen.side_effect = [http_err, success_resp]

        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)
        res = asyncio.run(provider.query("test prompt"))

        assert res == "Recovered response"
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(1.5)

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_5_503_bounded_retry_with_backoff(self, mock_urlopen, mock_sleep):
        """Req 5: 503 service unavailable performs bounded backoff and succeeds on retry."""
        http_err = make_http_error(503, msg="Service Unavailable", headers={"Retry-After": "2.0"})
        success_resp = MagicMock()
        success_resp.read.return_value = make_gemini_success_response("503 recovered")
        success_resp.__enter__.return_value = success_resp

        mock_urlopen.side_effect = [http_err, success_resp]

        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)
        res = asyncio.run(provider.query("test prompt"))

        assert res == "503 recovered"
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(2.0)

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_6_repeated_503_fails_cleanly(self, mock_urlopen, mock_sleep):
        """Req 6: Persistent 503 errors fail cleanly with GeminiServiceUnavailableError after max_retries."""
        http_err = make_http_error(503, msg="Model Overloaded")
        mock_urlopen.side_effect = http_err

        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)

        with pytest.raises(GeminiServiceUnavailableError) as exc_info:
            asyncio.run(provider.query("test prompt"))

        assert "service unavailable (503)" in str(exc_info.value).lower()
        # 1 initial + 2 retries = 3 calls
        assert mock_urlopen.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_7_400_bad_request_fails_immediately(self, mock_urlopen, mock_sleep):
        """Req 7: 400 Bad Request is not retried and raises GeminiInvalidRequestError."""
        http_err = make_http_error(
            400,
            body_dict={"error": {"message": "Invalid JSON payload structure", "status": "INVALID_ARGUMENT"}},
        )
        mock_urlopen.side_effect = http_err

        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)

        with pytest.raises(GeminiInvalidRequestError):
            asyncio.run(provider.query("test prompt"))

        assert mock_urlopen.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_8_401_403_auth_fails_immediately(self, mock_urlopen, mock_sleep):
        """Req 8: 401/403 Auth failures are not retried and raise GeminiAuthenticationError."""
        http_err = make_http_error(
            403,
            body_dict={"error": {"message": "API key not valid. Please pass a valid API key.", "status": "PERMISSION_DENIED"}},
        )
        mock_urlopen.side_effect = http_err

        provider = GeminiProvider(api_key="test_key", model_name="gemini-3.5-flash", max_retries=2)

        with pytest.raises(GeminiAuthenticationError):
            asyncio.run(provider.query("test prompt"))

        assert mock_urlopen.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_9_404_model_not_found_uses_bounded_fallback(self, mock_urlopen, mock_sleep):
        """Req 9 & 10: 404 Model Not Found triggers bounded fallback to an available model."""
        http_404 = make_http_error(
            404,
            body_dict={"error": {"message": "Model 'gemini-nonexistent' not found", "status": "NOT_FOUND"}},
        )
        success_resp = MagicMock()
        success_resp.read.return_value = make_gemini_success_response("Fallback model output")
        success_resp.__enter__.return_value = success_resp

        # First call on nonexistent model -> 404, Second call on fallback model -> success
        mock_urlopen.side_effect = [http_404, success_resp]

        provider = GeminiProvider(
            api_key="test_key",
            model_name="gemini-nonexistent",
            fallback_models=["gemini-2.5-flash"],
        )

        res = asyncio.run(provider.query("test prompt"))
        assert res == "Fallback model output"

        # Checked primary then exactly 1 fallback
        assert mock_urlopen.call_count == 2
        first_url = mock_urlopen.call_args_list[0][0][0].full_url
        second_url = mock_urlopen.call_args_list[1][0][0].full_url
        assert "gemini-nonexistent" in first_url
        assert "gemini-2.5-flash" in second_url

    @patch("asyncio.sleep", new_callable=AsyncMock)
    @patch("urllib.request.urlopen")
    def test_10_fallback_is_strictly_bounded_on_all_404(self, mock_urlopen, mock_sleep):
        """Req 10 & 11: If all models 404, terminates cleanly without unbounded hopping."""
        http_404 = make_http_error(
            404,
            body_dict={"error": {"message": "Model not found", "status": "NOT_FOUND"}},
        )
        mock_urlopen.side_effect = http_404

        provider = GeminiProvider(
            api_key="test_key",
            model_name="gemini-bad-1",
            fallback_models=["gemini-bad-2", "gemini-bad-3"],
        )

        with pytest.raises(GeminiModelNotFoundError):
            asyncio.run(provider.query("test prompt"))

        # Bounded: attempted primary + at most 1 fallback
        assert mock_urlopen.call_count <= 2

    def test_12_redact_sensitive_keys(self):
        """Req 13: Secrets in error messages and URLs are redacted."""
        raw_msg = "Error connecting to https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key=AIzaSySecretApiKey12345 in backend"
        redacted = _redact_sensitive(raw_msg)
        assert "AIzaSySecretApiKey12345" not in redacted
        assert "key=[REDACTED]" in redacted

    def test_13_parse_retry_info_details(self):
        """Req 12: Parses retryDelay from details payload or headers."""
        err_body = {
            "error": {
                "message": "Resource exhausted",
                "status": "RESOURCE_EXHAUSTED",
                "details": [{"@type": "RetryInfo", "retryDelay": "4.25s"}],
            }
        }
        msg, status, delay, is_perm = _parse_error_body(429, {}, json.dumps(err_body).encode("utf-8"))
        assert delay == 4.25
        assert not is_perm

    @patch("urllib.request.urlopen")
    def test_14_successful_response_and_plan_generation(self, mock_urlopen):
        """Req 14 & 15: Successful query and generate_plan response parsing."""
        plan_json = json.dumps({
            "nodes": [
                {
                    "id": "1",
                    "type": "custom",
                    "position": {"x": 250, "y": 0},
                    "data": {"label": "Open Wikipedia in new tab", "isExecuting": False, "isCompleted": False},
                }
            ],
            "edges": [],
            "metadata": {
                "intent": "browser_navigation",
                "confidence": 0.95,
                "reasoning_summary": "Open Wikipedia in browser tab",
            },
        })
        success_resp = MagicMock()
        success_resp.read.return_value = make_gemini_success_response(plan_json)
        success_resp.__enter__.return_value = success_resp
        mock_urlopen.return_value = success_resp

        provider = GeminiProvider(api_key="test_key")
        plan = asyncio.run(provider.generate_plan("Open Wikipedia"))

        assert isinstance(plan, PlanGraphResponse)
        assert len(plan.nodes) == 1
        assert plan.nodes[0].data["label"] == "Open Wikipedia in new tab"
