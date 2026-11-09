"""
agent_prompts.py — BrowserAgent Gemini Prompt Templates (Phase 3)
=================================================================
Separates prompt text from agent logic for readability and future tuning.

Guidelines for these prompts:
- Keep them compact: the agent calls Gemini on EVERY loop iteration.
- Be explicit about the decision JSON schema.
- Prohibit CSS selector invention and element hallucination.
- Remind the model that observations may change between iterations.
"""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# System prompt (sent once per session; instructs Gemini's role)
# ─────────────────────────────────────────────────────────────────────────────

BROWSER_AGENT_SYSTEM_PROMPT = """You are an autonomous browser agent controlling a real web browser through constrained, safe actions.

YOUR RULES:
1. You receive the current browser observation: URL, title, page text, and a list of interactive elements.
2. Each interactive element has a short stable reference like elem_001, elem_002.
3. Choose EXACTLY ONE next action based on what you currently observe.
4. Use element references (e.g. elem_003) — NEVER invent CSS selectors or XPath.
5. NEVER reference an element that is not listed in the current observation.
6. Do NOT assume a previous action succeeded. Read the observation to verify.
7. Declare FINISH only when the task objective is clearly achieved based on the current URL, title, or page text.
8. If the page state is unexpected or an action failed, choose RECOVER or an alternative action, or FAIL.
9. If a previous action failed or produced an unexpected state, do NOT blindly repeat the identical action. Replan based on the new observation.
10. If the task cannot be completed safely, choose FAIL.

RESPONSE FORMAT (JSON only, no markdown):
{
  "decision": "execute | finish | recover | fail",
  "reason": "one short sentence explaining the decision",
  "action": {
    "action_type": "navigate | click | fill | press | screenshot | extract_text | get_title | get_url | scroll | wait_for_selector",
    "element_ref": "elem_NNN or null",
    "url": "full URL or null",
    "value": "input value or null",
    "key": "key name or null",
    "new_tab": false
  }
}

Set "action" to null when decision is "finish", "fail", or "recover".
The "action_type" field is required when decision is "execute" or "recover".
"""

# ─────────────────────────────────────────────────────────────────────────────
# User prompt template (built per iteration from current state)
# ─────────────────────────────────────────────────────────────────────────────

def build_user_prompt(
    objective: str,
    success_criteria: str | None,
    constraints: list[str],
    observation_summary: str,
    actions_taken: int,
    max_actions: int,
) -> str:
    """
    Build the per-iteration user prompt from current task + observation state.

    Keeps the format compact and token-efficient for repeated Gemini calls.

    Args:
        objective:        The task objective string from BrowserTask.
        success_criteria: Optional human-readable completion condition.
        constraints:      List of constraint strings from BrowserTask.
        observation_summary: Output of BrowserObservation.to_agent_summary().
        actions_taken:    Number of actions already executed this task.
        max_actions:      Maximum allowed actions (from BrowserTask.max_actions).

    Returns:
        Formatted user prompt string.
    """
    lines = [
        f"TASK OBJECTIVE: {objective}",
    ]

    if success_criteria:
        lines.append(f"SUCCESS CRITERIA: {success_criteria}")

    if constraints:
        lines.append(f"CONSTRAINTS: {'; '.join(constraints)}")

    lines.append(f"ACTIONS USED: {actions_taken} / {max_actions}")
    lines.append("")
    lines.append("CURRENT BROWSER STATE:")
    lines.append(observation_summary)
    lines.append("")
    lines.append("Based on the current browser state, choose exactly one next action.")

    return "\n".join(lines)
