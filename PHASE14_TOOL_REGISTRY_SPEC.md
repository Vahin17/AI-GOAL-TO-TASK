# Phase 14 — Architecture Specification
## Autonomous Tool Registry & Action Synthesis Layer

**Project:** AI Goal-To-Task Planning Engine  
**Document Version:** 1.0.0  
**Status:** DRAFT / PRE-IMPLEMENTATION SPECIFICATION  
**Author:** Architecture Review & Planning  
**Date:** September 1, 2026  
**Target Execution:** Phase 14  
**Primary Artifact:** `PHASE14_TOOL_REGISTRY_SPEC.md`

---

## 1. Executive Summary & Context

### 1.1 Verified Baseline (Phases 1–13)
The AI Goal-to-Task Planning Engine has successfully verified:
* **Storage & Vectors:** PostgreSQL 16 + pgvector running live in Docker (port 5436) as the single source of truth for knowledge chunks, HNSW vector indexes, and semantic execution memory. ChromaDB is completely eradicated (0 dependencies, 0 imports, 0 shims).
* **Operational Telemetry:** MongoDB Atlas connection pooling for goals, plans, executions, metrics, and safety records.
* **Orchestration (ARDO):** Adaptive Reactive Dynamic Orchestrator with an 8-state deterministic finite state machine, topological cycle detection, multi-factor priority queue, exponential backoff with jitter, starvation protection, and live explainability ("Why This Task Now?").
* **Safety Governance:** Deterministic 12-policy engine evaluating strongly-typed `SafetyContext`. `CRITICAL` risk tasks are blocked at DAG build time, while `REQUIRE_APPROVAL` tasks pause at hash-bound approval gates.
* **Browser Automation:** Headless Chromium automation via Playwright (`backend/execution/browser/playwright_executor.py`), supporting 9 declarative actions with screenshot and telemetry capture.
* **Frontend:** Next.js 16 (React 19) SaaS console with 3-panel layout, React Flow interactive DAG canvas, Zustand 5 state management, and real-time WebSocket event streaming.
* **Test Health:** 134/134 pytest tests passing (100%), 0 TypeScript errors (`npx tsc --noEmit`).

### 1.2 The Core Phase 14 Architectural Gap
While Playwright browser execution is fully operational, it is currently **invoked only when an `ARDOTaskNode` has a pre-populated `browser_action` field**. 
In the current production flow:
1. User enters a goal (e.g., *"Open internal dashboard, search for Phoenix, update project description, and take screenshot"*).
2. The LLM planner (`backend/ai/langchain_provider.py`) decomposes this goal into high-level visual graph nodes (`TaskNode(id, type, position, data={"label": ...})`).
3. The planner does **not** bind tools or synthesize action parameters (URLs, selectors, inputs).
4. As a result, `ARDODagBuilder` creates `ARDOTaskNode` instances where `browser_action is None`.
5. When `RuntimeEngine` iterates the queue, it detects `node.browser_action is None` and diverts to the **simulated task executor** (random 1.5–4.0s sleep and 15% simulated failure rate).

**Phase 14 bridges this gap** by introducing:
1. A **Universal Tool Registry** that registers available capabilities, parameter schemas, safety profiles, and executors.
2. An **Action Synthesis Layer** that transforms high-level task labels into concrete, validated, strongly-typed tool actions prior to execution dispatch.
3. An **Execution Engine Tool Dispatcher** that delegates execution to the Tool Registry while preserving complete backward compatibility with existing tests and simulated fallbacks.

---

## 2. Current Architecture Audit (Read-Only Findings)

### 2.1 Existing Tool-Related Abstractions
* **`backend/execution/browser/browser_models.py`:**
  * `BrowserActionType`: Enum of 9 actions (`NAVIGATE`, `CLICK`, `FILL`, `PRESS`, `WAIT_FOR_SELECTOR`, `EXTRACT_TEXT`, `SCREENSHOT`, `GET_TITLE`, `GET_URL`).
  * `BrowserAction`: Pydantic model with `action_type`, `selector`, `value`, `url`, `key`, `timeout_ms`, `description`.
  * `BrowserActionResult`: Pydantic model with `success`, `action_type`, `text_extracted`, `current_url`, `current_title`, `error`, `duration_ms`, `metadata`.
  * `BrowserWorkflowResult`: Aggregate result model.
* **`backend/orchestration/dag_builder.py`:**
  * `ARDOTaskNode`: Contains `browser_action: Optional[Any] = None` and `browser_result: Optional[Any] = None`.
* **Missing:** No generic `Tool`, `ToolDefinition`, `ToolParameter`, `ToolRegistry`, or `ToolResult` abstraction exists outside the browser package.

### 2.2 Existing Playwright Implementation
* **Driver File:** `backend/execution/browser/playwright_executor.py`
* **Core Class:** `PlaywrightBrowserExecutor`
  * Manages `async_playwright` Chromium lifecycle (`start()`, `stop()`, context with viewport 1280x720, default timeout 5000ms).
  * Executes single actions via `execute_action(action: BrowserAction) -> BrowserActionResult`.
  * Executes multi-step sequences via `execute_workflow(...) -> BrowserWorkflowResult`.
* **Adapter File:** `backend/execution/browser/browser_task_adapter.py`
  * `BrowserTaskAdapter.infer_safety_context(task_id, action) -> SafetyContext`: Translates browser actions into typed safety context.
  * `BrowserTaskAdapter.evaluate_action_safety(task_id, action) -> SafetyDecision`: Evaluates against `SafetyPolicyEngine`.
  * `BrowserTaskAdapter.execute_safe_browser_task(...) -> Tuple[BrowserActionResult, SafetyDecision]`: Enforces policy blocks and approval gates before dispatching to `PlaywrightBrowserExecutor`.

### 2.3 Existing Execution Engine Dispatch
* **File:** `backend/orchestration/runtime_engine.py`
* **Dispatch Location:** `RuntimeEngine._attempt_execution(node: ARDOTaskNode) -> bool` (lines 334–407)
  * **Real Execution Branch (lines 341–365):**
    ```python
    if getattr(node, "browser_action", None) is not None and self.browser_adapter is not None:
        action = node.browser_action
        result, decision = await self.browser_adapter.execute_safe_browser_task(...)
        node.browser_result = result
        ...
        failed = not result.success
    ```
  * **Simulated Execution Branch (lines 366–368):**
    ```python
    else:
        # Simulated success/failure
        failed = random.random() < SIM_FAILURE_RATE
    ```
  * **Simulated Delay (lines 280–284 & 399–401):**
    ```python
    exec_time = random.uniform(SIM_EXEC_TIME_MIN, SIM_EXEC_TIME_MAX)
    node.estimated_execution_time = exec_time
    await asyncio.sleep(exec_time)
    ```

### 2.4 Existing Safety Engine Integration
* **File:** `backend/orchestration/safety_engine.py` & `safety_policy.py`
* **Authorization Flow:**
  1. `SafetyAnalyzer` performs batch LLM semantic analysis on high-level plan nodes (`analyze_plan`).
  2. `SafetyPolicyEngine` deterministically matches `SafetyContext` against 12 hard policies.
  3. `CRITICAL` risk -> `ExecutionDecision.BLOCK` (node starts in `TaskState.BLOCKED`).
  4. `HIGH` risk -> `ExecutionDecision.REQUIRE_APPROVAL` (node pauses in `TaskState.APPROVAL_NEEDED`).
  5. Operator approves via `POST /orchestrate/{session_id}/approve/{task_id}`, creating an `ApprovalBinding` that unlocks the `asyncio.Event` gate.
* **Phase 14 Integration Requirement:** Synthesized tool actions have concrete targets (e.g. `selector="#password"`, `url="https://api.external.com"`). The safety engine must re-evaluate synthesized tool actions using `BrowserTaskAdapter.infer_safety_context` to prevent "safe-label bypass" (e.g. task labeled "Verify login" that synthesizes a credential modification action).

### 2.5 Existing Planner Output & Schema
* **File:** `backend/schemas/models.py`
* **Schemas:** `PlanGraphResponse`, `TaskNode`, `TaskEdge`, `PlanGraphMetadata`.
* **Limitation:** `TaskNode` only contains `id`, `type`, `position`, `data: {"label": "..."}`. It lacks tool definitions, capability requirements, or action parameters.

### 2.6 Existing Frontend & API Layer
* **API:** `POST /orchestrate` starts session; `GET /orchestrate/{id}/evidence` returns screenshots and extracted text; `WS /ws/orchestrate/{id}` streams events.
* **Frontend:** `ExecutionControl.tsx` already has an **Evidence Tab** rendering screenshots and browser action parameters (`actionType`, `selector`, `url`), and a **Safety Approval Panel** for operator interaction.

---

## 3. Phase 14 Architecture Design

### 3.1 Design Principles
1. **Zero Rewrites of Working Drivers:** Keep `PlaywrightBrowserExecutor` and `BrowserTaskAdapter` completely intact. Wrap them in a standardized tool interface.
2. **Deterministic Capability Discovery:** The registry must allow querying tools by domain (`browser`, `http`, `filesystem`, `search`), action type, and required inputs.
3. **Multi-Stage Action Synthesis:**
   - **Stage 1: Intent & Capability Matching:** Determines whether a task requires browser interaction, search, or generic execution.
   - **Stage 2: Action Parameterization:** Produces strongly-typed arguments conforming to the tool's Pydantic input schema.
   - **Stage 3: Parameter Validation:** Validates arguments against schema prior to execution.
   - **Stage 4: Pre-Execution Safety Gate:** Evaluates synthesized action against `SafetyPolicyEngine`.
4. **Resilient Fallback & Non-Breaking Execution:** If a task cannot be synthesized into a registered tool action (e.g. purely conceptual task or LLM synthesis failure), it cleanly falls back to the verified default execution pathway without crashing.
5. **Future-Proof Tool Interface:** Adding a new tool (e.g. `HttpApiTool` or `FileSystemTool`) requires only registering a new `ToolDefinition` subclass, with 0 modifications to `RuntimeEngine`.

---

## 4. Component & Module Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                             PLANNING LAYER                               │
│  User Goal ──► LLM Planner / Fallback Planner ──► PlanGraphResponse     │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         ACTION SYNTHESIS LAYER                           │
│  backend/execution/synthesis/action_synthesizer.py                       │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │ 1. CapabilityMatcher                                                │  │
│  │    Matches TaskNode ("Click Search Button") ──► ToolCapability      │  │
│  └─────────────────────────────────┬──────────────────────────────────┘  │
│                                    │                                     │
│  ┌─────────────────────────────────▼──────────────────────────────────┐  │
│  │ 2. ParameterSynthesizer (LLM / Rule-Based)                         │  │
│  │    Synthesizes arguments: `selector='#search-btn'`, `action='click'` │  │
│  └─────────────────────────────────┬──────────────────────────────────┘  │
│                                    │                                     │
│  ┌─────────────────────────────────▼──────────────────────────────────┐  │
│  │ 3. ActionValidator                                                 │  │
│  │    Validates arguments against ToolDefinition.input_schema         │  │
│  └─────────────────────────────────┬──────────────────────────────────┘  │
│                                    │                                     │
│  ┌─────────────────────────────────▼──────────────────────────────────┐  │
│  │ 4. SafetyBindingEvaluator                                          │  │
│  │    Infers SafetyContext from synthesized action & checks policies  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │ (Attaches ToolCall to ARDOTaskNode)
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          ORCHESTRATION LAYER                             │
│  ARDODagBuilder (Graph Construction) ──► PriorityEngine (Queue Sort)     │
│                                     │                                    │
│                                     ▼                                    │
│  RuntimeEngine._attempt_execution()                                      │
└────────────────────────────────────┬─────────────────────────────────────┘
                                     │
                                     ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                          TOOL REGISTRY LAYER                             │
│  backend/execution/registry/tool_registry.py                             │
│                                                                          │
│  ┌─────────────────────────┐   ┌───────────────────────────┐            │
│  │ ToolRegistry            │   │ ToolDefinition            │            │
│  │ - register(tool)        ├───► - name: str               │            │
│  │ - get(name)             │   │ - capability: ToolCap     │            │
│  │ - find_by_capability()  │   │ - input_schema: Type      │            │
│  │ - dispatch(call)        │   │ - execute(input)          │            │
│  └────────────┬────────────┘   └─────────────▲─────────────┘            │
│               │                              │                           │
│               ▼                              │                           │
│  ┌───────────────────────────────────────────┴────────────────────────┐  │
│  │ BrowserActionTool (Adapter over PlaywrightBrowserExecutor)         │  │
│  │ Actions: navigate, click, fill, extract_text, screenshot, etc.     │  │
│  └───────────────────────────────────────────┬────────────────────────┘  │
└──────────────────────────────────────────────┼───────────────────────────┘
                                               │
                                               ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         PHYSICAL DRIVER LAYER                            │
│  PlaywrightBrowserExecutor (Real Headless Chromium Process)              │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Detailed Class & Interface Specifications

### 5.1 Tool Registry Models & Interfaces
**File:** `backend/execution/registry/tool_models.py`

```python
from __future__ import annotations
from enum import Enum
from typing import Any, Dict, List, Optional, Type
from pydantic import BaseModel, Field

class ToolCapability(str, Enum):
    """Broad capability domains for tool categorization."""
    BROWSER_AUTOMATION = "browser_automation"
    HTTP_REQUEST       = "http_request"
    FILE_SYSTEM        = "file_system"
    DATA_SEARCH        = "data_search"
    SYSTEM_COMMAND     = "system_command"
    CUSTOM             = "custom"

class ToolCall(BaseModel):
    """
    Concrete invocation request bound to an ARDOTaskNode.
    """
    call_id:     str = Field(..., description="Unique ID for this tool call")
    tool_name:   str = Field(..., description="Target tool registered in ToolRegistry")
    action_name: str = Field(..., description="Specific sub-action within the tool")
    arguments:   Dict[str, Any] = Field(default_factory=dict, description="Validated tool arguments")
    timeout_ms:  int = Field(default=5000, description="Execution timeout in milliseconds")
    description: Optional[str] = Field(None, description="Human-readable execution intent")

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()

class ToolResult(BaseModel):
    """
    Unified result returned by any Tool executor.
    """
    call_id:        str
    tool_name:      str
    action_name:    str
    success:        bool
    output_data:    Optional[Any] = None
    extracted_text: Optional[str] = None
    error:          Optional[str] = None
    duration_ms:    float = 0.0
    artifacts:      Dict[str, Any] = Field(default_factory=dict, description="Screenshots, logs, raw data")
    metadata:       Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()
```

**File:** `backend/execution/registry/base_tool.py`

```python
from abc import ABC, abstractmethod
from typing import Any, Dict, Type
from pydantic import BaseModel
from execution.registry.tool_models import ToolCapability, ToolCall, ToolResult
from orchestration.safety_schemas import SafetyContext

class BaseTool(ABC):
    """
    Abstract contract for all executable tools in the engine.
    """
    name: str
    description: str
    capability: ToolCapability
    input_schema: Type[BaseModel]
    output_schema: Type[BaseModel]

    @abstractmethod
    async def execute(self, tool_call: ToolCall) -> ToolResult:
        """Execute the tool call asynchronously and return a standardized ToolResult."""
        pass

    @abstractmethod
    def infer_safety_context(self, task_id: str, tool_call: ToolCall) -> SafetyContext:
        """Map the concrete tool call parameters into typed SafetyContext for policy checking."""
        pass

    def validate_call(self, arguments: Dict[str, Any]) -> BaseModel:
        """Validate raw arguments against the tool's input schema."""
        return self.input_schema(**arguments)
```

**File:** `backend/execution/registry/tool_registry.py`

```python
import logging
from typing import Dict, List, Optional
from execution.registry.base_tool import BaseTool
from execution.registry.tool_models import ToolCapability, ToolCall, ToolResult

logger = logging.getLogger(__name__)

class ToolRegistry:
    """
    Thread-safe central registry managing tool discovery, validation, and dispatch.
    """
    _instance: Optional[ToolRegistry] = None

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    @classmethod
    def get_instance(cls) -> ToolRegistry:
        if cls._instance is None:
            cls._instance = ToolRegistry()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> ToolRegistry:
        cls._instance = ToolRegistry()
        return cls._instance

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            logger.warning(f"[ToolRegistry] Overwriting existing tool: {tool.name}")
        self._tools[tool.name] = tool
        logger.info(f"[ToolRegistry] Registered tool: {tool.name} (capability: {tool.capability.value})")

    def get_tool(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "capability": t.capability.value,
                "schema": t.input_schema.model_json_schema() if hasattr(t.input_schema, "model_json_schema") else {},
            }
            for t in self._tools.values()
        ]

    def find_by_capability(self, capability: ToolCapability) -> List[BaseTool]:
        return [t for t in self._tools.values() if t.capability == capability]

    async def dispatch(self, tool_call: ToolCall) -> ToolResult:
        tool = self.get_tool(tool_call.tool_name)
        if not tool:
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                action_name=tool_call.action_name,
                success=False,
                error=f"TOOL_NOT_FOUND: No tool registered with name '{tool_call.tool_name}'",
            )
        try:
            tool.validate_call(tool_call.arguments)
            return await tool.execute(tool_call)
        except Exception as e:
            logger.error(f"[ToolRegistry] Error executing tool '{tool_call.tool_name}': {e}")
            return ToolResult(
                call_id=tool_call.call_id,
                tool_name=tool_call.tool_name,
                action_name=tool_call.action_name,
                success=False,
                error=f"EXECUTION_ERROR: {str(e)}",
            )
```

---

### 5.2 Playwright Browser Tool Implementation
**File:** `backend/execution/browser/browser_tool.py`

This wraps the already verified `PlaywrightBrowserExecutor` and `BrowserTaskAdapter` into a `BaseTool`:

```python
from __future__ import annotations
from typing import Any, Dict, Optional
from execution.registry.base_tool import BaseTool
from execution.registry.tool_models import ToolCapability, ToolCall, ToolResult
from execution.browser.browser_models import BrowserAction, BrowserActionResult, BrowserActionType
from execution.browser.playwright_executor import PlaywrightBrowserExecutor
from execution.browser.browser_task_adapter import BrowserTaskAdapter
from orchestration.safety_schemas import SafetyContext

class BrowserTool(BaseTool):
    """
    Playwright Chromium browser automation tool conforming to BaseTool.
    """
    name = "browser"
    description = "Executes safe, sandboxed browser actions (navigate, click, fill, extract, screenshot) using Chromium."
    capability = ToolCapability.BROWSER_AUTOMATION
    input_schema = BrowserAction
    output_schema = BrowserActionResult

    def __init__(self, executor: Optional[PlaywrightBrowserExecutor] = None, adapter: Optional[BrowserTaskAdapter] = None):
        self.executor = executor or PlaywrightBrowserExecutor(headless=True)
        self.adapter = adapter or BrowserTaskAdapter(executor=self.executor)

    def infer_safety_context(self, task_id: str, tool_call: ToolCall) -> SafetyContext:
        action = BrowserAction(
            action_type=BrowserActionType(tool_call.action_name),
            **tool_call.arguments
        )
        return BrowserTaskAdapter.infer_safety_context(task_id, action)

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        action = BrowserAction(
            action_type=BrowserActionType(tool_call.action_name),
            **tool_call.arguments
        )
        action_res = await self.executor.execute_action(action)
        
        artifacts = {}
        if action.action_type == BrowserActionType.SCREENSHOT and "screenshot_size_bytes" in action_res.metadata:
            artifacts["screenshot"] = action_res.metadata

        return ToolResult(
            call_id=tool_call.call_id,
            tool_name=self.name,
            action_name=tool_call.action_name,
            success=action_res.success,
            extracted_text=action_res.text_extracted,
            error=action_res.error,
            duration_ms=action_res.duration_ms,
            artifacts=artifacts,
            metadata={
                "current_url": action_res.current_url,
                "current_title": action_res.current_title,
                **action_res.metadata
            }
        )
```

---

### 5.3 Action Synthesis Engine
**File:** `backend/execution/synthesis/action_synthesizer.py`

Responsible for analyzing task nodes in the plan and synthesizing concrete `ToolCall` instances.

```python
from __future__ import annotations
import logging
import uuid
import re
from typing import Dict, List, Optional, Tuple
from schemas.models import TaskNode
from execution.registry.tool_models import ToolCall, ToolCapability
from execution.registry.tool_registry import ToolRegistry
from execution.browser.browser_models import BrowserActionType

logger = logging.getLogger(__name__)

class ActionSynthesizer:
    """
    Transforms high-level task labels into concrete, validated ToolCalls.
    
    Supports:
    1. Deterministic heuristic pattern synthesis (instant, zero-token cost, resilient).
    2. LLM-based structured parameterization (when complex selectors or inputs are required).
    """

    def __init__(self, registry: Optional[ToolRegistry] = None):
        self.registry = registry or ToolRegistry.get_instance()

    def synthesize_for_task(
        self,
        task_id: str,
        task_label: str,
        goal_text: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolCall]:
        """
        Main entry point: analyzes task_label and goal to synthesize a concrete ToolCall.
        Returns None if the task is non-executable or generic (which uses simulation runner).
        """
        # 1. Deterministic Heuristic Matcher
        heuristic_call = self._heuristic_synthesis(task_id, task_label, goal_text, context)
        if heuristic_call:
            return heuristic_call

        # 2. If no heuristic matches and LLM available, perform semantic synthesis
        # (Reserved for Phase 14 LLM parameter synthesis)
        return None

    def _heuristic_synthesis(
        self,
        task_id: str,
        label: str,
        goal_text: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolCall]:
        """
        Fast deterministic synthesis for common browser actions.
        """
        label_lower = label.lower()

        # NAVIGATE: "Navigate to http...", "Open dashboard...", "Visit URL..."
        if any(k in label_lower for k in ("navigate", "open", "visit", "go to")):
            url_match = re.search(r'(https?://[^\s]+|file://[^\s]+)', label)
            target_url = url_match.group(0) if url_match else (context.get("url") if context else None)
            if not target_url and "dashboard" in label_lower and context and "dashboard_url" in context:
                target_url = context["dashboard_url"]

            if target_url:
                return ToolCall(
                    call_id=f"call_{uuid.uuid4().hex[:8]}",
                    tool_name="browser",
                    action_name=BrowserActionType.NAVIGATE.value,
                    arguments={"url": target_url},
                    description=label,
                )

        # CLICK: "Click #search-btn", "Submit form", "Click button..."
        if any(k in label_lower for k in ("click", "submit", "press button")):
            selector_match = re.search(r'(#[a-zA-Z0-9_-]+|\.[a-zA-Z0-9_-]+|button\[[^\]]+\])', label)
            selector = selector_match.group(0) if selector_match else None
            if selector:
                return ToolCall(
                    call_id=f"call_{uuid.uuid4().hex[:8]}",
                    tool_name="browser",
                    action_name=BrowserActionType.CLICK.value,
                    arguments={"selector": selector},
                    description=label,
                )

        # FILL: "Fill #input with ...", "Type ... into #selector"
        if any(k in label_lower for k in ("fill", "type", "enter", "input")):
            selector_match = re.search(r'(#[a-zA-Z0-9_-]+|\.[a-zA-Z0-9_-]+|input\[[^\]]+\])', label)
            selector = selector_match.group(0) if selector_match else None
            # Extract value from quotes or after "with"
            val_match = re.search(r'["\']([^"\']+)["\']', label)
            val = val_match.group(1) if val_match else "Test Input"
            if selector:
                return ToolCall(
                    call_id=f"call_{uuid.uuid4().hex[:8]}",
                    tool_name="browser",
                    action_name=BrowserActionType.FILL.value,
                    arguments={"selector": selector, "value": val},
                    description=label,
                )

        # EXTRACT: "Extract text from #selector", "Get text..."
        if any(k in label_lower for k in ("extract", "scrape", "read text")):
            selector_match = re.search(r'(#[a-zA-Z0-9_-]+|\.[a-zA-Z0-9_-]+)', label)
            selector = selector_match.group(0) if selector_match else None
            if selector:
                return ToolCall(
                    call_id=f"call_{uuid.uuid4().hex[:8]}",
                    tool_name="browser",
                    action_name=BrowserActionType.EXTRACT_TEXT.value,
                    arguments={"selector": selector},
                    description=label,
                )

        # SCREENSHOT: "Take screenshot", "Capture page"
        if any(k in label_lower for k in ("screenshot", "capture screen")):
            return ToolCall(
                call_id=f"call_{uuid.uuid4().hex[:8]}",
                tool_name="browser",
                action_name=BrowserActionType.SCREENSHOT.value,
                arguments={},
                description=label,
            )

        return None
```

---

### 5.4 ARDOTaskNode and Graph Builder Extension
In `backend/orchestration/dag_builder.py`:
1. Add `tool_call: Optional[ToolCall] = None` and `tool_result: Optional[ToolResult] = None` to `ARDOTaskNode`.
2. Keep `browser_action` and `browser_result` for backward compatibility. If `tool_call` is a browser tool, automatically populate `node.browser_action`:
   ```python
   if node.tool_call and node.tool_call.tool_name == "browser":
       node.browser_action = BrowserAction(
           action_type=BrowserActionType(node.tool_call.action_name),
           **node.tool_call.arguments
       )
   ```
3. In `ARDODagBuilder.from_plan_graph()`:
   Call `ActionSynthesizer.synthesize_for_task(node_id, label, goal_text)`:
   If a tool call is synthesized:
   - Bind `ardo_node.tool_call = tool_call`
   - Re-evaluate safety with `tool.infer_safety_context()` to ensure synthesized actions never bypass policy gates!

---

### 5.5 RuntimeEngine Integration
In `backend/orchestration/runtime_engine.py`:
Update `_attempt_execution()`:
```python
async def _attempt_execution(self, node: ARDOTaskNode) -> bool:
    while True:
        # 1. Execute via ToolRegistry if tool_call is present
        if getattr(node, "tool_call", None) is not None:
            registry = ToolRegistry.get_instance()
            tool = registry.get_tool(node.tool_call.tool_name)
            
            # Pre-execution safety evaluation
            if tool:
                s_ctx = tool.infer_safety_context(node.task_id, node.tool_call)
                decision = self.browser_adapter.safety_policy_engine.evaluate(
                    s_ctx, node.tool_call.description or node.task_name
                )
                if decision.is_blocked:
                    node.status = TaskStateMachine.transition(node.status, TaskState.FAILED, node.task_id)
                    node.execution_logs.append(f"BLOCKED BY SAFETY POLICY: {decision.explanation}")
                    return False
                if decision.execution_decision == ExecutionDecision.REQUIRE_APPROVAL:
                    if not self.browser_adapter.safety_engine.is_approved(self.session_id, node.task_id, node.task_name):
                        node.execution_logs.append("APPROVAL_REQUIRED before tool execution.")
                        return False

            tool_result = await registry.dispatch(node.tool_call)
            node.tool_result = tool_result
            if node.tool_call.tool_name == "browser":
                node.browser_result = tool_result  # Sync for UI evidence compatibility
            node.execution_logs.append(
                f"Tool '{node.tool_call.tool_name}.{node.tool_call.action_name}': success={tool_result.success}, duration={tool_result.duration_ms:.1f}ms"
            )
            failed = not tool_result.success

        # 2. Legacy/Backward Compatible Browser Action (Phase 10 & 11 tests)
        elif getattr(node, "browser_action", None) is not None and self.browser_adapter is not None:
            result, decision = await self.browser_adapter.execute_safe_browser_task(
                task_id=node.task_id,
                session_id=self.session_id,
                action=node.browser_action,
            )
            node.browser_result = result
            if decision.is_blocked:
                node.status = TaskStateMachine.transition(node.status, TaskState.FAILED, node.task_id)
                return False
            failed = not result.success

        # 3. Simulated Execution Fallback for generic/non-tool tasks
        else:
            failed = random.random() < SIM_FAILURE_RATE

        # Common retry / success handling follows existing ARDO state machine transitions
        ...
```

---

## 6. Data Flow Architecture

```
User Input Goal: "Open dashboard and search for Phoenix"
                       │
                       ▼
1. Plan Generation:
   PlanGraphResponse(
     nodes=[
       TaskNode(id="n1", label="Navigate to http://localhost:8080/dashboard"),
       TaskNode(id="n2", label="Fill #project-search-input with 'Phoenix'"),
       TaskNode(id="n3", label="Click #project-search-btn")
     ]
   )
                       │
                       ▼
2. Action Synthesis:
   ActionSynthesizer.synthesize_for_task("n1")
   ──► ToolCall(tool_name="browser", action="navigate", arguments={"url": "http://localhost:8080/dashboard"})
   ActionSynthesizer.synthesize_for_task("n2")
   ──► ToolCall(tool_name="browser", action="fill", arguments={"selector": "#project-search-input", "value": "Phoenix"})
   ActionSynthesizer.synthesize_for_task("n3")
   ──► ToolCall(tool_name="browser", action="click", arguments={"selector": "#project-search-btn"})
                       │
                       ▼
3. Pre-Execution Safety Verification:
   BrowserTool.infer_safety_context("n2", tool_call) 
   ──► SafetyContext(action="write", target="#project-search-input", target_category="application_data")
   SafetyPolicyEngine.evaluate(s_ctx) ──► SafetyDecision(ALLOW)
                       │
                       ▼
4. DAG Construction & Priority Sorting:
   ARDOGraph populated with tool_call and synced browser_action
                       │
                       ▼
5. Runtime Execution & Dispatch:
   RuntimeEngine dispatches tool_call to ToolRegistry.dispatch()
   ──► BrowserTool.execute() ──► PlaywrightBrowserExecutor.execute_action()
                       │
                       ▼
6. Telemetry & Memory Persistence:
   ToolResult stored in node, events emitted over WebSocket,
   and execution outcome persisted to PostgreSQL Semantic Memory.
```

---

## 7. Required File Changes

### 7.1 New Files to Create

| File Path | Component | Responsibility |
|:---|:---|:---|
| `backend/execution/registry/__init__.py` | Module init | Package exposure for tool registry. |
| `backend/execution/registry/tool_models.py` | Models | `ToolCapability`, `ToolCall`, `ToolResult`. |
| `backend/execution/registry/base_tool.py` | Abstract Base | `BaseTool` contract and schema validation. |
| `backend/execution/registry/tool_registry.py` | Singleton Registry | Discovery, storage, validation, dispatch. |
| `backend/execution/browser/browser_tool.py` | Adapter Tool | Conforms `PlaywrightBrowserExecutor` to `BaseTool`. |
| `backend/execution/synthesis/__init__.py` | Module init | Package exposure for synthesis layer. |
| `backend/execution/synthesis/action_synthesizer.py` | Synthesizer | Task label to `ToolCall` synthesis engine. |
| `backend/tests/test_phase14_tool_registry.py` | Test Suite | Unit & integration tests for registry & synthesis. |

### 7.2 Modifications to Existing Files

| File Path | Scope of Modification |
|:---|:---|
| `backend/orchestration/dag_builder.py` | Add `tool_call` & `tool_result` to `ARDOTaskNode`. In `from_plan_graph`, integrate `ActionSynthesizer` and sync `browser_action`. |
| `backend/orchestration/runtime_engine.py` | Update `_attempt_execution()` to query `ToolRegistry` first before falling back to legacy browser action or simulator. |
| `backend/orchestration/scheduler.py` | In `ARDOSession.__init__`, register `BrowserTool` in `ToolRegistry` on startup. |
| `backend/main.py` | In startup event, initialize `ToolRegistry` and register built-in `BrowserTool`. |
| `backend/api/router.py` | Add optional diagnostic endpoint `GET /tools` exposing registered tool metadata. |

---

## 8. Safety & Risk Governance

### 8.1 Prevention of "Safe Label" Bypass
A major vulnerability in AI agent architectures is prompt injection where a task label looks harmless (e.g., *"Clean up temporary user files"*), but the synthesized action targets critical system files or credentials (e.g., `selector="#admin-password"`, `value="hacked"`).
* **Guaranteed Invariant:** `Tool.infer_safety_context(task_id, tool_call)` extracts the **actual concrete parameters** of the synthesized call.
* If `selector="#admin-password"`, `BrowserTool` sets `target_category = TargetCategory.CREDENTIAL` and `data_sensitivity = DataSensitivity.CREDENTIAL`.
* The `SafetyPolicyEngine` evaluates this against Policy `CREDENTIAL_MODIFICATION` and issues `ExecutionDecision.BLOCK`.
* The `RuntimeEngine` intercepts the block and marks the task `TaskState.FAILED`, preventing Playwright from ever dispatching the action.

---

## 9. API & Frontend Integration

### 9.1 API Changes
* **New Endpoint:** `GET /tools`
  * Returns list of registered tools, capabilities, and JSON schemas for developer inspection and frontend capability discovery.
* **Existing Endpoints:** No breaking changes. `POST /plan`, `POST /orchestrate`, `GET /orchestrate/{id}/state`, and `GET /orchestrate/{id}/evidence` remain 100% signature-compatible.

### 9.2 Frontend Changes
* **Zero Breaking Changes:** Because `BrowserTool` synchronizes its execution artifacts to `node.browser_result`, the existing `EvidenceTab` in `ExecutionControl.tsx` will automatically render screenshots and logs without frontend code modifications.

---

## 10. Database Schema Compatibility

* **PostgreSQL + pgvector:** No schema changes required. `execution_memories` stores `task_summary` and `metadata JSONB`, which seamlessly accommodates tool call summaries.
* **MongoDB Atlas:** No schema changes required. `orchestration_logs` and `task_runtime_state` store flexible BSON documents that capture `tool_call` and `tool_result`.

---

## 11. Test Strategy & Verification Plan

A new dedicated test suite `backend/tests/test_phase14_tool_registry.py` will validate:
1. **Registry Operations:** Registering, retrieving, querying by capability, and handling non-existent tools.
2. **Schema Validation:** Rejecting malformed parameters (e.g. `BrowserAction` missing URL on `NAVIGATE`).
3. **BrowserTool Conformance:** Executing real Playwright Chromium actions through the `ToolRegistry.dispatch()` interface.
4. **Action Synthesis:** Deterministic synthesis of `NAVIGATE`, `CLICK`, `FILL`, `EXTRACT_TEXT`, and `SCREENSHOT` from high-level labels.
5. **Safety Invariance:** Proving that synthesized actions targeting credentials or protected resources are deterministically BLOCKED before dispatch.
6. **Simulated Fallback Preservation:** Proving that tasks with no tool match still execute cleanly through the simulator.
7. **Full Regression Suite:** Ensuring all existing 134 backend tests pass with 0 regressions.

---

## 12. Regression Risk & Backward Compatibility

| Risk Area | Mitigation Strategy |
|:---|:---|
| **Existing Phase 10 & 11 Browser Tests** | `ARDOTaskNode` retains `browser_action`. `_attempt_execution()` checks `tool_call` first, then falls back to `browser_action`. Both paths remain active. |
| **Existing Phase 4 ARDO Scheduling Tests** | Tasks without tool calls continue through the exact simulated runner code paths, preserving test timings and state transitions. |
| **Safety Engine Policies** | `SafetyPolicyEngine` logic is completely untouched; only the source of `SafetyContext` is enriched with concrete tool parameters. |
| **PostgreSQL / pgvector Storage** | Zero database modifications; all schemas and HNSW indexes remain untouched. |

---

## 13. Explicit List of Things That MUST NOT Be Changed

1. **DO NOT replace or rewrite `PlaywrightBrowserExecutor`:** The existing Chromium management code is fully verified and working.
2. **DO NOT change ARDO State Machine Transitions:** The 8 states (`PENDING`, `WAITING`, `ACTIVE`, `RETRYING`, `COMPLETED`, `FAILED`, `BLOCKED`, `APPROVAL_NEEDED`) and allowed transitions are frozen.
3. **DO NOT weaken the Safety Policy Engine:** Policies `PROTECTED_SYSTEM_RESOURCE`, `CREDENTIAL_MODIFICATION`, etc., must remain deterministic hard blocks.
4. **DO NOT reintroduce ChromaDB:** PostgreSQL + pgvector is the sole vector database.
5. **DO NOT break existing test fixtures:** `browser_test_app.html` and `internal_dashboard.html` must remain the canonical local test targets.
6. **DO NOT implement speculative external tools in Phase 14:** Do not build real Bash/Shell, AWS, or Gmail tools in this phase; design the registry to support them, but implement only the `BrowserTool`.

---

## 14. Definition of Done (DoD) for Phase 14

* [ ] `backend/execution/registry/` implemented with `BaseTool`, `ToolRegistry`, `ToolCall`, `ToolResult`.
* [ ] `backend/execution/browser/browser_tool.py` wraps Playwright execution into `BaseTool`.
* [ ] `backend/execution/synthesis/action_synthesizer.py` synthesizes concrete actions from high-level task labels.
* [ ] `RuntimeEngine` dispatches tasks through `ToolRegistry`.
* [ ] Safety policies evaluate concrete synthesized tool arguments before execution.
* [ ] New test suite `test_phase14_tool_registry.py` passes 100%.
* [ ] Full backend regression test suite passes (>= 140 passing tests, 0 failures).
* [ ] Frontend TypeScript check passes with 0 errors (`npx tsc --noEmit`).
* [ ] End-to-end user goal with browser steps executes real Chromium actions automatically without manual `browser_action` binding in test code.
