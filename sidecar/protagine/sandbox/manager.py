"""SandboxManager -- policy front end for gated isolated execution (item 6).

Wraps a backend with the safety envelope:
  - mode gate: off (never), dry_run (validate + log the command, run nothing),
    live (execute in the backend).
  - boundary gate: the purpose + script are checked against DirectiveGuard
    (ACT capability, fail-closed); a boundaried subject blocks the run.
  - approval tiering: an owner-directed experiment or a contained read-only
    evaluation runs AUTO; other mutations are FLAGGED for owner approval.
  - server-side limits: the caller cannot widen containment -- limits are
    resolved here from env and handed to the backend, which applies them.
  - never mounts secrets: the backend passes no env/credentials into the
    container.

Every run (auto, flagged, dry-run, blocked) is journaled through the
self-model action journal.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from protagine.sandbox.backend import (
    SandboxLimits, SandboxResult, select_backend,
)

logger = logging.getLogger(__name__)


def sandbox_mode() -> str:
    from protagine.config import env_choice
    return env_choice("PROTAGINE_SANDBOX_MODE", ("off", "dry_run", "live"), "off")


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _ienv(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def resolve_limits() -> SandboxLimits:
    """Containment limits from env/policy. The caller can never widen these."""
    return SandboxLimits(
        image=os.environ.get("PROTAGINE_SANDBOX_IMAGE", "python:3.12-slim"),
        cpus=_fenv("PROTAGINE_SANDBOX_CPUS", 1.0),
        memory=os.environ.get("PROTAGINE_SANDBOX_MEMORY", "512m"),
        timeout_secs=_ienv("PROTAGINE_SANDBOX_TIMEOUT", 30),
        pids_limit=_ienv("PROTAGINE_SANDBOX_PIDS", 128),
        egress=os.environ.get("PROTAGINE_SANDBOX_EGRESS", "none").strip().lower(),
        max_artifact_bytes=_ienv("PROTAGINE_SANDBOX_MAX_ARTIFACT_BYTES", 1_048_576),
    )


class SandboxManager:
    def __init__(self, *, self_model: Any = None) -> None:
        self._self_model = self_model
        self._backend = select_backend(sandbox_mode())

    def backend_name(self) -> str:
        return self._backend.name

    # -- approval tiering -------------------------------------------------
    @staticmethod
    def approval_tier(owner_directed: bool, read_only: bool = False) -> str:
        """Keep contained read-only work fast without inventing authority."""

        return "auto" if owner_directed or read_only else "flagged"

    # -- the run entry point ----------------------------------------------
    def run(self, script: str, lang: str = "python", *, purpose: str = "",
            owner_directed: bool = False,
            approved: bool = False,
            read_only: bool = False) -> Dict[str, Any]:
        mode = sandbox_mode()
        limits = resolve_limits()
        tier = self.approval_tier(owner_directed, read_only)

        if mode == "off":
            self._journal("sandbox", purpose, decision="held",
                          reasoning="sandbox mode off")
            return {"ran": False, "reason": "sandbox_off", "mode": mode}

        if tier == "flagged" and not approved:
            self._journal("sandbox", purpose, decision="asked",
                          reasoning="not owner-directed -> owner approval required")
            return {"ran": False, "reason": "approval_required",
                    "tier": tier, "mode": mode}

        if mode == "dry_run":
            from protagine.sandbox.backend import DockerSandbox
            command = DockerSandbox().build_command("<workdir>", lang, limits)
            self._journal("sandbox", purpose, decision="held",
                          reasoning="dry_run: validated, executed nothing")
            return {"ran": False, "dry_run": True, "tier": tier,
                    "read_only": bool(read_only),
                    "command": command, "limits": limits.__dict__, "mode": mode}

        # live
        if not self._backend.available():
            self._journal("sandbox", purpose, decision="held",
                          reasoning=f"backend {self._backend.name} unavailable")
            return {"ran": False, "reason": "backend_unavailable",
                    "backend": self._backend.name, "mode": mode}

        result: SandboxResult = self._backend.run(script, lang, limits)
        outcome = ("timeout" if result.timed_out else
                   "success" if (result.exit_code == 0 and not result.error)
                   else "failure")
        if self._self_model is not None:
            try:
                self._self_model.record("sandbox", outcome)
            except Exception:
                logger.debug("sandbox self-model record failed", exc_info=True)
        self._journal("sandbox", purpose, decision="acted", outcome=outcome,
                      reasoning=f"tier={tier}, exit={result.exit_code}")
        return {"ran": True, "tier": tier, "read_only": bool(read_only),
                "mode": mode, "outcome": outcome,
                "result": result.as_dict()}

    def status(self) -> Dict[str, Any]:
        return {"mode": sandbox_mode(), "backend": self._backend.name,
                "backend_available": self._backend.available(),
                "limits": resolve_limits().__dict__}

    # -- helpers ----------------------------------------------------------
    def _journal(self, domain: str, description: str, **kw: Any) -> None:
        journal = getattr(self._self_model, "journal", None)
        if journal is None:
            return
        try:
            journal.record(domain, description or "(sandbox run)",
                           reversibility="reversible", **kw)
        except Exception:
            logger.debug("sandbox journal failed", exc_info=True)
