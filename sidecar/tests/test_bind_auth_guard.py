"""The sidecar must fail closed rather than serve an unauthenticated API on a
non-loopback interface (PROTAGINE_API_KEY unset + bind 0.0.0.0/LAN = open to net)."""
import pytest

from protagine.cli import _guard_bind_auth, _is_loopback_host


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", ""])
def test_loopback_recognized(host):
    assert _is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "10.0.0.1"])
def test_non_loopback_recognized(host):
    assert _is_loopback_host(host) is False


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_allowed_without_key(host, monkeypatch):
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    _guard_bind_auth(host)  # must not raise/exit


def test_non_loopback_with_key_allowed(monkeypatch):
    monkeypatch.setenv("PROTAGINE_API_KEY", "secret")
    _guard_bind_auth("0.0.0.0")  # must not raise/exit


def test_non_loopback_with_key_file_allowed(monkeypatch, tmp_path):
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    (tmp_path / "api.key").write_text("file-secret\n")
    _guard_bind_auth("0.0.0.0")  # the key file counts as configured auth


def test_non_loopback_without_key_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    monkeypatch.setenv("PROTAGINE_HOME", str(tmp_path))
    monkeypatch.delenv("PROTAGINE_ALLOW_OPEN_BIND", raising=False)
    with pytest.raises(SystemExit) as exc:
        _guard_bind_auth("0.0.0.0")
    assert exc.value.code == 2


def test_removed_open_bind_override_is_not_honored(monkeypatch):
    # The middleware only serves loopback callers without a key, so an
    # "open bind" override could never deliver what it promised.
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    monkeypatch.delenv("PROTAGINE_API_KEYRING_PATH", raising=False)
    monkeypatch.setenv("PROTAGINE_ALLOW_OPEN_BIND", "1")
    with pytest.raises(SystemExit):
        _guard_bind_auth("0.0.0.0")
