"""The memory provider sends a context section once per session while it stays the same.

Hermes appends each turn's recalled context to that turn's user message and replays it byte for byte on
every later turn, so a section the session's previous turn already carried is still in front of the model.
The provider keeps a fingerprint of each section it showed, per session; a section unchanged since the
session's last turn is one line ("unchanged since your last turn: <section>") in the new turn's context
only (earlier turns are never touched, so the cached prefix holds). A prefetch counts as shown only once
the turn it fed completed (``sync_turn``), Hermes waited for it, and it went inline whole; the fingerprints
reset on a session switch, before compression and on restart. Current Time is always sent fresh.
"""

import types

from test_protagine_memory_provider import (  # noqa: F401  (pytest fixture)
    _ASSEMBLE, _TEMPORAL, _FakeHttpx, _make_provider, provider_mod,
)

MEMORIES = {"id": "protagine-memories", "title": "Relevant Memories", "body": "The office is room 4.", "priority": 90}
WORK = {"id": "protagine-work", "title": "Work observed", "body": "Nothing in flight.", "priority": 80}


def _provider(provider_mod, monkeypatch, sections):
    fake = _FakeHttpx(routes={_ASSEMBLE: lambda request: {"sections": [dict(s) for s in sections]},
                              _TEMPORAL: {"title": "Current Time", "body": "Monday 09:00."}})
    p = _make_provider(provider_mod, fake, monkeypatch)
    p.initialize("s1", platform="cli")
    return p


def _turn(p, text, session="s1"):
    out = p.prefetch(text, session_id=session)
    p.sync_turn(text, "Done.", session_id=session)
    p.shutdown()
    return out


def test_a_section_unchanged_since_the_last_turn_is_one_line(provider_mod, monkeypatch):
    sections = [MEMORIES, WORK]
    p = _provider(provider_mod, monkeypatch, sections)
    first = _turn(p, "Where is my office?")
    assert "## Relevant Memories\nThe office is room 4." in first and "## Work observed" in first
    sections[1] = {**WORK, "body": "One task running."}
    second = _turn(p, "Anything running?")
    assert "unchanged since your last turn: Relevant Memories" in second
    assert "The office is room 4." not in second
    assert "## Work observed\nOne task running." in second                  # changed: sent whole
    assert "## Current Time" in second and "unchanged since your last turn: Current Time" not in second
    third = _turn(p, "And now?")
    assert "unchanged since your last turn: Relevant Memories" in third
    assert "unchanged since your last turn: Work observed" in third
    # Another session has seen nothing yet.
    other = p.prefetch("Where is my office?", session_id="s2")
    assert "The office is room 4." in other and "unchanged since" not in other


def test_only_a_completed_turn_counts_as_shown(provider_mod, monkeypatch):
    p = _provider(provider_mod, monkeypatch, [MEMORIES])
    p.prefetch("Where is my office?", session_id="s1")                     # the turn never completed
    again = p.prefetch("Where is my office, please?", session_id="s1")
    assert "The office is room 4." in again
    p.sync_turn("Some other message.", "Done.", session_id="s1")          # a late sync of another turn
    p.shutdown()
    assert "The office is room 4." in p.prefetch("Still there?", session_id="s1")


def test_fingerprints_reset_on_a_session_switch_before_compression_and_on_restart(provider_mod, monkeypatch):
    p = _provider(provider_mod, monkeypatch, [MEMORIES])
    _turn(p, "Where is my office?")
    assert "unchanged since" in _turn(p, "Again?")
    p.on_session_switch("s1", rewound=True)
    assert "The office is room 4." in _turn(p, "After undo?")
    p.on_pre_compress([{"role": "user", "content": "x"}])
    assert "The office is room 4." in _turn(p, "After compression?")
    p.on_session_switch("s2", parent_session_id="s1", reason="compression")
    assert "The office is room 4." in _turn(p, "In the continued session?", session="s2")
    restarted = _provider(provider_mod, monkeypatch, [MEMORIES])
    assert "The office is room 4." in _turn(restarted, "After a restart?")


def test_a_prefetch_hermes_stopped_waiting_for_is_never_counted(provider_mod, monkeypatch):
    p = _provider(provider_mod, monkeypatch, [MEMORIES])
    clock = [0.0]

    def monotonic():
        clock[0] += 5.0                                  # every read five seconds later: a slow sidecar
        return clock[0]
    monkeypatch.setattr(provider_mod, "_ttime", types.SimpleNamespace(time=lambda: 1_800_000_000.0,
                                                                       monotonic=monotonic))
    _turn(p, "Where is my office?")
    assert "The office is room 4." in _turn(p, "Again?")


def test_a_prefetch_hermes_spilled_to_a_file_is_never_counted(provider_mod, monkeypatch):
    p = _provider(provider_mod, monkeypatch, [MEMORIES])
    p._spill_cap = 20                                     # this output is past Hermes' inline limit
    _turn(p, "Where is my office?")
    assert "The office is room 4." in _turn(p, "Again?")
