"""Reasoning module — host-agnostic reasoning loop.

Extracted from protagine-ai's run_agent.py. The ReasoningLoop is the
server-side engine that powers the ``/v1/host/reasoning/turn`` endpoint.
Hosts (OpenClaw, Hermes, etc.) call that endpoint when their plugin
enables ``ownReasoningLoop``; the sidecar runs the actual LLM call +
tool iteration and returns the result.
"""

from protagine.reasoning.loop import ReasoningLoop, ReasoningConfig, ReasoningResult
from protagine.reasoning.executor import ToolExecutor

__all__ = ["ReasoningLoop", "ReasoningConfig", "ReasoningResult", "ToolExecutor"]
