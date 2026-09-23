"""Invariant property tests for the authority decision (evals 7.1) and its bindings.

The expected decisions come from an independent transcription of architecture
section 7.2 (levels x classes), 7.3 (the floor), 7.5 (the deny list) and 7.6
(budgets and the breaker), written here without reading the implementation.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings, strategies as st

from protagine.initiatives.store import InitiativeStore
from protagine.mind.authority import (
    Authority, CLASSES, LEVELS, MAY_CONTACT, Policy, classify, decide_table, floor_class, may_contact_of,
    new_ask_code,
)

# ---------------------------------------------------------------------------
# The independent table (architecture 7.2), transcribed by hand
# ---------------------------------------------------------------------------

LEVEL_TABLE = {
    # level: {class: decision}; contact is resolved by may_contact separately
    "suggest": {"internal": "act", "owner": "ask", "contact": "ask", "external": "ask", "floor": "ask"},
    "standard": {"internal": "act", "owner": "act", "contact": "by_may_contact", "external": "ask", "floor": "ask"},
    "trusted": {"internal": "act", "owner": "act", "contact": "by_may_contact", "external": "act", "floor": "ask"},
}
MAY_CONTACT_TABLE = {"auto": "act", "ask": "ask", "never": "drop"}


def expected_decision(level, cls, may_contact, floor, deny, budget_exhausted, breaker_tripped, enabled):
    """What section 7 says, in its own precedence: off, deny, floor, table, breaker, budget."""
    if not enabled or level == "off":
        return "drop"
    if deny:
        return "drop"
    if floor or cls == "floor":
        return "ask"                      # a floor action always resolves to ask; nothing raises it
    decision = LEVEL_TABLE[level][cls]
    if decision == "by_may_contact":
        decision = MAY_CONTACT_TABLE[may_contact]
    if level == "suggest" and cls == "contact":
        decision = "ask"                  # suggest: digest only for anything beyond internal
    if decision == "act" and breaker_tripped:
        decision = "ask"                  # the breaker demotes one level, never promotes
    if decision == "act" and budget_exhausted:
        decision = "defer"                # an exceeded budget defers; it is not an error
    return decision


ALL_COMBINATIONS = list(itertools.product(LEVELS, CLASSES, MAY_CONTACT, (False, True), (False, True),
                                          (False, True), (False, True), (False, True)))


def test_the_enumeration_covers_1920_combinations():
    assert len(ALL_COMBINATIONS) == 4 * 5 * 3 * 2 * 2 * 2 * 2 * 2 == 1920


@pytest.mark.parametrize("combination", ALL_COMBINATIONS,
                         ids=lambda c: "-".join(str(x) for x in c))
def test_decide_table_matches_the_architecture(combination):
    level, cls, may_contact, floor, deny, budget, breaker, enabled = combination
    assert decide_table(level=level, cls=cls, may_contact=may_contact, floor=floor, deny=deny,
                        budget_exhausted=budget, breaker_tripped=breaker, enabled=enabled) == \
        expected_decision(level, cls, may_contact, floor, deny, budget, breaker, enabled)


@settings(max_examples=300, deadline=None)
@given(level=st.sampled_from(LEVELS), cls=st.sampled_from(CLASSES), may_contact=st.sampled_from(MAY_CONTACT),
       floor=st.booleans(), deny=st.booleans(), budget=st.booleans(), breaker=st.booleans(), enabled=st.booleans())
def test_decision_invariants(level, cls, may_contact, floor, deny, budget, breaker, enabled):
    decision = decide_table(level=level, cls=cls, may_contact=may_contact, floor=floor, deny=deny,
                            budget_exhausted=budget, breaker_tripped=breaker, enabled=enabled)
    assert decision in {"act", "ask", "drop", "defer"}
    if not enabled or level == "off" or deny:
        assert decision == "drop"
    elif floor or cls == "floor":
        assert decision == "ask"                       # nothing raises the floor
    if may_contact == "never" and cls == "contact" and enabled and level != "off" and not deny and not floor:
        assert decision != "act"                       # a never contact is never contacted
    if decision == "act":
        assert not breaker and not budget              # a tripped breaker or an exhausted budget never acts
    if level == "suggest" and cls != "internal" and enabled and not deny:
        assert decision != "act"                       # suggest: digest only


# ---------------------------------------------------------------------------
# Classes, the floor and codes
# ---------------------------------------------------------------------------

def test_classify_by_kind_and_target():
    assert classify(kind="message", recipient="p-01", owner_id="p-01") == "owner"
    assert classify(kind="message", recipient="p-02", owner_id="p-01") == "contact"
    assert classify(kind="task", recipient=None, owner_id="p-01") == "internal"
    assert classify(kind="task", recipient="p-01", owner_id="p-01") == "owner"
    assert classify(kind="task", recipient="p-02", owner_id="p-01") == "contact"
    assert classify(kind="task", recipient=None, owner_id="p-01", toolsets=["web", "terminal"]) == "external"
    assert classify(kind="task", recipient=None, owner_id="p-01", text="wire $500 to the vendor") == "floor"
    assert classify(kind="note", recipient=None, owner_id="p-01") == "internal"


FLOOR_FIXTURE = [
    # (text, expected floor class or None)
    ("wire $500 to the vendor", "money_movement"),
    ("transfer money to the savings account", "money_movement"),
    ("pay the invoice, it is $1200", "money_movement"),
    ("send payment for the domain renewal", "money_movement"),
    ("buy the license for $49", "money_movement"),
    ("subscribe to the plan at $9/month", "money_movement"),
    ("settle the invoice", "money_movement"),                     # paraphrase: known miss
    ("move funds between accounts", "money_movement"),
    ("rm -rf /var/backups/old", "irreversible_deletion"),
    ("drop table users", "irreversible_deletion"),
    ("delete permanently the archived logs", "irreversible_deletion"),
    ("git push --force to main", "irreversible_deletion"),
    ("wipe the staging disk", "irreversible_deletion"),
    ("purge all cached artifacts", "irreversible_deletion"),
    ("erase everything in the temp bucket", "irreversible_deletion"),
    ("clear out the old backups", "irreversible_deletion"),       # paraphrase: known miss
    ("delete the repo", "irreversible_deletion"),                  # paraphrase: known miss
    ("rotate the API key for the mail service", "credential_change"),
    ("reset the admin password", "credential_change"),
    ("revoke the deploy token", "credential_change"),
    ("create a new ssh key for the build host", "credential_change"),
    ("change the security settings", "credential_change"),
    ("regenerate the certificate", "credential_change"),           # paraphrase: known miss
    ("send a bulk message to all contacts", "bulk_third_party_messaging"),
    ("email everyone about the outage", "bulk_third_party_messaging"),
    ("broadcast an sms to the whole list", "bulk_third_party_messaging"),
    ("dm all contacts the new address", "bulk_third_party_messaging"),
    ("text the whole team", "bulk_third_party_messaging"),         # paraphrase: known miss
    ("send the report to the owner", None),
    ("summarize the quarterly numbers", None),
    ("remind me to call the dentist", None),
    ("search the web for the venue", None),
    ("write the notes into the task workspace", None),
    ("draft a reply to the landlord", None),
    ("check whether the build passed", None),
    ("read the changelog and list the breaking changes", None),
    ("the price was $9", None),                                    # a single-digit amount is not movement
    ("look up the payment terms in the contract", None),
    ("what is the password policy document about", None),
    ("count the messages in the inbox", None),
    ("send me the address", None),
    ("compare the two proposals", None),
    ("schedule a follow-up for Friday", None),
    ("archive the finished task", None),
    ("tell me when the reply arrives", None),
    ("note that the meeting moved", None),
    ("the token count was 4096", None),
    ("list every contact I have", None),
    ("delete the draft paragraph in my notes", None),
    ("email the summary to the owner", None),
]


def test_floor_recall_fixture_reports_and_blocks_the_literal_forms():
    positives = [(text, cls) for text, cls in FLOOR_FIXTURE if cls]
    negatives = [text for text, cls in FLOOR_FIXTURE if cls is None]
    hits = sum(1 for text, cls in positives if floor_class(text) == cls)
    recall = hits / len(positives)
    false_positives = [text for text in negatives if floor_class(text)]
    assert not false_positives, false_positives
    assert recall >= 0.75, f"floor regex recall {recall:.2f}"      # the paraphrases are the known misses
    # every literal, tool-shaped positive is caught; the paraphrases need the structural floor
    for text in ("rm -rf /var/backups/old", "git push --force to main", "drop table users",
                 "wire $500 to the vendor", "rotate the API key for the mail service"):
        assert floor_class(text)


def test_ask_codes_avoid_ambiguous_letters_and_taken_codes():
    code = new_ask_code(["K7F"])
    assert len(code) == 3 and code != "K7F"
    assert not set("0O1IL") & set(code)


def test_may_contact_derivation_until_the_column_arrives():
    assert may_contact_of("p-01", owner_id="p-01") == "auto"
    assert may_contact_of({"contact_id": "p-02", "interaction_allowed": False}, owner_id="p-01") == "never"
    assert may_contact_of({"contact_id": "p-03", "interaction_allowed": True}, owner_id="p-01") == "ask"
    assert may_contact_of({"contact_id": "p-04", "may_contact": "auto"}, owner_id="p-01") == "auto"
    assert may_contact_of(None, owner_id="p-01") == "ask"


# ---------------------------------------------------------------------------
# Bound to the store: budgets, breaker, the guard
# ---------------------------------------------------------------------------

@pytest.fixture
def clocked_store(tmp_path):
    now = [datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)]
    store = InitiativeStore(state_dir=tmp_path)
    yield store, now
    store.close()


def _policy(**mind):
    return Policy.from_config({"autonomy": "standard", **mind})


def test_budget_defers_and_frees_up(clocked_store):
    store, now = clocked_store
    authority = Authority(_policy(budgets={"tasks_per_hour": 2, "concurrent_tasks": 5}), store,
                          owner_id="p-01", clock=lambda: now[0])
    for index in range(2):
        row, _ = store.create_intention(kind="task", type="t", title=f"task {index}", drive="duty", cls="owner",
                                        decision="act", decision_reason="r", status="approved",
                                        dedup_key=f"k{index}", recipient="p-01", created_at=now[0])
        store.transition(row.id, "approved", action="queued", at=now[0])
    verdict = authority.decide(kind="task", recipient="p-01", text="another", may_contact="auto")
    assert verdict.decision == "defer" and "tasks per hour" in verdict.reason
    now[0] += timedelta(hours=1, minutes=1)
    for row in store.intentions(limit=10):
        store.transition(row.id, "done", action="outcome_done", outcome="done", at=now[0])
    assert authority.decide(kind="task", recipient="p-01", text="another", may_contact="auto").decision == "act"


def test_breaker_trips_after_three_failures_and_resets(clocked_store):
    store, now = clocked_store
    authority = Authority(_policy(), store, owner_id="p-01", clock=lambda: now[0])
    for index in range(3):
        row, _ = store.create_intention(kind="task", type="t", title=f"task {index}", drive="duty", cls="owner",
                                        decision="act", decision_reason="r", status="dispatched",
                                        dedup_key=f"f{index}", recipient="p-01", created_at=now[0])
        store.transition(row.id, "failed", action="outcome_failed", outcome="failed", failed_at=now[0], at=now[0])
    state = authority.breaker_state("owner")
    assert state["tripped"] and state["failures"] == 3
    verdict = authority.decide(kind="task", recipient="p-01", text="owner work", may_contact="auto")
    assert verdict.decision == "ask" and "breaker" in verdict.reason
    assert authority.decide(kind="task", recipient=None, text="internal work").decision == "act"
    authority.reset_breaker("owner")
    assert authority.breaker_state("owner")["tripped"] is False
    assert authority.decide(kind="task", recipient="p-01", text="owner work", may_contact="auto").decision == "act"
    now[0] += timedelta(hours=73)
    assert authority.breaker_state("owner")["tripped"] is False


def test_breaker_demotion_expires_on_its_own(clocked_store):
    store, now = clocked_store
    authority = Authority(_policy(), store, owner_id="p-01", clock=lambda: now[0])
    for index in range(3):
        row, _ = store.create_intention(kind="task", type="t", title=f"task {index}", drive="duty", cls="contact",
                                        decision="act", decision_reason="r", status="dispatched",
                                        dedup_key=f"g{index}", recipient="p-02", created_at=now[0])
        store.transition(row.id, "failed", action="outcome_failed", outcome="failed", failed_at=now[0], at=now[0])
    assert authority.breaker_state("contact")["tripped"]
    now[0] += timedelta(hours=72, minutes=1)
    assert authority.breaker_state("contact")["tripped"] is False


def test_guard_rules(clocked_store):
    store, now = clocked_store
    authority = Authority(_policy(deny={"tools": ["terminal"], "text": ["secret-project"]}), store,
                          owner_id="p-01", clock=lambda: now[0])
    assert authority.guard(tool="kanban_create", args={"assignee": "protagine-act", "idempotency_key": "x"})["allow"]
    assert not authority.guard(tool="kanban_create", args={"assignee": "default", "idempotency_key": "x"})["allow"]
    assert not authority.guard(tool="kanban_create", args={"assignee": "protagine-act", "idempotency_key": "mind:1"})["allow"]
    assert not authority.guard(tool="terminal", args={"command": "ls"})["allow"]
    assert not authority.guard(tool="write_file", args={"path": "notes.md", "content": "secret-project"})["allow"]
    floor = authority.guard(tool="terminal2", args={"command": "rm -rf /data"})
    assert floor["action"] == "ask" and floor["floor"] == "irreversible_deletion"
    authority.set_enabled(False)
    assert authority.guard(tool="write_file", args={"path": "a"})["reason"].startswith("the mind is off")
    assert authority.guard(tool="write_file", args={"path": "a"}, run="guest")["allow"]


def test_deny_list_drops_and_the_floor_always_asks(clocked_store):
    store, now = clocked_store
    authority = Authority(_policy(autonomy="trusted", deny={"text": [r"\bpayroll\b"]}), store,
                          owner_id="p-01", clock=lambda: now[0])
    assert authority.decide(kind="task", recipient=None, text="update the payroll sheet").decision == "drop"
    verdict = authority.decide(kind="task", recipient=None, text="wire $900 to the landlord")
    assert verdict.decision == "ask" and verdict.cls == "floor" and verdict.floor == "money_movement"
    assert authority.decide(kind="message", recipient="p-02", text="hello", may_contact="never").decision == "drop"
    assert authority.decide(kind="message", recipient="p-02", text="hello", may_contact="auto").decision == "act"


def test_level_off_is_the_off_switch_for_effects(clocked_store):
    store, now = clocked_store
    authority = Authority(_policy(autonomy="off"), store, owner_id="p-01", clock=lambda: now[0])
    assert authority.enabled is False
    verdict = authority.decide(kind="task", recipient=None, text="tidy the notes")
    assert verdict.decision == "drop" and verdict.reason == "autonomy level off"
    assert authority.guard(tool="write_file", args={"path": "a"})["reason"].startswith("autonomy level off")
    assert authority.guard(tool="write_file", args={"path": "a"}, run="guest")["allow"]
    authority.policy.level = "standard"
    assert authority.enabled is True
