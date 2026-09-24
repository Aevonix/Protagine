"""The server raises its own open-file limit where the hard limit allows, and never lowers it."""
import logging
import resource

import pytest

from protagine.resources import OPEN_FILES, raise_open_file_limit


@pytest.fixture
def restore_nofile():
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    yield before
    resource.setrlimit(resource.RLIMIT_NOFILE, before)


def test_soft_limit_is_raised_to_the_target_within_the_hard_limit(restore_nofile, caplog):
    soft, hard = restore_nofile
    low = 256 if hard == resource.RLIM_INFINITY else min(256, hard)
    resource.setrlimit(resource.RLIMIT_NOFILE, (low, hard))
    with caplog.at_level(logging.INFO, logger="protagine.resources"):
        result = raise_open_file_limit()
    expected = OPEN_FILES if hard == resource.RLIM_INFINITY else min(OPEN_FILES, hard)
    assert result == (expected, hard)
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == (expected, hard)
    if expected < OPEN_FILES:
        assert "below the" in caplog.text and "hard limit" in caplog.text
    else:
        assert "open file limit: %d" % expected in caplog.text or f"open file limit: {expected}" in caplog.text


def test_a_soft_limit_already_above_the_target_is_left_alone(restore_nofile):
    soft, hard = restore_nofile
    if hard != resource.RLIM_INFINITY and hard <= OPEN_FILES:
        pytest.skip("the hard limit here cannot exceed the target")
    higher = OPEN_FILES + 1
    resource.setrlimit(resource.RLIMIT_NOFILE, (higher, hard))
    assert raise_open_file_limit() == (higher, hard)
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == higher


def test_a_custom_target_is_honoured(restore_nofile):
    soft, hard = restore_nofile
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(300, soft if soft != resource.RLIM_INFINITY else 300), hard))
    assert raise_open_file_limit(1024)[0] == (1024 if hard == resource.RLIM_INFINITY else min(1024, hard))
