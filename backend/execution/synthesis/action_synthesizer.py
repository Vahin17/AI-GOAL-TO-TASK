"""
action_synthesizer.py — RETIRED (Phase 14)
=========================================
ActionSynthesizer was the legacy ahead-of-time (AOT) and just-in-time (JIT)
browser action synthesis engine.

As of Phase 14, ActionSynthesizer is completely retired. All browser actions
and navigation decisions are owned dynamically at runtime by BrowserAgent
driven by live BrowserObservations and Gemini.
"""

from typing import Any, Dict, Optional


class ActionSynthesizer:
    """
    Retired legacy synthesizer (Phase 14).
    Any attempt to instantiate or use ActionSynthesizer raises RuntimeError.
    """

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "ActionSynthesizer was retired in Phase 14. "
            "All browser execution is dynamically owned by BrowserAgent."
        )

    def synthesize_for_task(self, *args, **kwargs) -> None:
        raise RuntimeError(
            "ActionSynthesizer.synthesize_for_task is retired. "
            "Use BrowserAgent for runtime browser actions."
        )
