"""A literal offset belongs to its clock, never a second embedded clock."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from pacomind.beliefs.source_time import (
    interpret_time_query, parse_source_date, source_event_time,
)


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("expression,expected", [
    ("17:15 +02:00 on 18 September 2026", "2026-09-18T15:15:00+00:00"),
    ("17:15+02:00 on 18 September 2026", "2026-09-18T15:15:00+00:00"),
    ("00:15:30 +05:45 on September 18, 2026", "2026-09-17T18:30:30+00:00"),
    ("23:15 -03:30 on 18 September 2026", "2026-09-19T02:45:00+00:00"),
    ("01:30 -04:00 on 1 November 2026", "2026-11-01T05:30:00+00:00"),
    ("01:30 -05:00 on 1 November 2026", "2026-11-01T06:30:00+00:00"),
    ("17:15 +00:00 on 18 September 2026", "2026-09-18T17:15:00+00:00"),
    ("2026-09-18T17:15:00+02:00", "2026-09-18T15:15:00+00:00"),
    ("2026-09-18T23:15:00-03:30", "2026-09-19T02:45:00+00:00"),
])
@pytest.mark.parametrize("quoted", [False, True])
def test_offset_clock_resolves_exact_utc_for_source_and_query(expression, expected, quoted):
    # The explicit offset must override the profile, including both folds of
    # its repeated local clock. No source occurrence timestamp is required.
    zone = "America/New_York"
    assert parse_source_date(expression, observed_at=None, timezone_name=zone) == expected
    assert source_event_time(expression, observed_at=None, timezone_name=zone) == {
        "expression": expression, "status": "resolved", "precision": "instant", "at": expected,
    }
    operand = json.dumps(expression) if quoted else expression
    for prefix, mode in [("Where was my office as of ", "valid_range"),
                         ("What did the camera record at ", "observed_range")]:
        query = interpret_time_query(prefix + operand + "?", now=NOW, timezone_name=zone)
        assert (query.mode, query.expression, query.start) == (mode, expression, expected)
        assert datetime.fromisoformat(query.end) == datetime.fromisoformat(expected) + timedelta(microseconds=1)
        if mode == "observed_range":
            assert query.accepts_observation(expected)
            assert not query.accepts_observation(query.end)
        else:
            assert query.accepts_claim({"valid_from": expected})
            assert not query.accepts_claim({"valid_from": query.end})


@pytest.mark.parametrize("offset", ["+24:00", "-24:00", "+01:60", "-02:99", "+2:00", "+0200", "+02:00:30"])
@pytest.mark.parametrize("quoted", [False, True])
def test_unsupported_offset_keeps_whole_operand_unresolved(offset, quoted):
    expression = f"17:15 {offset} on 18 September 2026"
    assert parse_source_date(expression, observed_at=None) is None
    assert source_event_time(expression, observed_at=None) == {
        "expression": expression, "status": "unresolved",
    }
    operand = json.dumps(expression) if quoted else expression
    for prefix in ["Where was my office as of ", "What did the camera record at "]:
        query = interpret_time_query(prefix + operand + "?", now=NOW)
        assert (query.mode, query.expression) == ("unresolved_time", expression)
        assert query.start is None and query.end is None
        assert not query.accepts_claim({"valid_from": "2026-01-01T00:00:00+00:00"})


@pytest.mark.parametrize("expression", [
    "17:15 +02:00 on 18 September 2026", "17:15 +24:00 on 18 September 2026",
])
def test_offset_in_quoted_report_is_still_evidence_not_request_window(expression):
    report = json.dumps("The camera recorded a visit at " + expression + ".")
    assert interpret_time_query("Inspect this report: " + report, now=NOW).mode == "current"
