"""The fixed per-request cost the adapter adds to every model request stays within its budget.

Every tool schema and both static prompt blocks are sent with every model request of every turn.
The M1 overhead gate (foreground prompt tokens within +15% of plain Hermes) failed at about +37%
with 12 tools (1,754 tokens, 6,700 characters) and 242 tokens of system text; this pins the fixed
part so it cannot regrow unnoticed. Counting method: characters of the same rendering the served
model's chat template uses (one JSON object per tool, the system text verbatim). On the
GLM-5.3-Flash tokenizer the schemas run at 3.7 characters per token and the prose at 4.9, so the
limits below are about 930 tokens of tools and 165 of system text (the budget's own measured
values are 880 and 148).
"""

import json

from protagine_hermes import prompt_section
from protagine_hermes.client import Settings
from protagine_hermes.reminders import SCHEMA as REMINDER_SCHEMA
from protagine_hermes.tools import FORGET_SCHEMA, PEOPLE_SCHEMA, SEARCH_SCHEMA, SELF_SCHEMA
from protagine_memory.provider import _PROTAGINE_TOOL_SCHEMAS, _SYSTEM_PROMPT

PLUGIN_SCHEMAS = (SELF_SCHEMA, PEOPLE_SCHEMA, SEARCH_SCHEMA, FORGET_SCHEMA, REMINDER_SCHEMA)
TOOL_BUDGET_CHARS = 3_400       # 6 tools; the first cut sent 12 tools in 6,700 characters
SYSTEM_BUDGET_CHARS = 800       # provider block + plugin section, measured 725; the first cut sent 1,004


def rendered(schema) -> str:
    """What the chat template puts in the prompt: the function object as one JSON line."""
    return json.dumps(schema, ensure_ascii=False) + "\n"


def test_tool_schemas_stay_within_the_budget():
    schemas = [*PLUGIN_SCHEMAS, *_PROTAGINE_TOOL_SCHEMAS]
    names = [schema["name"] for schema in schemas]
    # The provider's owner-lane affect write is gone: contact affect comes from the appraisal's
    # their_valence (M5), and no mind code read what the tool wrote.
    assert len(names) == len(set(names)) == 6 and "protagine_record_affect" not in names, names
    total = sum(len(rendered(schema)) for schema in schemas)
    assert total <= TOOL_BUDGET_CHARS, {schema["name"]: len(rendered(schema)) for schema in schemas}
    for schema in schemas:  # the person scope is bound server-side, never a model argument
        assert not {"contact_id", "person_id"} & set(schema["parameters"]["properties"]) or schema is PEOPLE_SCHEMA


def test_static_prompt_text_stays_within_the_budget(tmp_path):
    settings = Settings(sidecar_url="http://127.0.0.1:7777", key_file=tmp_path / "api.key", api_key="k", home=tmp_path,
                        hermes_home=tmp_path, outbox_path=tmp_path / "outbox.sqlite3")
    (tmp_path / "identity.yaml").write_text("owner:\n  name: Owner\n", encoding="utf-8")
    section = prompt_section(settings)({"session_id": "s", "platform": "cli"})
    total = len(_SYSTEM_PROMPT) + len(section)
    assert total <= SYSTEM_BUDGET_CHARS, (len(_SYSTEM_PROMPT), len(section))
    # The reading rules that used to ride in every turn's context are said once, here.
    assert "memory-context" in _SYSTEM_PROMPT and "Current Time" in _SYSTEM_PROMPT
    assert "protagine_memory_search" in section and "protagine_self" in section
