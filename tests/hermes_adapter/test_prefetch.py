"""Memory provider: prefetch works, guests get the scoped projection (evals 7.2 test 8 canary)."""

from conftest import API_KEY, CANARY, OWNER, probe

PROVIDER_PRELUDE = '''
import json, os
def emit(**values):
    print("@@RESULT@@" + json.dumps(values, default=str), flush=True)
from plugins.memory import load_memory_provider
provider = load_memory_provider("protagine-memory")
assert provider is not None, "provider did not load"
provider.initialize("session-1", hermes_home=os.environ["HERMES_HOME"], platform="cli")
from gateway.session_context import set_session_vars, clear_session_vars
'''


def test_provider_loads_from_shared_keys_and_prefetches_for_the_owner(home, sidecar):
    result = probe('''
context = provider.prefetch("what are my plans", session_id="session-1")
emit(name=provider.name, url=provider.sidecar_url, key=provider._api_key, context=context,
     writer=provider._turn_writer_enabled(), tools=[s["name"] for s in provider.get_tool_schemas()])
''', home, prelude=PROVIDER_PRELUDE)
    assert result["name"] == "protagine"
    assert result["url"] == sidecar.url and result["key"] == API_KEY
    assert CANARY in result["context"] and "shared facts" in result["context"]
    assert "## Current Time" in result["context"]
    assert result["writer"] is False  # the general plugin owns capture
    assert "protagine_claim_task" not in result["tools"] and "protagine_resolve_commitment" in result["tools"]
    assert "Persistent state" not in result["context"] and "[priority" not in result["context"]  # no per-turn preamble
    call, = sidecar.calls("/v1/host/context/assemble", "POST")
    assert call["authorization"] == f"Bearer {API_KEY}"
    assert call["json"]["context"]["contact_id"] == OWNER and "audience" not in call["json"]


def test_guest_prefetch_carries_the_viewer_scope_and_no_owner_only_text(home, sidecar):
    result = probe('''
tokens = set_session_vars(platform="telegram", user_id="2003", chat_id="2003", session_id="session-1")
context = provider.prefetch("what are the plans", session_id="session-1")
clear_session_vars(tokens)
emit(context=context)
''', home, prelude=PROVIDER_PRELUDE)
    assert CANARY not in result["context"]
    assert "shared facts" in result["context"]
    call, = sidecar.calls("/v1/host/context/assemble", "POST")
    assert call["json"]["context"]["contact_id"] == "p-03"
    assert call["json"]["audience"] == "viewer"
    assert "projection_policy" not in call["json"]


def test_unresolved_channel_sender_gets_no_context(home, sidecar):
    result = probe('''
provider._resolve_handle = lambda platform, sender: None
tokens = set_session_vars(platform="telegram", user_id="7777", chat_id="7777", session_id="session-1")
context = provider.prefetch("hello", session_id="session-1")
clear_session_vars(tokens)
emit(context=context)
''', home, prelude=PROVIDER_PRELUDE)
    assert result["context"] == ""
    assert sidecar.calls("/v1/host/context/assemble") == []


def test_provider_writes_turns_only_when_the_general_plugin_is_absent(home, sidecar):
    home.write_config(**{**home.config, "plugins": {**home.config["plugins"], "enabled": []}})
    result = probe('''
provider.sync_turn("remember X", "noted", session_id="session-1", turn_id="turn-1")
provider.shutdown()
emit(writer=provider._turn_writer_enabled())
''', home, prelude=PROVIDER_PRELUDE)
    assert result["writer"] is True
    call, = sidecar.calls("/v1/host/turns/sync", "POST")
    assert call["json"]["context"]["contact_id"] == OWNER
    assert call["json"]["user_message"]["content"] == "remember X"


def test_guest_direct_tools_are_withheld(home, sidecar):
    """A guest turn is offered no direct tool; a call that still arrives is refused once, terminally."""
    result = probe('''
tokens = set_session_vars(platform="telegram", user_id="2003", chat_id="2003", session_id="session-1")
guest = json.loads(provider.handle_tool_call("protagine_record_affect", {"valence": 0.1, "arousal": 0.1}))
guest_tools = [s["name"] for s in provider.get_tool_schemas()]
clear_session_vars(tokens)
tokens = set_session_vars(platform="telegram", user_id="1001", chat_id="1001", session_id="session-2")
owner_tools = [s["name"] for s in provider.get_tool_schemas()]
clear_session_vars(tokens)
emit(guest=guest, guest_tools=guest_tools, owner_tools=owner_tools)
''', home, prelude=PROVIDER_PRELUDE)
    assert result["guest"] == {"unavailable": True, "retry": False, "reason": result["guest"]["reason"]}
    assert "owner-only" in result["guest"]["reason"]
    assert result["guest_tools"] == []
    assert set(result["owner_tools"]) == {"protagine_record_affect", "protagine_resolve_commitment"}
    assert not any(call["path"].startswith("/v1/host/affect") for call in sidecar.requests)


def test_unbound_channel_session_is_built_without_direct_tools(home, sidecar):
    """A provider initialised for a real channel with no sender (an inbound the gateway could not
    bind) hands Hermes no direct tool at agent build time, so none can burn the turn's iterations."""
    result = probe('''
from plugins.memory import load_memory_provider
unbound = load_memory_provider("protagine-memory")
unbound.initialize("session-9", hermes_home=os.environ["HERMES_HOME"], platform="capture")
emit(tools=[s["name"] for s in unbound.get_tool_schemas()],
     call=json.loads(unbound.handle_tool_call("protagine_resolve_commitment", {"commitment_id": "c", "action": "fulfilled"})),
     cli_tools=[s["name"] for s in provider.get_tool_schemas()])
''', home, prelude=PROVIDER_PRELUDE)
    assert result["tools"] == []
    assert result["call"]["unavailable"] is True and result["call"]["retry"] is False
    assert "protagine_resolve_commitment" in result["cli_tools"]
    assert "protagine_list_goals" not in result["cli_tools"] and "protagine_get_patterns" not in result["cli_tools"]
