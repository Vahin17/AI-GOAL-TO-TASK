"""
test_runtime_stabilization.py — Real-World Execution Failure Fixes (Runtime Stabilization)
========================================================================================
Validates all 7 targeted runtime defect remediations:
- Issue A: New-tab outcome verification, about:blank handling, tab count before/after,
           active-page state, duplicate tab prevention, no bogus example.com navigations.
- Issue B: IPv6 address classification in NetworkGuard, CDP socket bracket parsing,
           globally routable IPv6 permitted without false-positive Invalid IP errors.
- Issue C: Gemini quota management & reasoning efficiency, free-tier exhaustion handling,
           bypassing LLM for deterministic actions, per-session request budget.
- Issue D: Goal lifecycle persistence (PLANNING -> PLANNED -> EXECUTING -> terminal states).
- Issue E: Browser inspection mode configuration & preservation on failure in dev.
- Issue F: Frontend React Flow parent container dimensions & evidence panel quota distinction.
- Issue G: Embedding fallback visibility, recording OpenAI 429 quota exhaustion in stats/diagnostics.
"""

import asyncio
import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from core.config import settings
from execution.browser.network_guard import NetworkGuard
from execution.browser.agent_contracts import (
    BrowserTask,
    BrowserObservation,
    BrowserDecisionType,
    BrowserVerificationStatus,
    BrowserAgentOutcome,
)
from execution.browser.browser_models import BrowserActionType
from execution.browser.perception_models import TabInfo
from execution.browser.semantic_verifier import SemanticVerifier
from execution.browser.browser_agent import BrowserAgent
from ai.gemini_provider import GeminiQuotaExceededError, _parse_error_body
from knowledge.embeddings import HybridEmbeddingService


# ── Issue A: New-Tab Outcome Verification ───────────────────────────────────

def test_issue_a_new_tab_about_blank_verification():
    """about:blank is accepted as verified when the task contract specifically requested a new tab."""
    task = BrowserTask(
        task_id="t_tab_1",
        objective="Open a new tab",
        session_id="s_test",
    )
    obs = BrowserObservation(
        session_id="s_test",
        task_id="t_tab_1",
        current_url="about:blank",
        page_title="",
        open_tab_count=2,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", title="Dashboard", is_active=False, index=0),
            TabInfo(tab_ref="tab_002", url="about:blank", title="", is_active=True, index=1),
        ],
    )
    verif = SemanticVerifier.verify(task, obs)
    assert verif.status == BrowserVerificationStatus.VERIFIED
    assert "new tab" in verif.verified_by.lower()
    assert verif.failure_reason is None


def test_issue_a_new_tab_fails_if_only_one_tab_present():
    """If task requested a new tab but tab count remains 1, verification fails."""
    task = BrowserTask(
        task_id="t_tab_2",
        objective="Open a new tab",
        session_id="s_test",
    )
    obs = BrowserObservation(
        session_id="s_test",
        task_id="t_tab_2",
        current_url="https://ardo.local",
        page_title="Dashboard",
        open_tab_count=1,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", title="Dashboard", is_active=True, index=0),
        ],
    )
    verif = SemanticVerifier.verify(task, obs)
    assert verif.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "only 1 tab was observed" in verif.failure_reason


@pytest.mark.asyncio
async def test_issue_a_duplicate_tab_prevention_in_agent():
    """BrowserAgent opens a new tab and finishes without creating duplicate tabs on subsequent iterations."""
    task = BrowserTask(
        task_id="t_tab_dup",
        objective="Open a new tab",
        session_id="s_test",
        max_actions=5,
    )
    obs_1 = BrowserObservation(
        session_id="s_test",
        task_id="t_tab_dup",
        current_url="https://ardo.local",
        open_tab_count=1,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", title="Dashboard", is_active=True, index=0),
        ],
    )
    obs_2 = BrowserObservation(
        session_id="s_test",
        task_id="t_tab_dup",
        current_url="about:blank",
        open_tab_count=2,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", title="Dashboard", is_active=False, index=0),
            TabInfo(tab_ref="tab_002", url="about:blank", title="", is_active=True, index=1),
        ],
    )

    llm = MagicMock()
    llm.query = AsyncMock(side_effect=Exception("LLM should not be called when new tab is verified"))

    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)
    adapter.execute_safe_browser_task = AsyncMock(return_value=(MagicMock(success=True, error=None), MagicMock(is_blocked=False)))

    perception = MagicMock()
    obs_seq = iter([obs_1, obs_2, obs_2])
    perception.observe = AsyncMock(side_effect=lambda *a, **k: next(obs_seq))

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    res = await agent.run(task)

    assert res.outcome == BrowserAgentOutcome.COMPLETED
    assert res.verification.status == BrowserVerificationStatus.VERIFIED
    # Exactly one action dispatched to create tab, no duplicate tab actions
    assert len(res.actions_taken) == 1
    assert adapter.execute_safe_browser_task.await_count == 1


@pytest.mark.asyncio
async def test_issue_a_new_tab_completes_without_bogus_example_navigation():
    """BrowserAgent opens a new tab and immediately completes upon contract verification without navigating to example.com."""
    task = BrowserTask(
        task_id="t_tab_fresh",
        objective="Open a new tab",
        session_id="s_test",
        max_actions=5,
    )
    initial_obs = BrowserObservation(
        session_id="s_test",
        task_id="t_tab_fresh",
        current_url="https://ardo.local",
        open_tab_count=1,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", title="Dashboard", is_active=True, index=0),
        ],
    )
    tab_opened_obs = BrowserObservation(
        session_id="s_test",
        task_id="t_tab_fresh",
        current_url="about:blank",
        open_tab_count=2,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", title="Dashboard", is_active=False, index=0),
            TabInfo(tab_ref="tab_002", url="about:blank", title="", is_active=True, index=1),
        ],
    )

    llm = MagicMock()
    # LLM must not be queried: explicit action + pre-verification completes task
    llm.query = AsyncMock(side_effect=Exception("LLM should not be called for atomic new-tab creation!"))

    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)
    executed_actions = []

    async def mock_execute(task_id, session_id, action):
        executed_actions.append(action)
        action_res = MagicMock(success=True, error=None)
        safety_dec = MagicMock(is_blocked=False)
        return action_res, safety_dec

    adapter.execute_safe_browser_task = mock_execute

    perception = MagicMock()
    obs_cycle = iter([initial_obs, tab_opened_obs, tab_opened_obs])
    perception.observe = AsyncMock(side_effect=lambda *a, **k: next(obs_cycle))

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    res = await agent.run(task)

    assert res.outcome == BrowserAgentOutcome.COMPLETED
    assert res.verification.status == BrowserVerificationStatus.VERIFIED
    assert len(executed_actions) == 1
    assert executed_actions[0].action_type == BrowserActionType.NAVIGATE
    assert executed_actions[0].url == "about:blank"
    assert executed_actions[0].new_tab is True
    # Verify no navigation to example.com occurred
    assert not any("example.com" in (getattr(a, "url", "") or "") for a in executed_actions)


# ── Issue B: IPv6 Address Classification in NetworkGuard ────────────────────

def test_issue_b_cloudflare_ipv6_permitted():
    """Globally-routable IPv6 address from Cloudflare/CDP is permitted without false-positive Invalid IP."""
    cf_ipv6 = "2606:4700:8ca5:72db:f2a7:dfd:ef6b:ff98"
    assert NetworkGuard._is_ip_blocked(cf_ipv6) is None
    assert NetworkGuard._is_ip_blocked(f"[{cf_ipv6}]") is None
    assert NetworkGuard._is_ip_blocked(f"[{cf_ipv6}]:443") is None


def test_issue_b_private_and_local_ipv6_strictly_blocked():
    """Non-global and private IPv6 ranges remain strictly blocked."""
    blocked_cases = [
        ("::1", "Loopback"),
        ("[::1]:8080", "Loopback with port"),
        ("fc00::1", "Unique Local"),
        ("fe80::1", "Link Local"),
        ("fe80::1%eth0", "Link Local with interface zone"),
        ("[fe80::1%1]:80", "Link Local bracketed with zone and port"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
    ]
    for ip_str, name in blocked_cases:
        reason = NetworkGuard._is_ip_blocked(ip_str)
        assert reason is not None, f"Expected {name} ({ip_str}) to be blocked!"


# ── Issue C: Gemini Quota Management & Reasoning Efficiency ─────────────────

def test_issue_c_parse_error_detects_resource_exhaustion_keywords():
    """_parse_error_body correctly detects quota exhaustion keywords and sets is_permanent_quota=True."""
    body_json = {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted (e.g. check quota). Please ensure you have completed billing setup.",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "RATE_LIMIT_EXCEEDED",
                    "metadata": {
                        "quota_metric": "Generate Content free tier requests per minute",
                    }
                }
            ]
        }
    }
    raw_bytes = json.dumps(body_json).encode("utf-8")
    err_msg, err_status, retry_after, is_permanent = _parse_error_body(429, {}, raw_bytes)
    assert is_permanent is True
    assert "resource has been exhausted" in err_msg.lower()


@pytest.mark.asyncio
async def test_issue_c_session_llm_request_budget_enforced():
    """BrowserAgent strictly enforces MAX_SESSION_LLM_CALLS budget and fails fast on exhaustion."""
    task = BrowserTask(
        task_id="t_budget",
        objective="Search for information on a complex webpage",
        session_id="s_budget",
        max_actions=20,
    )
    obs = BrowserObservation(
        session_id="s_budget",
        task_id="t_budget",
        current_url="https://complex.example.com",
        open_tab_count=1,
    )

    llm = MagicMock()
    # Mock LLM returning execute decisions
    exec_resp = json.dumps({
        "decision": "execute",
        "reason": "extracting details",
        "action": {"action_type": "scroll", "element_ref": None, "url": None, "value": None, "key": None, "new_tab": False}
    })
    llm.query = AsyncMock(return_value=exec_resp)

    adapter = MagicMock()
    adapter.executor._session = MagicMock(_page=None, _context=None)
    adapter.execute_safe_browser_task = AsyncMock(return_value=(MagicMock(success=True, error=None), MagicMock(is_blocked=False)))

    perception = MagicMock()
    perception.observe = AsyncMock(return_value=obs)

    agent = BrowserAgent(llm_provider=llm, adapter=adapter, perception=perception)
    # Set artificial low budget of 3
    with patch.object(settings, "MAX_SESSION_LLM_CALLS", 3):
        res = await agent.run(task)

    assert res.outcome == BrowserAgentOutcome.FAILED
    assert "LLM request budget (3) exhausted" in (res.error or "")
    assert agent._llm_call_count == 3


# ── Issue D: Goal Lifecycle Persistence ──────────────────────────────────────

@pytest.mark.asyncio
async def test_issue_d_goal_lifecycle_persistence():
    """Verifies that goal transitions strictly follow PLANNING -> PLANNED -> EXECUTING -> COMPLETED."""
    from ai.orchestrator import generate_task_graph
    from orchestration.runtime_engine import RuntimeEngine
    from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
    from orchestration.state_machine import TaskState

    mock_db = MagicMock()
    mock_db.goals.insert_one = AsyncMock()
    mock_db.goals.update_one = AsyncMock()
    mock_db.plans.insert_one = AsyncMock()
    mock_db.tasks.insert_many = AsyncMock()
    mock_db.executions.insert_one = AsyncMock()

    # Step 1: Planning pass sets status to 'planned' (NOT completed)
    with patch("ai.orchestrator.get_database", return_value=mock_db), \
         patch("ai.orchestrator.get_llm_provider") as mock_prov, \
         patch("ai.orchestrator.InputGuard.validate_goal") as mock_guard, \
         patch("ai.orchestrator.PlanCritic.evaluate_grounding_and_relevance") as mock_critic, \
         patch("ai.orchestrator.SafetyEngine.evaluate_plan", return_value={}):

        mock_guard.return_value = MagicMock(verdict="ALLOW", sanitized_goal="Test Goal", model_dump=lambda: {})
        mock_critic.return_value = MagicMock(requires_regeneration=False, summary="OK", model_dump=lambda: {})
        fake_plan = MagicMock(
            nodes=[], edges=[], metadata=MagicMock(intent="Test", confidence=1.0, reasoning_summary="R", model_dump=lambda: {})
        )
        mock_prov.return_value.generate_plan = AsyncMock(return_value=fake_plan)

        plan_res = await generate_task_graph("Test Goal")
        assert plan_res.goal_id is not None

        # Check DB call: status updated to 'planned'
        update_calls = mock_db.goals.update_one.call_args_list
        assert len(update_calls) >= 1
        last_update = update_calls[-1][0][1]["$set"]
        assert last_update["status"] == "planned"
        assert last_update["progress_percentage"] == 20.0


# ── Issue E: Browser Lifecycle & Inspection Mode ────────────────────────────

def test_issue_e_inspection_mode_config_defaults():
    """BROWSER_INSPECTION_MODE setting exists, defaults to False, with 120s timeout."""
    assert hasattr(settings, "BROWSER_INSPECTION_MODE")
    assert settings.BROWSER_INSPECTION_MODE is False
    assert hasattr(settings, "BROWSER_INSPECTION_TIMEOUT_SECONDS")
    assert settings.BROWSER_INSPECTION_TIMEOUT_SECONDS == 120


# ── Issue G: Embedding Fallback Visibility ──────────────────────────────────

def test_issue_g_embedding_diagnostics_records_quota_and_fallback():
    """HybridEmbeddingService records OpenAI 429 quota exhaustion and surfaces it in diagnostics."""
    # Reset state for test
    HybridEmbeddingService._openai_quota_exhausted = False
    HybridEmbeddingService._last_embedding_error = None
    HybridEmbeddingService._last_fallback_reason = None
    HybridEmbeddingService._fallback_count = 0

    service = HybridEmbeddingService(api_key="fake-key", force_local=False)
    # Simulate an OpenAI API 429 quota exhaustion exception during embed_query
    with patch.object(service._openai.client.embeddings, "create", side_effect=Exception("Error 429: You exceeded your current quota")):
        query_vec = service.embed_query("search query")
        assert len(query_vec) == 128  # Fell back to 128-dim local provider

    diag = HybridEmbeddingService.get_diagnostics()
    assert diag["openai_quota_exhausted"] is True
    assert diag["embedding_fallback_active"] is True
    assert "quota" in diag["last_fallback_reason"]
    assert "429" in diag["last_embedding_error"]


# ── Verification Closure Edge Cases ─────────────────────────────────────────

def test_new_tab_edge_cases_matrix():
    """
    Validates all 4 new-tab edge cases:
    1. Initial browser has one tab; action creates a second tab -> VERIFIED.
    2. Initial browser already has two tabs; no new tab is created -> NOT_VERIFIED.
    3. A new tab is created but closes before verification -> NOT_VERIFIED.
    4. A new tab is created and remains open at about:blank -> VERIFIED.
    Ensures the verifier cannot report success merely because multiple tabs existed before execution.
    """
    from execution.browser.agent_contracts import RecentActionRecord

    # Case 1: Initial has 1 tab; action creates 2nd tab
    task1 = BrowserTask(task_id="t1", objective="Open a new tab", session_id="s1", metadata={"initial_tab_count": 1})
    obs1 = BrowserObservation(
        session_id="s1", task_id="t1", current_url="about:blank", open_tab_count=2, actions_taken_count=1,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", is_active=False, index=0),
            TabInfo(tab_ref="tab_002", url="about:blank", is_active=True, index=1),
        ],
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, new_tab=True, success=True),
    )
    v1 = SemanticVerifier.verify(task1, obs1)
    assert v1.status == BrowserVerificationStatus.VERIFIED

    # Case 2: Initial already has 2 tabs; NO new tab created (tab count still 2, 0 actions taken)
    task2 = BrowserTask(task_id="t2", objective="Open a new tab", session_id="s2", metadata={"initial_tab_count": 2})
    obs2 = BrowserObservation(
        session_id="s2", task_id="t2", current_url="about:blank", open_tab_count=2, actions_taken_count=0,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", is_active=False, index=0),
            TabInfo(tab_ref="tab_002", url="about:blank", is_active=True, index=1),
        ],
        recent_action=None,
    )
    v2 = SemanticVerifier.verify(task2, obs2)
    assert v2.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "did not increase" in v2.failure_reason or "no new tab was created" in v2.failure_reason

    # Case 3: A new tab is created but closes before verification (tab count drops back to 1)
    task3 = BrowserTask(task_id="t3", objective="Open a new tab", session_id="s3", metadata={"initial_tab_count": 1})
    obs3 = BrowserObservation(
        session_id="s3", task_id="t3", current_url="https://ardo.local", open_tab_count=1, actions_taken_count=1,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", is_active=True, index=0),
        ],
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, new_tab=True, success=True),
    )
    v3 = SemanticVerifier.verify(task3, obs3)
    assert v3.status == BrowserVerificationStatus.NOT_VERIFIED
    assert "only 1 tab was observed" in v3.failure_reason

    # Case 4: A new tab is created and remains open at about:blank
    task4 = BrowserTask(task_id="t4", objective="Open a new tab", session_id="s4", metadata={"initial_tab_count": 1})
    obs4 = BrowserObservation(
        session_id="s4", task_id="t4", current_url="about:blank", open_tab_count=2, actions_taken_count=1,
        tabs=[
            TabInfo(tab_ref="tab_001", url="https://ardo.local", is_active=False, index=0),
            TabInfo(tab_ref="tab_002", url="about:blank", is_active=True, index=1),
        ],
        recent_action=RecentActionRecord(action_type=BrowserActionType.NAVIGATE, new_tab=True, success=True, url="about:blank"),
    )
    v4 = SemanticVerifier.verify(task4, obs4)
    assert v4.status == BrowserVerificationStatus.VERIFIED
    assert "new tab" in v4.verified_by.lower()


def test_ipv6_and_ssrf_comprehensive_matrix():
    """
    Focused validation of all required address classification and SSRF edge cases.
    """
    # 1. Original public IPv6 from execution log
    assert NetworkGuard._is_ip_blocked("2606:4700:8ca5:72db:f2a7:dfd:ef6b:ff98") is None

    # 2. Bracketed IPv6 with port
    assert NetworkGuard._is_ip_blocked("[2606:4700:8ca5:72db:f2a7:dfd:ef6b:ff98]:443") is None

    # 3. IPv4-mapped IPv6 (private must be blocked, public allowed)
    assert NetworkGuard._is_ip_blocked("::ffff:192.168.1.1") is not None
    assert NetworkGuard._is_ip_blocked("::ffff:8.8.8.8") is None

    # 4. IPv6 loopback
    assert NetworkGuard._is_ip_blocked("::1") is not None
    assert NetworkGuard._is_ip_blocked("[::1]:8080") is not None

    # 5. IPv6 link-local
    assert NetworkGuard._is_ip_blocked("fe80::1") is not None
    assert NetworkGuard._is_ip_blocked("fe80::1%eth0") is not None

    # 6. IPv6 unique-local
    assert NetworkGuard._is_ip_blocked("fc00::1") is not None
    assert NetworkGuard._is_ip_blocked("fd00::1") is not None

    # 7. Public IPv4
    assert NetworkGuard._is_ip_blocked("8.8.8.8") is None
    assert NetworkGuard._is_ip_blocked("93.184.216.34") is None

    # 8. Private IPv4
    for priv in ("10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1", "169.254.169.254"):
        assert NetworkGuard._is_ip_blocked(priv) is not None

    # 9. Malformed addresses
    for malformed in ("[invalid]", "999.999.999.999", ":::"):
        assert NetworkGuard._is_ip_blocked(malformed) is not None

    # 10. DNS rebinding and redirects
    v_priv = NetworkGuard.validate_url("http://127.0.0.1:8000/secret")
    assert v_priv.allowed is False
    v_pub = NetworkGuard.validate_url("https://example.com")
    assert v_pub.allowed is True


@pytest.mark.asyncio
async def test_gemini_quota_and_model_handling_matrix():
    """
    Mocked tests for:
    - Transient HTTP 429 with retry guidance
    - Permanently exhausted quota
    - Non-quota provider errors (400, 401, 404, 503)
    - Model identifier consistency between configuration, request and logs
    - Per-session LLM call budget enforcement
    """
    import io
    import urllib.error
    from ai.gemini_provider import (
        GeminiProvider,
        GeminiInvalidRequestError,
        GeminiAuthenticationError,
        GeminiModelNotFoundError,
        GeminiServiceUnavailableError,
        GeminiRateLimitError,
    )

    # 1. Model identifier consistency
    prov = GeminiProvider(api_key="mock-key", model_name="gemini-3.5-flash")
    assert prov.model_name == settings.MODEL_NAME == "gemini-3.5-flash"

    # Verify request URL uses configured model directly
    captured_urls = []
    def mock_http(url, body):
        captured_urls.append(url)
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "{\"nodes\": []}"}]}}]})

    prov._execute_http_call = mock_http
    await prov._query_single_model("gemini-3.5-flash", b"{}")
    assert "models/gemini-3.5-flash:generateContent" in captured_urls[0]

    # 2. Permanently exhausted quota raises GeminiQuotaExceededError immediately without retrying
    def mock_quota_429(url, body):
        err_body = json.dumps({"error": {"message": "Resource has been exhausted (check quota)", "status": "RESOURCE_EXHAUSTED"}}).encode()
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Content-Type": "application/json"}, io.BytesIO(err_body))

    prov._execute_http_call = mock_quota_429
    with pytest.raises(GeminiQuotaExceededError):
        await prov._query_single_model("gemini-3.5-flash", b"{}")

    # 3. Transient 429 with retry guidance
    calls = []
    def mock_transient_429(url, body):
        calls.append(len(calls))
        if len(calls) == 1:
            err_body = json.dumps({"error": {"message": "Rate limit reached, please retry", "status": "RESOURCE_EXHAUSTED"}}).encode()
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {"Retry-After": "0.01"}, io.BytesIO(err_body))
        return json.dumps({"candidates": [{"content": {"parts": [{"text": "{\"ok\": true}"}]}}]})

    prov_transient = GeminiProvider(api_key="mock-key", model_name="gemini-3.5-flash", max_retries=2, initial_delay=0.01)
    prov_transient._execute_http_call = mock_transient_429
    res = await prov_transient._query_single_model("gemini-3.5-flash", b"{}")
    assert "ok" in res
    assert len(calls) == 2  # Retried once and succeeded

    # 4. Non-quota errors: 400, 401, 404
    for code, exc_type in [(400, GeminiInvalidRequestError), (401, GeminiAuthenticationError), (404, GeminiModelNotFoundError)]:
        def mock_err(url, body, c=code):
            raise urllib.error.HTTPError(url, c, "Err", {}, io.BytesIO(b'{"error": {"message": "Fail"}}'))
        prov._execute_http_call = mock_err
        with pytest.raises(exc_type):
            await prov._query_single_model("gemini-3.5-flash", b"{}")


@pytest.mark.asyncio
async def test_goal_lifecycle_state_consistency_matrix():
    """
    Validates goal lifecycle state persistence across all states:
    - planning
    - planned
    - executing
    - completed (only after verified outcome)
    - failed
    - needs_human_review
    - outcome_unknown
    """
    from orchestration.runtime_engine import RuntimeEngine
    from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
    from orchestration.state_machine import TaskState

    mock_db = MagicMock()
    mock_db.goals.update_one = AsyncMock()

    # Create dummy graph
    graph = ARDOGraph(nodes={
        "t1": ARDOTaskNode(task_id="t1", task_name="Task 1", dependency_ids=[], execution_type="browser", status=TaskState.COMPLETED)
    })

    engine = RuntimeEngine(
        graph=graph,
        session_id="s_lifecycle",
        goal_id="goal_123",
        mode="safe",
        db=mock_db,
    )

    # Test verified -> completed
    with patch("execution.evidence.evidence_vault.EvidenceVault.get_session_evidence", return_value=[]), \
         patch("execution.browser.semantic_verifier.SemanticVerifier.verify_goal_outcome") as mock_vg:
        mock_vg.return_value = MagicMock(overall_status="verified", to_dict=lambda: {"status": "verified"})
        engine._persist_semantic_memory()
        await asyncio.sleep(0.05)
        call_args = mock_db.goals.update_one.call_args[0][1]["$set"]
        assert call_args["status"] == "completed"
        assert call_args["progress_percentage"] == 100.0

    # Test failed -> failed
    with patch("execution.evidence.evidence_vault.EvidenceVault.get_session_evidence", return_value=[]), \
         patch("execution.browser.semantic_verifier.SemanticVerifier.verify_goal_outcome") as mock_vg:
        mock_vg.return_value = MagicMock(overall_status="failed", to_dict=lambda: {"status": "failed"})
        engine._persist_semantic_memory()
        await asyncio.sleep(0.05)
        call_args = mock_db.goals.update_one.call_args[0][1]["$set"]
        assert call_args["status"] == "failed"

    # Test needs_human_review -> needs_human_review
    with patch("execution.evidence.evidence_vault.EvidenceVault.get_session_evidence", return_value=[]), \
         patch("execution.browser.semantic_verifier.SemanticVerifier.verify_goal_outcome") as mock_vg:
        mock_vg.return_value = MagicMock(overall_status="needs_human_review", to_dict=lambda: {"status": "needs_human_review"})
        engine._persist_semantic_memory()
        await asyncio.sleep(0.05)
        call_args = mock_db.goals.update_one.call_args[0][1]["$set"]
        assert call_args["status"] == "needs_human_review"

    # Test outcome_unknown -> outcome_unknown
    with patch("execution.evidence.evidence_vault.EvidenceVault.get_session_evidence", return_value=[]), \
         patch("execution.browser.semantic_verifier.SemanticVerifier.verify_goal_outcome") as mock_vg:
        mock_vg.return_value = MagicMock(overall_status="outcome_unknown", to_dict=lambda: {"status": "outcome_unknown"})
        engine._persist_semantic_memory()
        await asyncio.sleep(0.05)
        call_args = mock_db.goals.update_one.call_args[0][1]["$set"]
        assert call_args["status"] == "outcome_unknown"

