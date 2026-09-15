"""Install instance guidance and diagnostic skills for supported harnesses."""
from .context import PROTAGINE_CONTEXT_TEMPLATE, write_protagine_context
from .skills import (
    PROTAGINE_CHECK_SKILL, PROTAGINE_DIAGNOSTIC_SKILL,
    get_skill_config_path, get_skill_path, remove_protagine_skill,
    write_protagine_check_skill, write_protagine_skill,
)

__all__ = [
    "PROTAGINE_CONTEXT_TEMPLATE", "write_protagine_context",
    "PROTAGINE_CHECK_SKILL", "PROTAGINE_DIAGNOSTIC_SKILL",
    "get_skill_config_path", "get_skill_path", "remove_protagine_skill",
    "write_protagine_check_skill", "write_protagine_skill",
]
