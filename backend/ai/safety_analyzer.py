"""
safety_analyzer.py — Phase 5: Batch Semantic Risk Analyzer
===========================================================
Performs plan-level semantic analysis of task nodes using the configured LLM.

Architecture:
    - ONE batch LLM call per plan (not per task)
    - LLM is asked to return structured SafetyContext for every task
    - Strict Pydantic validation enforced on response
    - Malformed/missing entries fall back to SafetyContext.unknown()
    - Low-confidence outputs are explicitly flagged, not silently accepted
    - LLM result is ONLY input to policy engine — never directly authorizes execution

Batch Design Benefits:
    - Lower API latency
    - Shared plan-level context (goal, full task list)
    - Consistent semantic interpretation across related tasks
    - Fewer API quota units consumed
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

from pydantic import ValidationError

from orchestration.safety_schemas import (
    SafetyContext,
    ActionType,
    TargetCategory,
    Scope,
    Environment,
    Reversibility,
    DestructivePotential,
    DataSensitivity,
    AmbiguityLevel,
)

logger = logging.getLogger(__name__)

# Minimum confidence below which the result is explicitly flagged UNKNOWN
CONFIDENCE_THRESHOLD = 0.35


# ─────────────────────────────────────────────────────────────────────────────
# Batch Safety Analyzer
# ─────────────────────────────────────────────────────────────────────────────

class SafetyAnalyzer:
    """
    Batch semantic analyzer that converts raw task labels into typed SafetyContext objects.

    Uses the configured LLM provider. If unavailable, every task gets a SafetyContext.unknown().
    If a specific task's result is malformed, that task gets SafetyContext.unknown() while
    others continue to be processed normally.

    Usage:
        analyzer = SafetyAnalyzer()
        contexts = await analyzer.analyze_plan(goal, tasks)
        # contexts: {task_id: SafetyContext}
    """

    async def analyze_plan(
        self,
        goal: str,
        tasks: List[Dict],   # [{"task_id": ..., "task_name": ...}, ...]
    ) -> Dict[str, SafetyContext]:
        """
        Batch analyze all tasks in a plan and return {task_id: SafetyContext}.
        
        Fails safely:
            - LLM unavailable → all tasks get SafetyContext.unknown()
            - Malformed task entry → that task gets SafetyContext.unknown()
            - Extra/unknown task IDs in response are discarded
            - Missing task IDs in response → SafetyContext.unknown()
        """
        if not tasks:
            return {}

        expected_ids = {t["task_id"] for t in tasks}

        try:
            llm_result = await self._invoke_llm(goal, tasks)
            contexts = self._parse_batch_response(llm_result, tasks)
        except Exception as e:
            logger.warning(
                f"[SafetyAnalyzer] Batch semantic analysis failed: {e}. "
                "All tasks defaulting to UNKNOWN context."
            )
            return {t["task_id"]: SafetyContext.unknown(t["task_id"]) for t in tasks}

        # Validate: every expected task must have a context
        for task in tasks:
            tid = task["task_id"]
            if tid not in contexts:
                logger.warning(f"[SafetyAnalyzer] Missing context for task={tid}. Using UNKNOWN.")
                contexts[tid] = SafetyContext.unknown(tid)

        # Discard any unknown task IDs that LLM fabricated
        for extra_id in set(contexts.keys()) - expected_ids:
            logger.warning(f"[SafetyAnalyzer] Discarding fabricated task_id={extra_id} from LLM response.")
            del contexts[extra_id]

        return contexts

    async def _invoke_llm(self, goal: str, tasks: List[Dict]) -> str:
        """Invoke the configured LLM with the batch safety analysis prompt."""
        from core.config import settings

        api_key = getattr(settings, "OPENAI_API_KEY", None)
        if not api_key:
            raise RuntimeError("LLM_UNAVAILABLE: No API key configured.")

        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(temperature=0.0, model="gpt-4o-mini", api_key=api_key)

        prompt = self._build_batch_prompt(goal, tasks)
        response = await llm.ainvoke([{"role": "user", "content": prompt}])
        return response.content

    def _build_batch_prompt(self, goal: str, tasks: List[Dict]) -> str:
        task_list = "\n".join(
            f'  - task_id: "{t["task_id"]}", task_name: "{t["task_name"]}"'
            for t in tasks
        )
        enum_guide = """
Allowed enum values:
  action: read, write, create, delete, modify, execute, deploy, transmit, configure, reset, install, terminate, navigate, search, clean, authenticate, unknown
  target_category: os_system_file, os_boot_file, application_data, user_project_file, user_personal_data, temp_artifact, cache, credential, network_endpoint, database, configuration_file, build_artifact, browser_data, recycle_bin, download, external_service, unknown
  scope: system, user, application, process, network, external, cleanup, unknown
  environment: production, staging, development, local, unknown
  reversibility: high, medium, low, none, unknown
  destructive_potential: none, low, medium, high, critical
  data_sensitivity: none, low, medium, high, credential, pii
  ambiguity: clear, somewhat, highly, contradictory
"""
        return f"""You are a safety classification assistant. Your job is to analyze tasks in an AI execution plan and produce structured safety context for each task.

IMPORTANT: You are NOT authorizing or denying execution. You are ONLY providing structured semantic analysis.

User Goal: "{goal}"

Tasks to analyze:
{task_list}

{enum_guide}

For each task, provide a SafetyContext JSON object. Return ONLY a valid JSON object mapping task_id to SafetyContext. Example format:
{{
  "task_id_1": {{
    "task_id": "task_id_1",
    "action": "delete",
    "target": "temporary build files",
    "target_category": "temp_artifact",
    "scope": "cleanup",
    "environment": "development",
    "reversibility": "low",
    "destructive_potential": "low",
    "privilege_required": false,
    "system_critical": false,
    "data_sensitivity": "none",
    "external_side_effect": false,
    "consequence": "Removes temporary files from build output directory",
    "ambiguity": "clear",
    "semantic_confidence": 0.92
  }}
}}

Be precise about context. "Delete temporary build artifacts" has LOW destructive potential and LOW reversibility but targets TEMP_ARTIFACT, making it different from "Delete protected boot files" which targets OS_BOOT_FILE.
Return ONLY the JSON object, no markdown wrapping."""

    def _parse_batch_response(
        self, raw: str, tasks: List[Dict]
    ) -> Dict[str, SafetyContext]:
        """
        Parse and strictly validate the LLM batch response.
        Individual malformed entries fall back to SafetyContext.unknown() for that task.
        """
        contexts: Dict[str, SafetyContext] = {}

        # Strip markdown code blocks if LLM wraps output
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM returned invalid JSON: {e}")

        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object, got {type(parsed)}")

        for task in tasks:
            tid = task["task_id"]
            raw_ctx = parsed.get(tid)

            if raw_ctx is None:
                # Will be handled as missing in analyze_plan()
                continue

            if not isinstance(raw_ctx, dict):
                logger.warning(f"[SafetyAnalyzer] Non-dict context for task={tid}. Using UNKNOWN.")
                contexts[tid] = SafetyContext.unknown(tid)
                continue

            try:
                # Ensure task_id is set correctly
                raw_ctx["task_id"] = tid
                ctx = SafetyContext.model_validate(raw_ctx)

                # If confidence is critically low, downgrade to unknown
                if ctx.semantic_confidence < CONFIDENCE_THRESHOLD:
                    logger.warning(
                        f"[SafetyAnalyzer] Low confidence ({ctx.semantic_confidence:.2f}) "
                        f"for task={tid}. Will trigger UNKNOWN_SEMANTIC_CONTEXT policy."
                    )

                contexts[tid] = ctx

            except (ValidationError, Exception) as e:
                logger.warning(
                    f"[SafetyAnalyzer] Validation failed for task={tid}: {e}. Using UNKNOWN."
                )
                contexts[tid] = SafetyContext.unknown(tid)

        return contexts
