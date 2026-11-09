"""
execution.synthesis — Legacy synthesis package (ActionSynthesizer retired in Phase 14).
====================================================================================
ActionSynthesizer has been retired in Phase 14 in favor of dynamic runtime execution
owned exclusively by BrowserAgent.
"""

from execution.synthesis.navigation_resolver import NavigationResolver

__all__ = ["NavigationResolver"]
