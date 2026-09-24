"""The adapter loads on stock Hermes through public seams only (M1 acceptance test 7), and its one prompt
section carries the constitution, the owner and the self-narrative within Hermes' bounds (M8, seam 2)."""

import yaml

from conftest import probe

RENDER = '''
from hermes_cli.plugins import render_system_prompt_sections
def section():
    sections = render_system_prompt_sections({"session_id": "s", "platform": "cli"})
    return next(s.content for s in sections if s.id == "protagine")
'''
CONSTITUTION = "You are Agent. Your values: care. Your boundaries: never send money."
NARRATIVE_LEAD = "What you know about yourself, from your own record"

STOCK_HOOKS = {"pre_llm_call", "post_llm_call", "pre_tool_call", "on_kanban_dispatch_tick"}
TOOLS = {"protagine_self", "protagine_people", "protagine_memory_search", "protagine_memory_forget",
         "protagine_reminder"}


def test_plugin_registers_only_stock_seams(home, sidecar):
    result = probe('''
from tools.registry import registry
def ours(callbacks):
    return [cb for cb in callbacks if "protagine_hermes" in str(getattr(cb, "__module__", ""))]
hooks = {name: len(ours(cbs)) for name, cbs in manager._hooks.items() if ours(cbs)}
middleware = {name: len(ours(cbs)) for name, cbs in manager._middleware.items() if ours(cbs)}
emit(hooks=hooks, middleware=middleware, valid=[h for h in hooks if h not in VALID_HOOKS],
     registered=loaded.hooks_registered, commands=list(manager._plugin_commands),
     sections=list(manager._system_prompt_sections),
     tools=[name for name in %r if registry.get_entry(name) is not None],
     platforms=[name for name in getattr(manager, "_platforms", {}) if "protagine" in name])
''' % (sorted(TOOLS),), home)
    assert set(result["hooks"]) == STOCK_HOOKS, result["hooks"]
    assert all(count == 1 for count in result["hooks"].values())
    assert result["valid"] == []  # no patched-hook probes: every hook is in VALID_HOOKS
    assert result["middleware"] == {}
    assert "mind" in result["commands"]
    assert "protagine" in result["sections"]
    assert set(result["tools"]) == TOOLS
    assert result["platforms"] == []
    # Registration performs no sidecar I/O.
    assert sidecar.requests == [] or all(r["path"] == "/v1/mind/state" for r in sidecar.requests)


def test_registration_reads_key_file_and_sidecar_url(home, sidecar):
    result = probe('''
from protagine_hermes.client import load_settings
s = load_settings()
emit(url=s.sidecar_url, key=s.api_key, home=str(s.home), worker=s.worker_profile, outbox=str(s.outbox_path))
''', home)
    assert result["url"] == sidecar.url
    assert result["key"] == home.key_file.read_text().strip()
    assert result["home"] == str(home.instance)
    assert result["worker"] == "protagine-act"
    assert result["outbox"].startswith(str(home.hermes))


def test_plugin_loads_without_identity_or_mind_files(home, sidecar):
    (home.instance / "identity.yaml").unlink()
    (home.instance / "protagine.yaml").unlink()
    result = probe("emit(ok=loaded.enabled, error=loaded.error)", home)
    assert result["ok"] and not result["error"]


def test_prompt_section_carries_the_constitution_without_the_mind_routes(home, sidecar):
    """The constitution is read from identity.yaml by the plugin itself: no sidecar, no mind routes needed."""
    result = probe(RENDER + "emit(content=section())", home)
    content = result["content"]
    assert content.startswith(CONSTITUTION + "\n\nYour owner is Owner.\n\n")
    assert "protagine_memory_search" in content and "protagine_self yes or no" in content
    assert NARRATIVE_LEAD not in content
    assert sidecar.calls("/v1/mind/narrative") == []


def test_prompt_section_carries_the_narrative_the_sidecar_serves_and_caches_it(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.narrative = {"enabled": True, "text": "I researched tides for the owner. [i-01]",
                              "sections": {"recent": "I researched tides for the owner. [i-01]"},
                              "cites": ["i-01"], "updated_at": "2027-03-01T03:10:00+00:00"}
    result = probe(RENDER + "emit(first=section(), second=section())", home)
    content = result["first"]
    assert content.startswith(CONSTITUTION + "\n\nYour owner is Owner.\n\n" + NARRATIVE_LEAD)
    assert "I researched tides for the owner. [i-01]" in content and "protagine_self why" in content
    assert content.index("[i-01]") < content.index("Protagine keeps your long-term memory")
    assert result["second"] == content
    assert len(sidecar.calls("/v1/mind/narrative", "GET")) == 1  # 60 s cache


def test_prompt_section_omits_a_disabled_narrative(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.narrative = {"enabled": False, "text": "stale text from an off faculty", "sections": {}, "cites": []}
    result = probe(RENDER + "emit(content=section())", home)
    assert "stale text" not in result["content"] and NARRATIVE_LEAD not in result["content"]
    assert result["content"].startswith(CONSTITUTION)


def test_prompt_section_stays_within_bounds_and_fails_open(home, sidecar):
    """A 1,500-character constitution plus a 2,000-character narrative render under 4,000 characters, and a
    client that raises leaves the constitution in place (Hermes would otherwise skip the whole section)."""
    (home.instance / "identity.yaml").write_text(yaml.safe_dump({
        "owner": {"name": "Owner", "contact_id": "p-01", "handles": {"telegram": ["1001"]}},
        "agent": {"name": "Agent", "values": [f"v{i}".ljust(100, "v") for i in range(12)],
                  "boundaries": [f"b{i}".ljust(160, "b") for i in range(12)]}}))
    sidecar.mind_routes = True
    sidecar.mind.narrative = {"enabled": True, "text": ("I did a thing. [i-01]\n" * 200)[:2600], "sections": {},
                              "cites": ["i-01"]}
    result = probe(RENDER + '''
full = section()
import protagine_hermes.client as _client
def boom(self, **kwargs):
    raise RuntimeError("no narrative for you")
_client.ProtagineClient.narrative = boom
emit(full=full, degraded=section())
''', home)
    full, degraded = result["full"], result["degraded"]
    assert len(full) <= 4000 and full.startswith("You are Agent. Your values: v0")
    constitution = full.split("\n\nYour owner is Owner.\n\n", 1)[0]
    assert len(constitution) == 1500 and "Your boundaries: b0" in constitution  # clipped, boundaries partly in
    assert NARRATIVE_LEAD in full and "Protagine keeps your long-term memory" in full
    narrative = full.split("check with protagine_self why):\n", 1)[1].split("\n\nProtagine keeps", 1)[0]
    assert len(narrative) == 2000
    assert degraded.startswith("You are Agent. Your values: v0") and NARRATIVE_LEAD not in degraded
    assert "Protagine keeps your long-term memory" in degraded

