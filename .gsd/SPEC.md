# SPEC.md — Project Specification

> **Status**: `FINALIZED`

## Vision
An AI-first web application that acts as an Explainable AI Goal-to-Task Planning Copilot, transforming vague, natural language goals into structured, executable, and user-approved visual workflows within a highly interactive, premium aesthetic.

## Goals
1. Convert vague human goals into transparent, well-reasoned, and actionable step-by-step task workflows.
2. Deliver a 3-panel browser/web copilot UI featuring an AI chat interface, a live task workflow graph, and an execution control panel.
3. Validate and safely execute approved actions within the user environment, leveraging session memory to continuously learn and optimize future planning.

## Non-Goals (Out of Scope)
- Building a standard chatbot or generic dashboard interface.
- Autonomous execution of unsafe system modifications or potentially destructive actions without explicit interactive user approval.
- Silent background downloads or hidden operations.

## Users
Professionals, operators, and creatives who require an intelligent, operational web assistant to set up, orchestrate, and execute digital tasks (e.g., coding workspaces, design sessions). Also targeted at product reviewers or evaluators assessing "startup-grade" AI SaaS capabilities.

## Constraints
- **Design Constraints**: Visually premium "startup-grade" design: dark premium theme, glassmorphism, subtle animations, AI glow effects, and professional modern typography (similar to Comet or Arc).
- **Frontend Stack**: Next.js, React, Tailwind CSS, React Flow.
- **Backend Stack**: FastAPI, MongoDB, Motor.
- **AI Stack**: Sentence Transformers, LLM planning layer, LangChain orchestration.
- **Execution Stack**: Playwright, safe application/browser actions.

## Success Criteria
- [ ] The AI reliably extracts intent, required tools, and context from vague goal inputs with accurate logical decomposition.
- [ ] The system visually renders generated plans within a connected graph (React Flow) complete with reasoning tooltips.
- [ ] The user can successfully approve, reorder, or edit steps before initiating safe execution with live progress output.
- [ ] The session memory actively retrieves and applies patterns from past workflows to new identical/similar requests.
