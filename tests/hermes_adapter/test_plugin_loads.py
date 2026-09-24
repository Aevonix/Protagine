"""The adapter loads on stock Hermes through public seams only (M1 acceptance test 7)."""

from conftest import probe

STOCK_HOOKS = {"pre_llm_call", "post_llm_call", "pre_tool_call", "on_kanban_dispatch_tick"}
TOOLS = {"protagine_self", "protagine_people", "protagine_memory_search", "protagine_memory_forget",
         "protagine_reminder", "protagine_opinions"}


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
