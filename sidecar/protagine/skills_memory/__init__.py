"""Compounding learning: reusable procedure memory (item 3).

Stores candidate procedures (situation, steps, gotchas) and retrieves relevant
ones into project prompts. A historical completion, failure note or model
assessment does not verify procedure quality. Native skill evaluation retains
independent source-bound receipts; this store does not infer skill rewards.

Named ``skills_memory`` to avoid clashing with the existing ``skills/``
executor-skill registry (packaged executable skills). These are prompt-level
procedure memories for the sidecar's own reasoning loops; they inform
reasoning and never execute directly (PROTAGINE_SKILLS_ENABLED, default true).
Distillation starts in shadow (PROTAGINE_SKILLS_DISTILL).
"""

from protagine.skills_memory.models import Skill, signature_overlap, situation_signature
from protagine.skills_memory.store import SkillStore, skills_enabled, skills_distill_mode
from protagine.skills_memory.distill import should_distill, distill_from_completion
from protagine.skills_memory.retrieve import relevant_skills, format_block

__all__ = [
    "Skill", "SkillStore", "signature_overlap", "situation_signature",
    "should_distill", "distill_from_completion", "relevant_skills",
    "format_block", "skills_enabled", "skills_distill_mode",
]
