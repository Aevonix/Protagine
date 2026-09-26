"""The identity qualification pack counts calls to the owner's people tool."""
import json

from protagine.qualification.native_contact_identity import controls


def test_controls_count_protagine_people_calls_only():
    effects = {"tool_results": [
        {"call": {"name": "protagine_people", "arguments": json.dumps({"operation": "who", "q": "p-01"})},
         "result": json.dumps({"ok": True})},
        {"call": {"name": "tool_call", "arguments": json.dumps(
            {"calls": [{"name": "protagine_people", "arguments": {"operation": "inspect"}}]})}, "result": "{}"},
        {"call": {"name": "protagine_contacts", "arguments": json.dumps({"operation": "who"})}, "result": "{}"},
    ]}
    assert [call["arguments"]["operation"] for call in controls(effects)] == ["who", "inspect"]
