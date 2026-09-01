"""
test_phase12_ui_integration.py — Phase 12 Production UI & Interactive Console Tests
=====================================================================================
Validates backend REST endpoints, WebSocket streaming, session history, browser evidence,
safety approval flow, and pgvector knowledge grounding required by Phase 12 UI console.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from main import app
from orchestration.scheduler import ARDOScheduler
from execution.browser.browser_models import BrowserAction, BrowserActionResult, BrowserActionType
from orchestration.safety_schemas import ExecutionDecision, RiskLevel, SafetyDecision, PolicyID


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["ardo"] == "active"


def test_knowledge_stats_endpoint(client):
    res = client.get("/knowledge/stats")
    assert res.status_code == 200
    data = res.json()
    assert data["vector_backend"] == "postgresql_pgvector"
    assert "total_knowledge_chunks" in data
    assert "total_historical_memories" in data


def test_orchestration_history_endpoint(client):
    # Record test session
    ARDOScheduler.record_history(
        session_id="ardo_test_hist_1",
        goal="Test Goal for History Validation",
        mode="safe",
        task_count=4,
        status="completed",
        duration_seconds=12.4,
    )

    res = client.get("/orchestrate/history")
    assert res.status_code == 200
    data = res.json()
    assert "history" in data
    assert len(data["history"]) >= 1
    found = next((h for h in data["history"] if h["session_id"] == "ardo_test_hist_1"), None)
    assert found is not None
    assert found["goal"] == "Test Goal for History Validation"
    assert found["task_count"] == 4


@pytest.mark.asyncio
async def test_session_lifecycle_with_evidence_and_approval(client):
    # 1. Build Mock DAG
    plan_nodes = [
        {
            "id": "t1",
            "data": {"label": "Open Internal Dashboard"},
            "browser_action": {
                "action_type": "navigate",
                "url": "http://localhost:8080/dashboard",
                "description": "Navigate to dashboard",
            },
        },
        {
            "id": "t2",
            "data": {"label": "Deploy Service to Staging"},
            "browser_action": {
                "action_type": "click",
                "selector": "#deploy-btn",
                "description": "Click deploy button",
            },
        },
    ]
    plan_edges = [{"source": "t1", "target": "t2"}]
    safety_decisions = {
        "t2": SafetyDecision(
            task_id="t2",
            task_name="Deploy Service to Staging",
            execution_decision=ExecutionDecision.REQUIRE_APPROVAL,
            risk_level=RiskLevel.HIGH,
            triggered_policy_ids=[PolicyID.HIGH_IMPACT_CONFIGURATION],
            explanation="Staging deployment requires human sign-off",
        )
    }

    session_id = "ardo_phase12_test_sess"
    session = await ARDOScheduler.start(
        plan_nodes=plan_nodes,
        plan_edges=plan_edges,
        goal_id="g_phase12",
        session_id=session_id,
        mode="safe",
        safety_decisions=safety_decisions,
    )

    # 2. Verify State Endpoint
    res = client.get(f"/orchestrate/{session_id}/state")
    assert res.status_code == 200
    state = res.json()
    assert state["session_id"] == session_id
    assert len(state["graph_state"]) == 2

    # 3. Simulate evidence on node
    node_t1 = session.graph.get_node("t1")
    node_t1.browser_action = BrowserAction(action_type=BrowserActionType.NAVIGATE, url="http://localhost:8080/dashboard")
    node_t1.browser_result = BrowserActionResult(
        action_type=BrowserActionType.NAVIGATE,
        action=node_t1.browser_action,
        success=True,
        text_extracted="Internal Operations Portal - Sprint 42",
        metadata={"screenshot_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="},
    )

    # 4. Fetch Evidence Endpoint
    res_ev = client.get(f"/orchestrate/{session_id}/evidence")
    assert res_ev.status_code == 200
    ev_data = res_ev.json()
    assert ev_data["session_id"] == session_id
    assert ev_data["evidence_count"] >= 1
    t1_ev = next(e for e in ev_data["evidence_items"] if e["task_id"] == "t1")
    assert t1_ev["result"]["text_extracted"] == "Internal Operations Portal - Sprint 42"
    assert t1_ev["result"]["screenshot_base64"] is not None

    # 5. Approve Task Gate
    res_app = client.post(f"/orchestrate/{session_id}/approve/t2", json={"approved": True})
    assert res_app.status_code == 200
    assert res_app.json()["status"] == "approved"

    # 6. Verify Metrics
    res_m = client.get(f"/orchestrate/{session_id}/metrics")
    assert res_m.status_code == 200


def test_password_masking_in_evidence():
    from orchestration.dag_builder import ARDOGraph, ARDOTaskNode
    from orchestration.state_machine import TaskState

    graph = ARDOGraph()
    node = ARDOTaskNode(task_id="t_sec", task_name="Input Vault Token")
    node.status = TaskState.COMPLETED
    node.browser_action = BrowserAction(
        action_type=BrowserActionType.FILL,
        selector="#input-password-field",
        value="super_secret_token_12345",
    )
    node.browser_result = BrowserActionResult(
        action_type=BrowserActionType.FILL,
        action=node.browser_action,
        success=True,
    )
    graph.nodes["t_sec"] = node

    from orchestration.scheduler import ARDOSession
    session = ARDOSession(session_id="ardo_mask_test", graph=graph, mode="safe")
    evidence = session.get_evidence()

    assert evidence["evidence_count"] == 1
    action = evidence["evidence_items"][0]["action"]
    assert action["value"] == "********"
