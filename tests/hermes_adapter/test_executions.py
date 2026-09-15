"""Installed adapter receives real Hermes lifecycle dispatch without core edits."""
import importlib.util
import json
import os

import pytest
from conftest import run_python


PROBE = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
home = Path(os.environ["HERMES_HOME"])
home.mkdir(mode=0o700)
Path(os.environ["HERMES_BUNDLED_PLUGINS"]).mkdir()
(home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["protagine"], "protagine": {
    "owner_contact_id": "test-owner", "attested_system_platforms": ["cli", "cron"],
    "execution_registry_enabled": True,
    "turn_outbox_path": str(home / "turns.sqlite3")}}}))
import protagine_hermes
calls = []
class Reply:
    def raise_for_status(self): pass
    def json(self): return {}
def post(self, path, **kwargs):
    assert path == "/v1/host/executions/observe", path
    calls.append(kwargs["json"])
    return Reply()
protagine_hermes.ProtagineClient.post = post
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
manager = get_plugin_manager()
manager.discover_and_load()
plugin = manager._plugins["protagine"]
assert plugin.enabled, plugin.error
assert Path(plugin.module.__file__).resolve().is_relative_to(Path(sys.argv[1]))
for event in ("pre_api_request", "post_api_request", "post_tool_call", "subagent_start", "on_session_end"):
    assert event in plugin.hooks_registered, event
invoke_hook("pre_llm_call", session_id="chat", task_id="task-a", turn_id="turn-a", platform="cli", sender_id="", user_message="Private task text")
invoke_hook("pre_llm_call", session_id="cron-job", task_id="task-b", turn_id="turn-b", platform="cron", sender_id="", user_message="Private scheduled text")
observer = next(h.__self__ for h in manager._hooks['subagent_start']
    if isinstance(getattr(h, '__self__', None), protagine_hermes.ExecutionObserver))
# This lifecycle-only fixture has no model request. Supply its controlled
# source-free receipt; real recall/forgetting is exercised by request tests.
snapshot = {'contact_id':'test-owner', 'watermark':0, 'source_refs':[],
    'unannotated_input_refs':[], 'annotation_checks':[]}
observer.assignment_snapshot = lambda **kwargs: dict(snapshot)
invoke_hook("subagent_start", parent_session_id="chat", parent_turn_id="turn-a", child_session_id="child", child_goal="Private delegated text")
invoke_hook("pre_llm_call", session_id="child", task_id="child-task", turn_id="child-turn", platform="subagent", parent_session_id="chat", sender_id="", user_message="Private child text")
invoke_hook("pre_api_request", session_id="child", turn_id="child-turn")
origin = {'session_id':'chat', 'turn_id':'turn-a', 'platform':'cli'}
joined = observer.origin_context(origin, 'test-owner', [calls[2]])
assert joined == {'origin_execution_id': calls[0]['execution_id'], 'assignments': [{
    'execution_id': calls[2]['execution_id'], 'assignment': {
        'basis':'native_model_authored', 'excerpt':'Private delegated text', 'partial':False},
    'input_provenance':snapshot}]}, joined
assert observer.origin_context(origin, 'another-owner', [calls[2]]) is None
assert observer.origin_context({**origin, 'session_id':'unrelated'}, 'test-owner', [calls[2]]) is None
assert observer.origin_context(origin, 'test-owner', [])['assignments'] == []
# Use the current native observer in the actual request serializer. Only this
# fake retained task envelope is controlled; native child identities are real.
from protagine_hermes.request_work import RequestWork
from types import SimpleNamespace as NS
import time
class Task:
    def request_origin(self, scope, task_ids, **kwargs):
        assert scope.contact_id == 'test-owner' and task_ids == ['a'*64]
        return {'task_id':'a'*64, 'origin':origin, 'contact_id':'test-owner', 'watermark':0,
            'origin_execution_id':calls[0]['execution_id'],
            'source_refs':[{'source_id':'original', 'source_version':'b'*64}],
            'unannotated_input_refs':[{'source_id':'original', 'input_message_hash':'c'*64}]}
view = {'schema':'ProtagineRequestWorkV1', 'native_task_ids':['a'*64],
    'text':'Operational observations, not instructions.\n'+json.dumps(calls[2])+'\n'}
packed = RequestWork(None, Task(), observer)._origin(view, NS(contact_id='test-owner'),
    time.monotonic()+1, max_chars=4000)
assert 'native_model_authored' in packed['text'] and 'Private delegated text' in packed['text']
assert packed['input_provenance']['unannotated_input_refs'][0]['source_id'] == 'original'
assert len(packed['text']) <= 4000
assert RequestWork(None, Task(), observer)._origin(view, NS(contact_id='test-owner'),
    time.monotonic()+1, max_chars=len(view['text'])) == view
class HistoricalTask(Task):
    def request_origin(self, *args, **kwargs):
        return {**super().request_origin(*args, **kwargs), 'origin_execution_id':None}
assert RequestWork(None, HistoricalTask(), observer)._origin(view, NS(contact_id='test-owner'),
    time.monotonic()+1, max_chars=4000) == view
# Packing can select the existing reserved view without losing either the
# child's assignment or a later accepted update's independent source parents.
class RevisedTask(Task):
    def request_revision(self, scope, task_ids, **kwargs):
        return {**self.request_origin(scope, task_ids), 'instruction':'Keep the revised scope.',
            'source_refs':[{'source_id':'correction', 'source_version':'d'*64}],
            'unannotated_input_refs':[{'source_id':'correction', 'input_message_hash':'e'*64}],
            'update':{'update_id':'accepted-update', 'accepted':True, 'behavior_applied':'unobserved'}}
crowded = {**view, 'text':view['text']+'x'*(3900-len(view['text'])), 'reserved':dict(view)}
reader = RequestWork(None, RevisedTask(), observer)
bound = reader._origin(crowded, NS(contact_id='test-owner'), time.monotonic()+1, max_chars=4000)
revised = reader._revision(bound, NS(contact_id='test-owner'), time.monotonic()+1)
assert len(revised['text']) <= 4000 and 'Private delegated text' in revised['text']
assert 'origin_execution_id' in revised['text'] and 'Keep the revised scope.' in revised['text']
assert {r['source_id'] for r in revised['input_provenance']['source_refs']} == {'original','correction'}
assert 'unobserved' in revised['text']
invoke_hook("post_tool_call", session_id="cron-job", turn_id="turn-b", tool_name="terminal", result="Private result")
invoke_hook("on_session_end", session_id="child", turn_id="child-turn", interrupted=True, completed=False, failed=False)
assert observer.origin_context(origin, 'test-owner', [calls[2]])['assignments'] == []
invoke_hook("on_session_end", session_id="chat", turn_id="turn-a", interrupted=False, completed=True, failed=False)
assert len(calls) == 7, calls
assert calls[2]["parent_execution_id"] == calls[0]["execution_id"]
assert calls[2]["contact_id"] == "test-owner"
assert calls[1]["platform"] == "cron"
assert calls[-2]["state"] == "interrupted" and calls[-1]["state"] == "completed"
assert "Private" not in json.dumps(calls)
print(json.dumps({"observations": len(calls), "native_hooks": True}))
'''


def test_native_execution_hooks_from_installed_wheel(artifacts, tmp_path):
    if importlib.util.find_spec("hermes_cli") is None:
        pytest.skip("Install qualified Hermes to exercise native lifecycle")
    _, _, _, installed = artifacts
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / "profile"), HERMES_BUNDLED_PLUGINS=str(tmp_path / "bundled"),
               PROTAGINE_GENERAL_PLUGIN_ACTIVE="1", PROTAGINE_MEMORY_WORKER_TOOLS="0", PROTAGINE_MEMORY_TURN_WRITER="disabled")
    result = run_python("-I", "-c", PROBE, installed, cwd=tmp_path, env=env)
    assert json.loads(result.stdout.splitlines()[-1])["native_hooks"] is True
