# ROADMAP.md

> **Current Phase**: Not started
> **Milestone**: v1.0

## Must-Haves (from SPEC)
- [ ] 3-panel Comet-style copilot workspace UI
- [ ] Conversational Natural Language goal input
- [ ] Interactive task node graph (React Flow)
- [ ] AI intent extraction and task decomposition
- [ ] User approval, safety validation, and safe browser/app execution

## Phases

### Phase 1: Foundation & UI Shell
**Status**: ✅ Complete
**Objective**: Scaffold Next.js frontend, establish the premium dark theme (Tailwind CSS), and implement the 3-panel UI layout (Chat panel, Center panel, Execution Control panel).
**Requirements**: REQ-01, REQ-05

### Phase 2: AI Orchestration & Decomposition Engine
**Status**: ⬜ Not Started
**Objective**: Build the FastAPI Backend with LangChain/LLMs to receive user goals, extract intent, and decompose them into actionable steps with reasoning.
**Requirements**: REQ-02, REQ-04

### Phase 3: Visual Workflow Engine 
**Status**: ⬜ Not Started
**Objective**: Integrate React Flow into the Center Panel to dynamically render the generated tasks as connected visually premium workflow nodes with tooltips.
**Requirements**: REQ-03, REQ-06

### Phase 4: Execution & Safety Engine
**Status**: ⬜ Not Started
**Objective**: Implement safety validation to block silent destructive actions, and integrate the Playwright execution layer for safe app/browser operations alongside a live execution timeline.
**Requirements**: REQ-07, REQ-08, REQ-09

### Phase 5: Session Memory & Polish
**Status**: ⬜ Not Started
**Objective**: Connect MongoDB layer for session retrieval, optimize semantic matching (Sentence Transformers) for learning from past approvals, and finalize the modern startup-grade UI polish.
**Requirements**: REQ-10
