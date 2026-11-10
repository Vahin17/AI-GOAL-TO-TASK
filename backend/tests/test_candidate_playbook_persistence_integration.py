import pytest
import uuid
from core.config import settings
from core.auth_db import AuthDatabase
from knowledge.playbook_service import PlaybookService
from knowledge.semantic_memory import SemanticMemoryStore
from execution.browser.agent_contracts import GoalVerificationResult
from orchestration.dag_builder import ARDOTaskNode, ARDOGraph
from orchestration.state_machine import TaskState


def test_candidate_playbook_and_semantic_memory_live_persistence():
    auth_db = AuthDatabase.get_instance()
    
    # 1. Setup isolated test user
    unique_email = f"test_persist_{uuid.uuid4().hex[:8]}@ardo.ai"
    user = auth_db.create_user(
        email=unique_email,
        full_name="Persist Test User",
        password_hash="secure_hash_123",
    )
    user_id = user["id"]
    test_session_id = f"sess_persist_{uuid.uuid4().hex[:8]}"

    # 2. Build verified graph
    node1 = ARDOTaskNode(
        task_id="t1",
        task_name="Open YouTube",
        status=TaskState.COMPLETED,
    )
    graph = ARDOGraph(nodes={"t1": node1}, session_id=test_session_id)

    goal_verif = GoalVerificationResult(
        session_id=test_session_id,
        goal_text="Play song Yeh Fitoor Mera on YouTube",
        overall_status="verified",
        confidence_score=0.92,
        total_tasks=1,
        verified_tasks=1,
    )

    # 3. Generate candidate playbook
    candidate = PlaybookService.generate_candidate_playbook(
        user_id=user_id,
        session_id=test_session_id,
        goal_text="Play song Yeh Fitoor Mera on YouTube",
        graph=graph,
        goal_verification=goal_verif,
    )

    assert candidate is not None
    playbook_id = candidate["id"]
    assert candidate["status"] == "candidate"
    assert candidate["confidence_score"] == 0.92

    # Verify retrieval from PostgreSQL authoritative store
    retrieved_pb = PlaybookService.get_playbook_by_id(playbook_id, user_id=user_id)
    assert retrieved_pb is not None
    assert retrieved_pb["id"] == playbook_id
    assert retrieved_pb["status"] == "candidate"

    # Promote verified candidate
    promoted = PlaybookService.promote_candidate(playbook_id, user_id=user_id)
    assert promoted is True

    updated_pb = PlaybookService.get_playbook_by_id(playbook_id, user_id=user_id)
    assert updated_pb["status"] == "promoted"
    assert updated_pb["version"] == 2

    # 4. Test Semantic Memory persistence and retrieval
    memory_store = SemanticMemoryStore()
    memory_item = memory_store.record_memory(
        goal_text="Play song Yeh Fitoor Mera on YouTube",
        plan_id=f"plan_{uuid.uuid4().hex[:8]}",
        session_id=test_session_id,
        memory_type="successful_plan",
        task_summary="Navigated to YouTube, searched for Yeh Fitoor Mera, verified playback",
        outcome_status="verified",
    )
    assert memory_item is not None
    assert memory_item.memory_id is not None

    # Retrieve memory
    matches = memory_store.retrieve_memories(
        goal_query="Yeh Fitoor Mera song playback",
        top_k=3,
        min_score=0.1,
    )
    assert len(matches) > 0
    assert any("Yeh Fitoor Mera" in m.goal_text for m in matches)

