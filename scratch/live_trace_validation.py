import asyncio
import logging
import sys
import os
import traceback

# Ensure backend directory is in pythonpath
sys.path.insert(0, os.path.abspath("backend"))

from core.config import settings
from ai.gemini_provider import GeminiProvider
from ai.orchestrator import generate_task_graph
from orchestration.dag_builder import DAGBuilder
from orchestration.runtime_engine import RuntimeEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("live_trace")

gemini_calls = []

original_query = GeminiProvider.query

async def instrumented_query(self, prompt: str, system_instruction: str = "", json_mode: bool = False, *args, **kwargs):
    stack = traceback.extract_stack()
    caller = "unknown"
    purpose = "unknown"
    
    # Identify caller
    for frame in reversed(stack):
        filename = frame.filename.replace("\\", "/")
        if "orchestrator.py" in filename:
            caller = "Planner / generate_task_graph"
            purpose = "Generate initial task DAG from natural language goal"
            break
        elif "safety_analyzer.py" in filename:
            caller = "SafetyAnalyzer"
            purpose = "Analyze execution safety and assign risk levels"
            break
        elif "browser_agent.py" in filename:
            caller = "BrowserAgent"
            purpose = "Semantic browser interaction reasoning / action selection"
            break
        elif "semantic_verifier.py" in filename:
            caller = "SemanticVerifier"
            purpose = "Semantic verification of browser state"
            break

    call_info = {
        "index": len(gemini_calls) + 1,
        "caller": caller,
        "purpose": purpose,
        "prompt_length": len(prompt),
        "prompt_preview": prompt[:150].replace("\n", " "),
        "model": getattr(self, "model", "default"),
    }
    gemini_calls.append(call_info)
    print(f"\n[TRACE] >>> GEMINI CALL #{call_info['index']}: Caller='{caller}' | Purpose='{purpose}' | Model='{call_info['model']}'", flush=True)
    
    return await original_query(self, prompt, system_instruction, json_mode, *args, **kwargs)

GeminiProvider.query = instrumented_query

async def main():
    goal = "Navigate to https://www.wikipedia.org"
    print(f"\n==================================================", flush=True)
    print(f"STARTING SECOND LIVE VALIDATION: '{goal}'", flush=True)
    print(f"==================================================\n", flush=True)

    # 1. Planning (includes Planner and SafetyAnalyzer)
    print("[1] Generating task graph & safety evaluation...", flush=True)
    plan_response = await generate_task_graph(goal)
    print(f"    Generated plan with {len(plan_response.nodes)} nodes and {len(plan_response.edges)} edges:", flush=True)
    for n in plan_response.nodes:
        print(f"      - Node {n.id}: '{n.data.get('label')}' (type: {n.type}, metadata: {n.data.get('metadata', {})})", flush=True)

    # 2. DAG Construction
    print("\n[2] Building DAG...", flush=True)
    graph = DAGBuilder.build(
        nodes_data=[n.model_dump() for n in plan_response.nodes],
        edges_data=[e.model_dump() for e in plan_response.edges],
    )

    # 3. Runtime Execution
    print("\n[3] Executing RuntimeEngine with BrowserAgent...", flush=True)
    session_id = "live_val_sess_002"
    runtime = RuntimeEngine(
        graph=graph,
        session_id=session_id,
    )
    
    events_received = []
    async for event in runtime.run():
        events_received.append(event)
        event_type = getattr(event, "event_type", None) or getattr(event, "type", str(event))
        task_id = getattr(event, "task_id", None)
        details = getattr(event, "details", {})
        if event_type in ("task_started", "task_completed", "task_failed", "action_executed", "verification_completed", "observation_received", "session_complete"):
            print(f"    [EVENT] {event_type} on {task_id}: {details}", flush=True)

    print(f"\n==================================================", flush=True)
    print(f"LIVE VALIDATION COMPLETE", flush=True)
    print(f"==================================================\n", flush=True)
    print(f"TOTAL GEMINI CALLS: {len(gemini_calls)}\n", flush=True)
    
    print("GEMINI CALL BREAKDOWN:")
    for c in gemini_calls:
        print(f"  Call #{c['index']}: [{c['caller']}] - {c['purpose']} (Prompt length: {c['prompt_length']} chars)", flush=True)

    # Group by caller
    by_caller = {
        "Planner / generate_task_graph": 0,
        "SafetyAnalyzer": 0,
        "BrowserAgent": 0,
        "SemanticVerifier": 0,
        "Any other Gemini caller": 0,
    }
    for c in gemini_calls:
        if c['caller'] in by_caller:
            by_caller[c['caller']] += 1
        else:
            by_caller["Any other Gemini caller"] += 1

    print("\nSUMMARY BY COMPONENT:")
    for caller, count in by_caller.items():
        print(f"  - {caller}: {count}", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
