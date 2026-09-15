"""Write a short, model-independent Protagine reference into a workspace."""
from pathlib import Path

PROTAGINE_CONTEXT_TEMPLATE = """# Protagine integration

Protagine is a Proto-AGI engine providing persistent memory, shared task state
and autonomy. Your agent's private identity is separate from the platform name.

Use `protagine_health` to check connectivity and `protagine_get_context` for relevant,
authenticated context. Check `protagine_check_commitments` before promising work.
Recall useful evidence with `protagine_lookup_facts`; cite the returned source and
ask about contradictions. Save durable facts with `protagine_remember_fact` only
when their source and usefulness are clear. Corrections and forgetting govern
source-backed recall across sessions; do not create diagnostic junk memories.

Use `protagine doctor` and `protagine mcp detect` for installation checks. Hermes uses
its native Protagine plugin and memory provider. Coding harnesses may use the MCP
server. Both connect to the same selected instance over its authenticated API;
sharing an instance does not grant every caller access to every private source.

Use the selected instance's PROTAGINE_* configuration and credential
reference. Never guess a key or print credentials into diagnostic transcripts.
"""



def write_protagine_context(workspace_dir: Path) -> bool:
    if not workspace_dir.exists():
        return False
    try:
        (workspace_dir / "PROTAGINE.md").write_text(PROTAGINE_CONTEXT_TEMPLATE)
        return True
    except OSError:
        return False
