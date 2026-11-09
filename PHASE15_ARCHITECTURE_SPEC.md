# Phase 15 Architecture Specification
## Autonomous Environment Perception & Semantic Action Synthesis Layer

**Project:** AI Goal-To-Task Planning Engine  
**Author:** Antigravity Architect & Engineer  
**Date:** September 1, 2026  
**Document:** `PHASE15_ARCHITECTURE_SPEC.md`  
**Status:** **READ-ONLY ARCHITECTURE SPECIFICATION (PRE-IMPLEMENTATION)**  
**Target Milestone:** Phase 15  

---

## 1. Executive Summary

Phase 14 established the engine's core tool infrastructure: the abstract `BaseTool` contract, the centralized `ToolRegistry`, the `BrowserTool` wrapping Playwright Chromium, deterministic heuristic `ActionSynthesizer`, concrete-parameter safety governance, and prioritized runtime dispatch. All 152 backend tests pass, Playwright end-to-end automation is verified live, and 0 frontend TypeScript errors remain.

However, a rigorous audit reveals the **central bottleneck preventing true agentic autonomy**:
The system currently executes in a **perceptually blind, open-loop manner**:
1. **Perception Deficit:** The browser executor has no capability to inspect the active DOM, accessibility tree, or interactive element layout (`<button>`, `<input>`, `<a>`, `<select>`). Once navigation completes, the engine is blind to what is on the screen.
2. **Synthesis Deficit:** `ActionSynthesizer` relies entirely on deterministic regex matching for hardcoded CSS selectors (`#id`, `.class`). If a planner outputs a natural language task (e.g., *"Search for project Phoenix on dashboard"*), the regex finds no explicit `#id` selector and returns `None`, causing the runtime to fall back to simulated execution (sleeping 1.5–4.0 seconds).
3. **Closed-Loop Deficit:** There is no perception-action feedback loop. The engine cannot inspect a live web page, discover that a search input has selector `#project-search-input`, synthesize the action to fill it, submit the form, and observe whether the expected project appeared.

**Phase 15 directly solves this bottleneck** by introducing:
1. **Environment Perception Engine:** Live, token-efficient DOM and accessibility tree inspection in `PlaywrightBrowserExecutor` and `BrowserTool` (`INSPECT_PAGE` / `EXTRACT_INTERACTIVE_ELEMENTS`).
2. **Semantic / LLM-Assisted Action Synthesis:** Context-aware parameter synthesis combining high-level task intent with live perceived element candidates into validated `ToolCall`s.
3. **Closed-Loop Step Verification:** Post-execution verification confirming that DOM state transitioned as expected before transitioning ARDO task states to `COMPLETED`.

---

## 2. Complete Current-State Inventory (Post-Phase 14 Baseline)

### A. AI / Planning
* **LLM Providers (`backend/ai/`):** `LangChainProvider` wrapping OpenAI `ChatOpenAI(model="gpt-4o-mini")`. Deterministic fallback planner (`backend/ai/fallback_planner.py`) generating rule-based DAGs when LLM is unavailable or unconfigured.
* **Task Decomposition & DAG Generation:** Plans decomposed into nodes and edges matching React Flow schemas (`PlanGraphResponse`). Topological ordering enforced.
* **RAG Pipeline (`backend/rag/`):** Context assembly with token budget bounds (1,500 tokens), chunking, metadata filtering, and provenance tracking.
* **Semantic Memory (`backend/knowledge/`):** Persistent vector memory using PostgreSQL 16 + pgvector (`knowledge_chunks` and `execution_memories` tables with HNSW cosine indexes). Fully verified across restarts. ChromaDB completely removed (0 references).

### B. Orchestration
* **ARDO State Machine (`backend/orchestration/`):** 8-state model (`PENDING`, `WAITING`, `ACTIVE`, `RETRYING`, `COMPLETED`, `FAILED`, `BLOCKED`, `APPROVAL_NEEDED`).
* **Scheduler & Priority Engine:** Dynamic priority scoring, dependency resolution, concurrency limiting (`max_concurrency`), starvation prevention, and aging boosts.
* **Runtime Engine:** Asynchronous dispatch with 3-tier precedence (`tool_call` → legacy `browser_action` → simulated execution fallback).
* **Fault Injection & Resilience:** Exponential backoff with jitter, retry budgets (default max 3 retries), reactive rescheduling for failed branches.
* **WebSocket Streaming:** Real-time event broadcasting (`task_started`, `task_completed`, `task_failed`, `approval_needed`, `evidence_captured`).

### C. Tool System (`backend/execution/registry/`)
* **`ToolCapability` Enums:** `BROWSER_AUTOMATION`, `HTTP_REQUEST`, `FILE_SYSTEM`, `DATA_SEARCH`, `SYSTEM_COMMAND`, `CUSTOM`.
* **`ToolCall` & `ToolResult` Models:** Strongly typed Pydantic models with parameter validation, execution timing, output extraction, artifacts, and metadata.
* **`BaseTool` Interface:** Abstract base class defining `name`, `description`, `capability`, `input_schema`, `output_schema`, async `execute()`, and `infer_safety_context()`.
* **`ToolRegistry` Singleton:** Thread-safe singleton supporting registration, retrieval, capability discovery, schema validation, and structured exception containment.
* **Current Registrations:** `BrowserTool` is the sole registered tool in production startup (`backend/main.py`) and session initialization (`backend/orchestration/scheduler.py`).

### D. Action Synthesis (`backend/execution/synthesis/`)
* **Deterministic Synthesis:** Regex-based heuristic parser for 7 browser action types (`NAVIGATE`, `CLICK`, `FILL`, `EXTRACT_TEXT`, `SCREENSHOT`, `GET_TITLE`, `WAIT_FOR_SELECTOR`).
* **Limitations:** Only succeeds if task labels contain explicitly formatted URLs (`https?://`, `file://`) or CSS selectors (`#...`, `....`).
* **Autonomy Level:** Rule-bound and brittle. Unstructured natural-language tasks return `None`.
* **LLM Synthesis:** Architected as a stub in Phase 14; not yet integrated.

### E. Browser Automation (`backend/execution/browser/`)
* **Driver:** Playwright Chromium headless executor (`PlaywrightBrowserExecutor`) managing browser context, page lifecycle, timeouts, and sandboxing (`--no-sandbox`, `--disable-dev-shm-usage`).
* **Supported Actions:** `NAVIGATE`, `CLICK`, `FILL`, `PRESS`, `WAIT_FOR_SELECTOR`, `EXTRACT_TEXT`, `SCREENSHOT`, `GET_TITLE`, `GET_URL`.
* **Missing Capabilities:** No page inspection, no DOM snapshot extraction, no accessibility tree distillation, no interactive element cataloging.
* **Safety Integration:** `BrowserTaskAdapter` derives typed `SafetyContext` (target URL, domain category, credential risk) from concrete action arguments.

### F. Safety Governance (`backend/safety/`)
* **Engine:** Hybrid two-layer safety engine: deterministic policies (`SafetyPolicyEngine`) + LLM semantic analyzer (`SafetyAnalyzer`).
* **Invariants:**
  1. Critical tasks (`BLOCKED`) can never be overridden or approved.
  2. Concrete tool arguments are evaluated prior to execution (`dag_builder.py` and `runtime_engine.py`).
  3. Credential modification targets (`#password`, `secret`, `token`) are unconditionally blocked at build time even if high-level task labels appear harmless.
  4. Cryptographic approval tokens bind specific task IDs, payload hashes, and session IDs.

### G. Database & Persistence
* **PostgreSQL 16 + pgvector (Port 5436 in Docker):** Single source of truth for vector embeddings (128 dimensions), knowledge chunks, and execution memories with HNSW cosine indexes.
* **MongoDB Atlas:** Operational execution telemetry, historical run tracking, and aggregate metrics.

### H. Frontend
* **Stack:** Next.js 14, React 18, Zustand state management, React Flow DAG canvas.
* **Capabilities:** Real-time WebSocket synchronization, task state visualization, interactive approval gate modal, evidence tab with screenshot viewer and extracted text inspector.
* **TypeScript Check:** `npx tsc --noEmit` passes with 0 errors.

### I. API (`backend/api/router.py`)
* **Endpoints:**
  - `POST /plan`: Generates DAG from user goal.
  - `POST /orchestrate/start`: Initiates execution session.
  - `GET /orchestrate/{session_id}/state`: Retrieves real-time ARDO state.
  - `POST /orchestrate/{session_id}/approve/{task_id}`: Submits approval token.
  - `GET /orchestrate/{session_id}/evidence`: Retrieves captured browser screenshots and artifacts.
  - `GET /tools`: Diagnostic inspection of registered tools, capabilities, and schemas.

---

## 3. Tool Coverage Matrix

| Capability | Abstraction Exists | Real Implementation | Registered | Tested | Autonomous Execution | Missing Functionality |
|:---|:---:|:---:|:---:|:---:|:---:|:---|
| **Browser Automation** | YES (`BaseTool`) | YES (`Playwright`) | YES (`ToolRegistry`) | YES (18 tests) | PARTIAL (Regex only) | Page inspection, dynamic selector discovery |
| **HTTP/REST Requests** | YES (`ToolCapability.HTTP_REQUEST`) | NO | NO | NO | NO | Entire `HttpApiTool` class & execution logic |
| **Search / Information**| YES (`ToolCapability.DATA_SEARCH`) | NO | NO | NO | NO | Search tool & query adapter |
| **Filesystem Access** | YES (`ToolCapability.FILE_SYSTEM`) | NO | NO | NO | NO | Local file read/write tool |
| **Database Access** | NO | NO | NO | NO | NO | Ad-hoc query tool (PG store is internal only) |
| **Email / SMTP** | NO | NO | NO | NO | NO | Email tool |
| **Calendar** | NO | NO | NO | NO | NO | Calendar integration |
| **Shell / System Commands**| YES (`ToolCapability.SYSTEM_COMMAND`) | NO | NO | NO | NO | Terminal/CLI execution tool |
| **Document Processing** | NO | NO | NO | NO | NO | PDF/DOCX parser tool |
| **Data Extraction** | PARTIAL (Browser text) | PARTIAL (DOM inner_text) | PARTIAL (Via browser) | YES | PARTIAL | Structured JSON/table scraping |
| **External SaaS** | NO | NO | NO | NO | NO | Third-party API wrappers |
| **Custom User Tools** | YES (`ToolCapability.CUSTOM`) | NO | NO | NO | NO | Dynamic plugin loading |

*Summary:* The only implemented, registered, and tested tool capability is **Browser Automation**. All other capabilities currently exist solely as enum values in `ToolCapability`.

---

## 4. Autonomy Audit: Detailed Lifecycle Trace

Let us trace the complete execution flow for a realistic natural-language goal:
> *"Log in to the staging portal, find project Phoenix, and capture its current sprint status."*

| Stage | Expected Agentic Behavior | Current Implementation State | Mechanism | Autonomy Assessment |
|:---|:---|:---|:---|:---|
| **1. Goal Input** | Receive natural language goal | IMPLEMENTED | User input via `/plan` | **Autonomous** |
| **2. LLM Plan** | Decompose goal into high-level subtasks | IMPLEMENTED | `LangChainProvider` / fallback | **Autonomous** |
| **3. Task Labels** | Output semantic labels: *"Navigate to portal"*, *"Search Phoenix"*, *"Verify status"* | IMPLEMENTED | DAG generation in `ai/orchestrator.py` | **Autonomous** |
| **4. Capability ID** | Identify that tasks require `BROWSER_AUTOMATION` | PARTIALLY IMPLEMENTED | Regex keywords in `ActionSynthesizer` | **Rule-Bound / Fragile** |
| **5. Tool Selection** | Query `ToolRegistry.find_by_capability()` | IMPLEMENTED | Default mapping to `browser` tool | **Deterministic** |
| **6. Parameter Synthesis** | Extract selectors, URLs, and values | **FAILED ON NATURAL GOALS** | Regex checks for `#id` and quotes; fails if missing | **STOPS AUTONOMY** |
| **7. Validation** | Validate parameters against `BrowserAction` | IMPLEMENTED | Pydantic schema validation in `ToolRegistry` | **Deterministic** |
| **8. Safety Check** | Re-evaluate concrete parameters against policies | IMPLEMENTED | `SafetyPolicyEngine` evaluates target category | **Autonomous Governance** |
| **9. Execution** | Perform physical action in Chromium | IMPLEMENTED | `PlaywrightBrowserExecutor` | **Real Physical Execution** |
| **10. Observation** | Inspect page DOM to discover available inputs/buttons | **MISSING** | No DOM inspection or accessibility extraction | **BLIND** |
| **11. Result Interpretation**| Determine if action achieved desired state change | **MISSING** | `node.status` set to `COMPLETED` solely if driver didn't crash | **NAIVE** |
| **12. Next Action** | Adapt next task parameters based on observed output | **MISSING** | DAG nodes are static and immutable | **OPEN-LOOP** |
| **13. Completion** | Finalize goal with verified evidence | PARTIALLY IMPLEMENTED | Collects screenshot/logs into `get_evidence()` | **Passive Logging** |

### Where Autonomy Stops:
Autonomy abruptly halts at **Stage 6 (Parameter Synthesis)** and **Stage 10 (Environment Observation)**. 
Unless the prompt author explicitly inserts CSS selectors (e.g., `#project-search-input`) directly into the task label string, synthesis returns `None`, and the engine silently drops back into simulation mode. The agent cannot discover elements on the page.

---

## 5. Real-World Execution Audit

### Test Case:
Goal: *"Open a web application, find a project, update its description, verify the change, take a screenshot, and report the result."*

1. **Step 1: Open web application**
   - *Status:* **REAL**. Synthesizer extracts URL via regex; Chromium navigates to URL; captures title and duration.
2. **Step 2: Find project (fill search input & click search)**
   - *Status:* **SIMULATED unless hardcoded**. If label is `"Search for project Phoenix"`, synthesis returns `None` → Simulator runs for 2.5s. If label is `"Fill #project-search-input with 'Phoenix'"` and `"Click #project-search-btn"`, it runs real Playwright.
3. **Step 3: Update its description (click edit, type text, click save)**
   - *Status:* **SIMULATED unless hardcoded**. Requires 3 hardcoded selectors (`#edit-btn`, `#desc-textarea`, `#save-btn`).
4. **Step 4: Verify the change**
   - *Status:* **SIMULATED**. No assertion engine checks whether `#desc-cell` inner text matches the updated text.
5. **Step 5: Take screenshot**
   - *Status:* **REAL**. Captures PNG bytes, records size, and attaches base64 artifact.
6. **Step 6: Report the result**
   - *Status:* **REAL**. Attaches to `node.tool_result` and broadcasts via WebSocket.

### Biggest Limitation:
**The engine cannot observe the browser environment to determine what selectors exist.** The developer or user must act as the "DOM inspector" ahead of time and embed CSS selectors into the natural language prompt. This contradicts the fundamental definition of an autonomous agent.

---

## 6. Gap Analysis

| Priority | Architectural Gap | Description & Code Evidence | Why It Matters | Dependencies |
|:---:|:---|:---|:---|:---|
| **P0** | **Environment Perception Deficit** | `PlaywrightBrowserExecutor` lacks DOM/accessibility inspection. (Lines 112–235 of `playwright_executor.py` only execute actions, never inspect page elements). | Without perception, dynamic selector resolution is mathematically impossible. | Builds on Phase 10–14 Playwright integration |
| **P0** | **Semantic / LLM Action Synthesis Deficit** | `ActionSynthesizer` relies solely on regex (Lines 45–140 of `action_synthesizer.py`). Non-regex tasks return `None` and fall back to simulation. | Prevents natural-language goals from driving real browser actions. | Builds on Phase 14 `ToolCall` and `ActionSynthesizer` |
| **P1** | **Closed-Loop Verification Deficit** | `RuntimeEngine._attempt_execution` marks task `COMPLETED` if tool returns `success=True`, without verifying expected DOM state change. | Tasks can falsely succeed even if an action was rejected by the web app (e.g. form validation failure). | Builds on Perception Engine |
| **P2** | **Multi-Tool Expansion Deficit** | Registry only has `BrowserTool`. No `HttpApiTool` or `FileSystemTool`. | Limits real-world agent tasks that need API calls or local file operations. | Builds on Tool Registry |
| **P3** | **Dynamic ARDO Mid-Flight Graph Mutation** | ARDO graph is static AOT. Cannot inject recovery nodes during runtime. | If an unexpected popup appears, graph cannot dynamically insert a "Dismiss Modal" node. | Builds on ARDO Scheduler |

---

## 7. Analysis of Candidate Directions for Phase 15

### Candidate A: Autonomous Environment Perception & Semantic Action Synthesis (RECOMMENDED)
* **Title:** Autonomous Environment Perception & Semantic Action Synthesis
* **Objective:** Equip `BrowserTool` with live page inspection (`INSPECT_PAGE` returning interactive elements, accessibility tree, and visible text), and equip `ActionSynthesizer` with LLM-assisted semantic grounding to map natural language intent into concrete selectors against live page observations.
* **Pros:**
  - Solves the single biggest blocker in the repository (the P0 perception & synthesis bottleneck).
  - Directly converts existing Phase 10–14 Playwright infrastructure into a genuinely autonomous web agent.
  - Zero database schema changes.
  - Zero breaking changes to ARDO state machine or safety governance.
* **Cons:**
  - Requires structured LLM calls during synthesis when regex heuristics are insufficient.
* **Why it MUST be Phase 15:** It bridges the fatal gap between high-level planning and physical execution.

### Candidate B: Multi-Tool Expansion (HTTP/API & Filesystem Tools)
* **Title:** Multi-Tool Expansion: HTTP API & Filesystem Execution
* **Objective:** Implement `HttpApiTool` and `FileSystemTool` and register them in `ToolRegistry`.
* **Pros:** Broadens tool capabilities horizontally.
* **Cons:**
  - Merely adds more tools that will suffer from the exact same limitation: without semantic synthesis, HTTP tool will only work if URLs, headers, and bodies are hardcoded in task labels.
  - Leaves the browser agent completely blind and brittle.
* **Why it should NOT be Phase 15:** Broadening tool breadth before solving tool autonomy compounds architectural debt.

### Candidate C: Dynamic Subgraph Replanning in ARDO
* **Title:** Dynamic ARDO Subgraph Replanning & Mid-Flight Branch Mutation
* **Objective:** Allow `RuntimeEngine` to mutate the DAG at runtime based on task failures or intermediate observations.
* **Pros:** Allows flexible branching.
* **Cons:**
  - Extremely high risk of violating ARDO 8-state machine invariants and topological sort guarantees.
  - Unnecessary if individual tasks can intelligently perceive and execute their steps.
* **Why it should NOT be Phase 15:** High complexity with low immediate benefit while the agent is still blind to page DOM.

### Candidate D: Multi-Modal Vision Grounding (VLM Coordinates)
* **Title:** Visual Perception & Multi-Modal Coordinate Grounding
* **Objective:** Take full-page screenshots and use a Vision-Language Model to output (x, y) click coordinates.
* **Pros:** Can interact with `<canvas>` and image elements.
* **Cons:**
  - Massive token cost (thousands of tokens per image).
  - High latency (2–5 seconds per step).
  - Brittle to viewport zooming and screen scaling.
  - Playwright already has direct DOM access which is instant, 100% accurate, and token-free.
* **Why it should NOT be Phase 15:** Premature optimization and cost-inefficient compared to DOM accessibility inspection.

---

## 8. Detailed Specification of the Recommended Phase 15

### Core Definition
```
PHASE 15 TITLE:
Autonomous Environment Perception & Semantic Action Synthesis Layer

PHASE 15 OBJECTIVE:
Transform the AI Goal-To-Task Planning Engine from a blind, regex-dependent executor 
into an intelligent, perceptually-grounded agent by implementing live browser page 
inspection (DOM/accessibility tree distillation), LLM-assisted semantic action synthesis, 
and closed-loop post-condition verification while preserving strict concrete-parameter 
safety governance.
```

### Why Now:
Phase 14 built the tool plumbing (`ToolRegistry`, `BrowserTool`, `ToolCall`, `ToolResult`). The plumbing works (152/152 tests passing). But the agent cannot feed realistic goals into the plumbing because it cannot see the page DOM. Phase 15 provides the "eyes" and "semantic brain" for the existing tool limbs.

---

## 9. Architectural Design

```
+-----------------------------------------------------------------------------------+
|                                 USER GOAL                                         |
|      "Log into staging portal, find project Phoenix, verify active status"        |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                              LANGCHAIN PLANNER                                    |
|   Node 1: Navigate to http://localhost:8080/dashboard                             |
|   Node 2: Search for project Phoenix                                              |
|   Node 3: Verify project status is Active                                         |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                               DAG BUILDER                                         |
|   - Regex Heuristic Matcher: Node 1 -> ToolCall(browser, navigate, url=...)       |
|   - Semantic Nodes 2 & 3: Deferred to Runtime Contextual Synthesis                |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                        ARDO RUNTIME ENGINE (PERCEPTION LOOP)                      |
|                                                                                   |
|  [Step 1: Execute Node 1]                                                         |
|  Dispatch Navigate -> Page Loads -> State: ACTIVE -> COMPLETED                    |
|                                                                                   |
|  [Step 2: Execute Node 2 ("Search for project Phoenix")]                          |
|  1. PERCEIVE: Call BrowserTool.inspect_page()                                     |
|     => Returns distilled DOM:                                                     |
|        - input#project-search-input (placeholder='Search projects...', type='text')|
|        - button#project-search-btn (text='Search Project')                        |
|  2. SEMANTIC SYNTHESIS: ActionSynthesizer.synthesize_with_perception()            |
|     => Input: Intent ("Search for project Phoenix") + Perceived Elements          |
|     => Output: ToolCall(browser, fill, selector='#project-search-input', val='...')|
|  3. SAFETY RE-EVALUATION: Evaluate concrete selector against SafetyPolicyEngine   |
|     => Safe! (TargetCategory.DOM_ELEMENT, not CREDENTIAL)                         |
|  4. EXECUTE & OBSERVE:                                                            |
|     => Fill input -> Click search -> Verify DOM updated                           |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                          EVIDENCE & TELEMETRY                                     |
|  - Screenshots attached to ToolResult                                             |
|  - Extracted DOM text attached to node                                            |
|  - Real-time WebSocket event broadcast to Frontend Evidence Tab                   |
+-----------------------------------------------------------------------------------+
```

---

## 10. Component Specifications

### 10.1 Environment Perception Engine (`backend/execution/browser/page_inspector.py`)
Provides fast, structured, token-efficient extraction of actionable page elements directly via Playwright's JavaScript execution context:
* **Interactive Elements Distillation:**
  - Evaluates in-page JS extracting visible: `input`, `button`, `select`, `textarea`, `a[href]`, and elements with `role="button"`, `role="link"`, `role="searchbox"`.
  - Filters out hidden (`display: none`, `visibility: hidden`, `opacity: 0`, 0x0 bounding box).
  - Returns for each element:
    - `tag`: e.g., `"input"`
    - `element_id`: e.g., `"project-search-input"`
    - `name`: e.g., `"query"`
    - `element_type`: e.g., `"text"`
    - `placeholder`: e.g., `"Search projects..."`
    - `text`: inner text or accessible label (truncated to 60 chars)
    - `selector`: optimal unique CSS selector (preferring `#id`, then `name`, then accessible role/aria-label)
* **Accessibility Tree Snapshot:**
  - Uses Playwright's `page.accessibility.snapshot()` to produce a compact tree of landmarks, headings, and controls.
* **Token Budget Control:**
  - Enforces strict limits: max 40 interactive elements, max 1,000 tokens total representation. Never dumps full raw HTML.

### 10.2 Page Inspection Model (`backend/execution/browser/inspection_models.py`)
```python
class PerceivedElement(BaseModel):
    selector: str
    tag: str
    element_id: Optional[str] = None
    element_type: Optional[str] = None
    placeholder: Optional[str] = None
    text: Optional[str] = None
    is_interactive: bool = True

class PageSnapshot(BaseModel):
    url: str
    title: str
    interactive_elements: List[PerceivedElement] = Field(default_factory=list)
    heading_hierarchy: List[str] = Field(default_factory=list)
    status_code: Optional[int] = 200
    timestamp: float = Field(default_factory=time.time)
```

### 10.3 BrowserTool Extension (`backend/execution/browser/browser_tool.py`)
Add `INSPECT_PAGE` action type to `BrowserActionType`:
* Direct execution via `PlaywrightBrowserExecutor.inspect_page()` returning `PageSnapshot`.
* Exposes `BrowserTool.inspect_page()` returning structured `PageSnapshot`.

### 10.4 Semantic Action Synthesizer (`backend/execution/synthesis/semantic_synthesizer.py`)
Extends `ActionSynthesizer` with LLM-assisted grounding:
* If deterministic heuristic matches (e.g. explicit URL or `#id`), use heuristic (0 tokens, 0ms latency).
* If heuristic returns `None` AND an active page snapshot is provided:
  - Formats a compact structured prompt containing:
    1. Task Intent: e.g., `"Search for project Phoenix"`
    2. Available Elements: compact list of candidate selectors with labels and placeholders
  - Invokes `ChatOpenAI(model="gpt-4o-mini", temperature=0.0)` with a strict Pydantic output schema `SynthesizedActionProposal`.
  - Converts proposal into typed `ToolCall`.
  - If LLM call fails or times out (2.0s limit), falls back to fuzzy token matching against element labels/placeholders.

### 10.5 Closed-Loop Post-Condition Verification
In `RuntimeEngine`:
* Following action execution, the runtime can optionally perform post-execution verification:
  - For `NAVIGATE`: verifies `page.url` or `page.title` changed.
  - For `FILL`: verifies element value matches input.
  - For `CLICK`: verifies navigation or DOM mutation occurred.
  - For `EXTRACT_TEXT`: verifies extracted text is non-empty and matches target criteria.

---

## 11. Safety Governance Invariants in Phase 15

Phase 15 strictly adheres to the non-negotiable safety invariant:
```
CONCRETE SYNTHESIZED PARAMETERS MUST ALWAYS BE EVALUATED BY SAFETY POLICIES 
BEFORE DISPATCH TO PLAYWRIGHT.
```
1. **Perception Safety:** `INSPECT_PAGE` is strictly read-only (`ActionType.READ`). It has zero external side effects and does not require human approval.
2. **Credential Protection:** If `SemanticActionSynthesizer` matches a task like `"Enter password"` to selector `#user-password`, the synthesized `ToolCall` is passed through `tool.infer_safety_context()`. The target `#user-password` is categorized as `TargetCategory.CREDENTIAL`. `SafetyPolicyEngine` unconditionally blocks it (`TaskState.BLOCKED`). The physical browser never touches it without human sign-off.
3. **Destructive Action Protection:** Buttons with text matching `"Delete"`, `"Drop"`, `"Terminate"`, `"Purge"` trigger `RiskLevel.HIGH` and require explicit human approval token before execution.

---

## 12. Database & Persistence Impact

```
================================================================================
POSTGRESQL SCHEMA CHANGES:    NONE (0 changes required)
PGVECTOR IMPACT:              NONE (128-dim embeddings remain untouched)
HNSW INDEXES:                 REUSED AS-IS (cosine similarity on memories)
MONGODB TELEMETRY CHANGES:    NONE (Records PageSnapshot metadata in logs)
================================================================================
```
Phase 15 utilizes the existing `execution_memories` table to store successful action synthesis patterns (Intent → Selector mapping), enabling semantic memory retrieval to accelerate future synthesis without LLM calls!

---

## 13. API & Frontend Impact

### API Additions
* Add `POST /orchestrate/{session_id}/inspect`: Diagnostic endpoint returning current `PageSnapshot` for active browser session.

### Frontend Integration
* **Evidence Tab (`frontend/src/components/ExecutionControl.tsx`):**
  - Displays detected interactive elements in an expandable "Page Inspection" drawer alongside screenshots and extracted text.
  - Zero breaking changes to existing Zustand stores (`executionStore.ts`, `evidenceStore.ts`).

---

## 14. Implementation Boundaries

### Phase 15 MUST Implement:
1. `PageInspector` in `backend/execution/browser/page_inspector.py` generating compact `PageSnapshot` from live Chromium.
2. `INSPECT_PAGE` action type in `BrowserActionType` and `PlaywrightBrowserExecutor`.
3. `inspect_page()` method on `BrowserTool` conforming to `BaseTool`.
4. `SemanticActionSynthesizer` in `backend/execution/synthesis/semantic_synthesizer.py` implementing prompt grounding with `gpt-4o-mini` and deterministic fuzzy fallback.
5. Integration of live perception into `ActionSynthesizer.synthesize_for_task()`.
6. Integration into `RuntimeEngine` execution loop (perceive before synthesis for natural-language tasks).
7. Pre-execution safety evaluation of all semantically synthesized tool parameters.
8. Comprehensive test suite (`tests/test_phase15_perception_synthesis.py`).

### Phase 15 MUST NOT Implement:
1. NO new tool types (no HTTP tool, no filesystem tool, no shell tool).
2. NO database migrations or schema updates.
3. NO modifications to ARDO 8-state machine transitions.
4. NO modifications to `PlaywrightBrowserExecutor` core browser launch/shutdown lifecycle.
5. NO VLM/pixel-coordinate vision grounding.
6. NO mid-flight DAG topology restructuring.

---

## 15. Testing & Verification Strategy

### Required Test Suite (`tests/test_phase15_perception_synthesis.py`)
1. **`test_15_1_page_inspector_interactive_elements`:** Verifies detection of inputs, buttons, and links on `internal_dashboard.html`.
2. **`test_15_2_page_inspector_hidden_element_filtering`:** Confirms hidden/collapsed elements are excluded.
3. **`test_15_3_page_inspector_token_budget_bound`:** Confirms snapshot size stays under 1,000 tokens.
4. **`test_15_4_browser_tool_inspect_action`:** Verifies `BrowserTool.execute(INSPECT_PAGE)`.
5. **`test_15_5_semantic_synthesis_search_input`:** Natural label *"Search for Phoenix"* maps to `#project-search-input` without hardcoded selectors.
6. **`test_15_6_semantic_synthesis_click_button`:** Natural label *"Submit search"* maps to `#project-search-btn`.
7. **`test_15_7_semantic_synthesis_llm_fallback_resilience`:** When LLM is simulated down, fuzzy keyword matcher successfully resolves obvious selectors.
8. **`test_15_8_safety_governance_on_semantically_resolved_credential`:** Natural label *"Enter admin credentials"* resolving to `#admin-password` is intercepted and blocked by `SafetyPolicyEngine`.
9. **`test_15_9_end_to_end_natural_language_browser_workflow`:** Full autonomous execution of a 3-step natural language goal on `internal_dashboard.html` with zero developer-authored selectors in the plan.
10. **`test_15_10_regression_152_tests`:** Full regression suite confirming 152 existing tests pass with 0 regressions.

---

## 16. Long-Term Architectural Roadmap

```
PHASE 14 (COMPLETED):
  Tool Registry Architecture, BrowserTool Conformance & Deterministic Action Synthesis.

PHASE 15 (SPECIFIED HERE):
  Autonomous Environment Perception & Semantic Action Synthesis Layer.
  (Page inspection, DOM distillation, LLM-assisted parameter synthesis, closed-loop verification).

PHASE 16:
  Multi-Tool Expansion & System Orchestration.
  (HttpApiTool for REST services, FileSystemTool for workspace files, cross-tool data piping).

PHASE 17:
  Dynamic Runtime Adaptation & Adaptive Subgraph Replanning.
  (Mid-flight DAG branching, conditional node execution, error-recovery subtrees in ARDO).

PHASE 18:
  Multi-Modal Visual Grounding & Canvas/Non-DOM Automation.
  (VLM screenshot perception, visual verification, pixel-space fallbacks).

PHASE 19:
  Autonomous Multi-Agent Collaboration & Parallel Execution Pools.
  (Specialized subagents for Planning, Research, Browser Execution, and Code Review).

PHASE 20:
  Full Production Hardening, Multi-Tenant Isolation & Sandbox Enterprise Release.
```

---

## 17. Final Architecture Summary

```
================================================================================
CURRENT MATURITY:            Robust infrastructure, verified tool registry,
                             deterministic safety governance, but perceptually
                             blind and dependent on hardcoded selectors in labels.

CURRENT BIGGEST BOTTLENECK:  Perception Deficit: The engine cannot inspect page
                             DOM to discover selectors, and ActionSynthesizer
                             fails on natural language without hardcoded CSS #ids.

RECOMMENDED PHASE 15:        Autonomous Environment Perception &
                             Semantic Action Synthesis Layer.

WHY:                         It directly bridges high-level semantic intent to
                             physical execution without requiring human-authored
                             CSS selectors.

WHAT IT UNLOCKS:             True end-to-end autonomous web execution from
                             arbitrary natural-language user goals.

DATABASE / SCHEMA CHANGES:   NONE (0 changes to PostgreSQL, MongoDB, or pgvector).
FRONTEND BREAKING CHANGES:   NONE (0 breaking changes; purely additive evidence).
================================================================================
```
