"""AutonomyLoop configuration dataclass."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional
from enum import Enum
from zoneinfo import ZoneInfo


class AutonomyMode(str, Enum):
    """Autonomy loop operating mode."""
    REACTIVE = "reactive"    # On-demand only (default)
    PROACTIVE = "proactive"  # Timer-based


def _enabled_phases(value) -> Optional[tuple[str, ...]]:
    if value is None:
        return None
    parts = value.split(',') if isinstance(value, str) else value
    if not isinstance(parts, (list, tuple)) or not parts:
        raise ValueError('enabled_phases must name at least one existing phase')
    if any(not isinstance(part, str) or not part.strip() for part in parts):
        raise ValueError('enabled_phases contains an empty or invalid name')
    return tuple(dict.fromkeys(part.strip() for part in parts))


@dataclass
class InitiativeConfig:
    """Configuration for initiative deduplication and feedback (v0.7.10)."""
    cooldown_hours: float = 24.0
    cooldown_tasks: float = 12.0
    cooldown_contacts: float = 72.0
    max_snooze_hours: int = 168
    feedback_enabled: bool = True


@dataclass
class AutonomyConfig:
    """Configuration for the Protagine autonomy loop.

    All time values are in seconds unless noted.
    """

    # Operating mode: reactive (on-demand) or proactive (timer-based)
    mode: AutonomyMode = AutonomyMode.REACTIVE

    # Where the mode came from: "env" (explicit PROTAGINE_AUTONOMY_MODE),
    # "preset" (PROTAGINE_AUTONOMY_PRESET via preset-loop coupling, H4.1),
    # "legacy_tick" (tick interval set without a mode), "config" (config
    # file), or "default". Diagnostic only — surfaced in the posture
    # endpoint so an operator can always see WHY the loop runs as it does.
    mode_source: str = "default"

    # None retains the existing phase set. A deployment can activate only the
    # reviewed phases of this same loop without waking unrelated maintenance.
    enabled_phases: Optional[tuple[str, ...]] = None
    # Only affects the execute phase: persist proposals without legacy skill
    # execution, queue dispatch, broadcasts or delivery. Other services retain
    # their independent configuration and authority.
    proposals_only: bool = False

    # IANA timezone for quiet hours (e.g., "America/El_Salvador")
    timezone: str = "UTC"

    # How long to sleep between ticks when no events wake the loop early.
    tick_interval_secs: float = 300.0

    # Minimum initiative priority score [0.0–1.0] to execute an action.
    initiative_confidence_threshold: float = 0.7

    # Maximum autonomous actions taken per hour (safety limit).
    max_actions_per_hour: int = 20

    # Suppress non-urgent surfacing during quiet hours ("HH:MM" 24-hour format).
    # Set both to "00:00" to disable quiet hours.
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "07:00"

    # Minimum anomaly severity [0.0–1.0] to surface to the initiative engine.
    anomaly_severity_threshold: float = 0.6

    # Minimum prediction confidence [0.0–1.0] to surface predictions.
    prediction_confidence_threshold: float = 0.75

    # Hours after which an active goal is considered stale.
    goal_stale_threshold_hours: float = 24.0

    # How often to run the identity bootstrap self-check (hours).
    bootstrap_check_interval_hours: int = 24

    # How often to run the self-reflection component (days).
    self_reflection_interval_days: int = 7

    # ── Owner check-in (silence-triggered proactive outreach) ──────────
    # When no initiatives fire for this many hours, reach out to the owner.
    owner_check_in_enabled: bool = False
    owner_check_in_silent_hours: float = 1.0
    owner_check_in_cooldown_hours: float = 4.0
    owner_contact_id: Optional[str] = None  # None = resolve from identity

    # ── Proactive delivery (push initiatives to webhook) ────────────────
    # When False, initiatives are stored but never pushed to the delivery bridge.
    # The agent must poll via context-digest to see pending work.
    proactive_delivery_enabled: bool = False

    # ── Delivery shadow mode (outward-facing rollout safety) ────────────
    # When True, reach-out initiatives that would be delivered are LOGGED
    # (target + payload) but never sent, regardless of proactive_delivery_enabled.
    # Lets an operator eyeball the intended first batch before enabling real
    # sends. Real delivery requires proactive_delivery_enabled AND shadow off.
    delivery_shadow_mode: bool = False

    # ── Conversation synthesis (periodic memory scan for goals) ─────────
    # Scans stored conversation memories for implicit goals and commitments.
    conversation_synthesis_enabled: bool = True
    conversation_synthesis_interval_secs: float = 1800.0  # 30 min
    conversation_synthesis_lookback_hours: float = 2.0
    conversation_synthesis_min_confidence: float = 0.35

    # ── class methods ──────────────────────────────────────────────────

    @classmethod
    def from_protagine_config(cls, protagine_cfg: object) -> "AutonomyConfig":
        """Construct config from a Protagine config object or dict.

        Looks for an ``autonomy`` sub-key/attribute. If not found, returns
        defaults. Supports both dict-like (``protagine_cfg["autonomy"]``) and
        object-like (``protagine_cfg.autonomy``) configs.

        Args:
            protagine_cfg: Protagine config object/dict, or None for defaults.
        """
        if protagine_cfg is None:
            return cls()

        # Extract the autonomy sub-section
        autonomy_section = None
        if isinstance(protagine_cfg, dict):
            autonomy_section = protagine_cfg.get("autonomy")
        else:
            autonomy_section = getattr(protagine_cfg, "autonomy", None)

        if autonomy_section is None:
            return cls()

        # Read values from the section
        def _get(key: str, default):
            if isinstance(autonomy_section, dict):
                return autonomy_section.get(key, default)
            return getattr(autonomy_section, key, default)

        defaults = cls()

        # Mode and timezone
        mode_str = str(_get("mode", "reactive")).lower()
        mode = AutonomyMode(mode_str) if mode_str in [m.value for m in AutonomyMode] else AutonomyMode.REACTIVE
        timezone = str(_get("timezone", "UTC"))

        # Validate timezone
        try:
            ZoneInfo(timezone)
        except Exception:
            timezone = "UTC"

        return cls(
            mode=mode,
            mode_source="config",
            enabled_phases=_enabled_phases(_get("enabled_phases", None)),
            proposals_only=bool(_get("proposals_only", False)),
            timezone=timezone,
            tick_interval_secs=float(_get("tick_interval_secs", defaults.tick_interval_secs)),
            initiative_confidence_threshold=float(_get(
                "initiative_confidence_threshold",
                defaults.initiative_confidence_threshold,
            )),
            max_actions_per_hour=int(_get("max_actions_per_hour", defaults.max_actions_per_hour)),
            quiet_hours_start=str(_get("quiet_hours_start", defaults.quiet_hours_start)),
            quiet_hours_end=str(_get("quiet_hours_end", defaults.quiet_hours_end)),
            anomaly_severity_threshold=float(_get(
                "anomaly_severity_threshold",
                defaults.anomaly_severity_threshold,
            )),
            prediction_confidence_threshold=float(_get(
                "prediction_confidence_threshold",
                defaults.prediction_confidence_threshold,
            )),
            goal_stale_threshold_hours=float(_get(
                "goal_stale_threshold_hours",
                defaults.goal_stale_threshold_hours,
            )),
            bootstrap_check_interval_hours=int(_get(
                "bootstrap_check_interval_hours",
                defaults.bootstrap_check_interval_hours,
            )),
            self_reflection_interval_days=int(_get(
                "self_reflection_interval_days",
                defaults.self_reflection_interval_days,
            )),
            owner_check_in_enabled=bool(_get("owner_check_in_enabled", defaults.owner_check_in_enabled)),
            owner_check_in_silent_hours=float(_get(
                "owner_check_in_silent_hours",
                defaults.owner_check_in_silent_hours,
            )),
            owner_check_in_cooldown_hours=float(_get(
                "owner_check_in_cooldown_hours",
                defaults.owner_check_in_cooldown_hours,
            )),
            owner_contact_id=_get("owner_contact_id", defaults.owner_contact_id),
            proactive_delivery_enabled=bool(
                _get("proactive_delivery_enabled", defaults.proactive_delivery_enabled)
            ),
            delivery_shadow_mode=bool(
                _get("delivery_shadow_mode", defaults.delivery_shadow_mode)
            ),
            conversation_synthesis_enabled=bool(_get(
                "conversation_synthesis_enabled",
                defaults.conversation_synthesis_enabled,
            )),
            conversation_synthesis_interval_secs=float(_get(
                "conversation_synthesis_interval_secs",
                defaults.conversation_synthesis_interval_secs,
            )),
            conversation_synthesis_lookback_hours=float(_get(
                "conversation_synthesis_lookback_hours",
                defaults.conversation_synthesis_lookback_hours,
            )),
            conversation_synthesis_min_confidence=float(_get(
                "conversation_synthesis_min_confidence",
                defaults.conversation_synthesis_min_confidence,
            )),
        )

    @classmethod
    def from_env(cls) -> "AutonomyConfig":
        """Construct config from environment variables.

        All env vars are optional; unset vars fall back to field defaults.

        Environment variables:
            PROTAGINE_AUTONOMY_MODE
            PROTAGINE_AUTONOMY_PHASES (comma-separated existing phase names)
            PROTAGINE_AUTONOMY_PROPOSALS_ONLY
            PROTAGINE_PRESET_LOOP_COUPLING (default on: an active
                PROTAGINE_AUTONOMY_PRESET supplies the mode when
                PROTAGINE_AUTONOMY_MODE is unset)
            PROTAGINE_TIMEZONE
            PROTAGINE_AUTONOMY_TICK_INTERVAL_SECS
            PROTAGINE_AUTONOMY_INITIATIVE_CONFIDENCE_THRESHOLD
            PROTAGINE_AUTONOMY_MAX_ACTIONS_PER_HOUR
            PROTAGINE_AUTONOMY_QUIET_HOURS_START
            PROTAGINE_AUTONOMY_QUIET_HOURS_END
            PROTAGINE_AUTONOMY_ANOMALY_SEVERITY_THRESHOLD
            PROTAGINE_AUTONOMY_PREDICTION_CONFIDENCE_THRESHOLD
            PROTAGINE_AUTONOMY_GOAL_STALE_THRESHOLD_HOURS
            PROTAGINE_OWNER_CHECK_IN_ENABLED
            PROTAGINE_OWNER_CHECK_IN_SILENT_HOURS
            PROTAGINE_OWNER_CHECK_IN_COOLDOWN_HOURS
            PROTAGINE_OWNER_CONTACT_ID
            PROTAGINE_PROACTIVE_DELIVERY_ENABLED
            PROTAGINE_DELIVERY_SHADOW
            PROTAGINE_CONVERSATION_SYNTHESIS_ENABLED
            PROTAGINE_CONVERSATION_SYNTHESIS_INTERVAL_SECS
            PROTAGINE_CONVERSATION_SYNTHESIS_LOOKBACK_HOURS
            PROTAGINE_CONVERSATION_SYNTHESIS_MIN_CONFIDENCE
        """
        logger = logging.getLogger(__name__)

        # Mode selection. Precedence (H4.1):
        #   explicit PROTAGINE_AUTONOMY_MODE  >  coupled preset mode
        #   >  legacy tick-interval migration  >  default (reactive)
        # Explicit env ALWAYS wins — PROTAGINE_AUTONOMY_MODE=reactive is the
        # rollback path even under a preset. Coupling errors fail toward
        # reactive (coupled_loop_mode never raises; a broken import just
        # falls through to the legacy resolution).
        mode = AutonomyMode.REACTIVE
        mode_source = "default"
        raw_mode = os.environ.get("PROTAGINE_AUTONOMY_MODE")
        if raw_mode is not None and raw_mode.strip():
            mode_str = raw_mode.strip().lower()
            mode = (AutonomyMode(mode_str)
                    if mode_str in [m.value for m in AutonomyMode]
                    else AutonomyMode.REACTIVE)
            mode_source = "env"
        else:
            coupled = None
            try:
                from protagine.util.autonomy_preset import coupled_loop_mode
                coupled = coupled_loop_mode()
            except Exception:
                coupled = None  # fail toward reactive
            if coupled in ("reactive", "proactive"):
                mode = AutonomyMode(coupled)
                mode_source = "preset"

        # Timezone
        timezone = os.environ.get("PROTAGINE_TIMEZONE", "UTC")
        try:
            ZoneInfo(timezone)
        except Exception:
            logger.warning("Invalid PROTAGINE_TIMEZONE '%s', falling back to UTC", timezone)
            timezone = "UTC"

        # Legacy migration: if tick interval set without mode (and no coupled
        # preset supplied one), assume proactive
        legacy_tick = os.environ.get("PROTAGINE_AUTONOMY_TICK_INTERVAL_SECS")
        if legacy_tick and mode_source == "default":
            logger.warning(
                "PROTAGINE_AUTONOMY_TICK_INTERVAL_SECS set without PROTAGINE_AUTONOMY_MODE. "
                "Defaulting to PROACTIVE mode to preserve existing behavior. "
                "Add PROTAGINE_AUTONOMY_MODE=proactive to make this explicit."
            )
            mode = AutonomyMode.PROACTIVE
            mode_source = "legacy_tick"

        def _float(key: str, default: float) -> float:
            v = os.environ.get(key)
            return float(v) if v is not None else default

        def _int(key: str, default: int) -> int:
            v = os.environ.get(key)
            return int(v) if v is not None else default

        def _str(key: str, default: str) -> str:
            return os.environ.get(key, default)

        def _bool(key: str, default: bool) -> bool:
            v = os.environ.get(key, "").lower()
            if v == "true":
                return True
            if v == "false":
                return False
            return default

        defaults = cls()
        return cls(
            mode=mode,
            mode_source=mode_source,
            enabled_phases=_enabled_phases(os.environ.get("PROTAGINE_AUTONOMY_PHASES")),
            proposals_only=_bool("PROTAGINE_AUTONOMY_PROPOSALS_ONLY", False),
            timezone=timezone,
            tick_interval_secs=_float(
                "PROTAGINE_AUTONOMY_TICK_INTERVAL_SECS",
                defaults.tick_interval_secs,
            ),
            initiative_confidence_threshold=_float(
                "PROTAGINE_AUTONOMY_INITIATIVE_CONFIDENCE_THRESHOLD",
                defaults.initiative_confidence_threshold,
            ),
            max_actions_per_hour=_int(
                "PROTAGINE_AUTONOMY_MAX_ACTIONS_PER_HOUR",
                defaults.max_actions_per_hour,
            ),
            quiet_hours_start=_str(
                "PROTAGINE_AUTONOMY_QUIET_HOURS_START",
                defaults.quiet_hours_start,
            ),
            quiet_hours_end=_str(
                "PROTAGINE_AUTONOMY_QUIET_HOURS_END",
                defaults.quiet_hours_end,
            ),
            anomaly_severity_threshold=_float(
                "PROTAGINE_AUTONOMY_ANOMALY_SEVERITY_THRESHOLD",
                defaults.anomaly_severity_threshold,
            ),
            prediction_confidence_threshold=_float(
                "PROTAGINE_AUTONOMY_PREDICTION_CONFIDENCE_THRESHOLD",
                defaults.prediction_confidence_threshold,
            ),
            goal_stale_threshold_hours=_float(
                "PROTAGINE_AUTONOMY_GOAL_STALE_THRESHOLD_HOURS",
                defaults.goal_stale_threshold_hours,
            ),
            bootstrap_check_interval_hours=_int(
                "PROTAGINE_AUTONOMY_BOOTSTRAP_CHECK_INTERVAL_HOURS",
                defaults.bootstrap_check_interval_hours,
            ),
            self_reflection_interval_days=_int(
                "PROTAGINE_AUTONOMY_SELF_REFLECTION_INTERVAL_DAYS",
                defaults.self_reflection_interval_days,
            ),
            owner_check_in_enabled=_bool(
                "PROTAGINE_OWNER_CHECK_IN_ENABLED",
                defaults.owner_check_in_enabled,
            ),
            owner_check_in_silent_hours=_float(
                "PROTAGINE_OWNER_CHECK_IN_SILENT_HOURS",
                defaults.owner_check_in_silent_hours,
            ),
            owner_check_in_cooldown_hours=_float(
                "PROTAGINE_OWNER_CHECK_IN_COOLDOWN_HOURS",
                defaults.owner_check_in_cooldown_hours,
            ),
            owner_contact_id=os.environ.get(
                "PROTAGINE_OWNER_CONTACT_ID",
                defaults.owner_contact_id,
            ),
            proactive_delivery_enabled=_bool(
                "PROTAGINE_PROACTIVE_DELIVERY_ENABLED",
                defaults.proactive_delivery_enabled,
            ),
            delivery_shadow_mode=_bool(
                "PROTAGINE_DELIVERY_SHADOW",
                defaults.delivery_shadow_mode,
            ),
            conversation_synthesis_enabled=_bool(
                "PROTAGINE_CONVERSATION_SYNTHESIS_ENABLED",
                defaults.conversation_synthesis_enabled,
            ),
            conversation_synthesis_interval_secs=_float(
                "PROTAGINE_CONVERSATION_SYNTHESIS_INTERVAL_SECS",
                defaults.conversation_synthesis_interval_secs,
            ),
            conversation_synthesis_lookback_hours=_float(
                "PROTAGINE_CONVERSATION_SYNTHESIS_LOOKBACK_HOURS",
                defaults.conversation_synthesis_lookback_hours,
            ),
            conversation_synthesis_min_confidence=_float(
                "PROTAGINE_CONVERSATION_SYNTHESIS_MIN_CONFIDENCE",
                defaults.conversation_synthesis_min_confidence,
            ),
        )
