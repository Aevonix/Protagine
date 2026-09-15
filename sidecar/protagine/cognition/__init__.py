"""Cognition substrate for Protagine.

Background thinking powered by OpenClaw subagent spawning.
Protagine owns the trigger pipeline, cognition prompt, and API surface.
OpenClaw owns the LLM execution, model routing, and concurrency.
"""

from protagine.cognition.prompt import build_cognition_prompt
from protagine.cognition.trigger import trigger_cognition

__all__ = ["build_cognition_prompt", "trigger_cognition"]
