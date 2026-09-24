"""User-manager autostart for one configured instance, without a supervisor."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import time

from protagine.resources import OPEN_FILES


class ServiceError(RuntimeError):
    pass


#: Seconds between a crashed sidecar and its restart. An error at startup (a bad protagine.yaml,
#: a native panic) would otherwise re-import the whole sidecar every 5 s, all day.
RESTART_SECONDS = 30


def _private_write(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.service-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _systemd_quote(value):
    value = str(value)
    if any(c in value for c in '\n\r\0'):
        raise ServiceError('Service paths must not contain line breaks or NUL')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _systemd_path(value):
    # These scalar settings take the whole path, unlike ExecStart's argv parser.
    value = str(value)
    if any(c in value for c in '\n\r\0') or value.rstrip() != value or value.endswith('\\'):
        raise ServiceError('Unsupported line ending in service path')
    return value.replace('%', '%%')


class InstanceService:
    def __init__(self, state, hermes_home, *, python=None, platform=None, home=None, runner=None,
                 host=None, port=None):
        self.state = Path(state).resolve()
        self.hermes_home = Path(hermes_home).resolve()
        # Where the sidecar answers, for the readiness checks; ``protagine.yaml`` values
        # come in through these arguments, a service unit's through the environment.
        self.host = str(host or os.environ.get('PROTAGINE_SIDECAR_HOST', '127.0.0.1'))
        self.port = int(port or os.environ.get('PROTAGINE_SIDECAR_PORT', '7777'))
        # Resolving a venv's python symlink would select the system interpreter.
        self.python = os.path.abspath(python or sys.executable)
        self.platform = platform or sys.platform
        self.home = Path(home or Path.home())
        suffix = hashlib.sha256(os.fsencode(self.state)).hexdigest()[:20]
        self.label = 'ai.protagine.instance.' + suffix
        if self.platform == 'darwin':
            self.name = self.label + '.plist'
            self.link = self.home / 'Library/LaunchAgents' / self.name
            self.target = f'gui/{os.getuid()}/{self.label}'
        elif self.platform.startswith('linux'):
            self.name = 'protagine-' + suffix + '.service'
            config = Path(os.environ.get('XDG_CONFIG_HOME') or self.home / '.config')
            self.link = config / 'systemd/user' / self.name
            self.target = self.name
        else:
            raise ServiceError('Instance autostart supports Linux systemd user services and macOS launchd')
        self.definition = self.state / 'service' / self.name
        self.backup = self.definition.with_name(self.name + '.previous')
        # The rotating runtime log is the sidecar's own; the manager appends the process's raw
        # stdout/stderr (a native panic, a traceback before logging starts) to a file of its own,
        # so a rotation never leaves the manager writing into a renamed or unlinked file.
        self.log = self.state / 'service' / 'sidecar.log'
        self.manager_log = self.state / 'service' / ('launchd.log' if self.platform == 'darwin' else 'systemd.log')
        self.runner = runner or subprocess.run

    @classmethod
    def selected(cls):
        """The service of the instance ``protagine.yaml`` describes."""
        from protagine.config import ConfigError, apply_environment, load_config
        try:
            config = load_config(required=True)
        except ConfigError as exc:
            raise ServiceError(str(exc)) from None
        apply_environment(config)
        return cls(config.home, config.hermes_home)

    def _run(self, *args, check=True):
        try:
            result = self.runner(list(args), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceError(f'User service manager unavailable ({type(exc).__name__})') from None
        if check and result.returncode:
            raise ServiceError(f'User service command failed: {args[0]} {args[1]} (exit {result.returncode})')
        return result

    def _manager_ready(self):
        if self.platform == 'darwin':
            self._run('launchctl', 'print', f'gui/{os.getuid()}')
        else:
            self._run('systemctl', '--user', 'show-environment')

    def _owned(self):
        if not self.link.exists() and not self.link.is_symlink():
            return False
        if not self.link.is_symlink() or self.link.resolve() != self.definition:
            raise ServiceError(f'Refusing an unowned service definition at {self.link}')
        if not self.definition.is_file():
            raise ServiceError('Private service definition is missing; reinstall it before managing the service')
        return True

    def render(self):
        arguments = [self.python, '-m', 'protagine', 'start']
        environment = {'PROTAGINE_HOME': str(self.state), 'HERMES_HOME': str(self.hermes_home),
                       'PROTAGINE_INSTANCE_SERVICE': self.label, 'PYTHONUNBUFFERED': '1'}
        if self.platform == 'darwin':
            # The vector store holds a descriptor per data file; macOS starts a user
            # process at 256. Both limits, so the server's own raise has room.
            limits = {'NumberOfFiles': OPEN_FILES}
            return plistlib.dumps({'Label': self.label, 'ProgramArguments': arguments,
                'WorkingDirectory': str(self.state), 'EnvironmentVariables': environment,
                'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': RESTART_SECONDS,
                'ExitTimeOut': 20, 'Umask': 0o077,
                'SoftResourceLimits': limits, 'HardResourceLimits': dict(limits),
                'StandardOutPath': str(self.manager_log), 'StandardErrorPath': str(self.manager_log)},
                sort_keys=True)
        quote = _systemd_quote
        return ('[Unit]\nDescription=Protagine private instance ' + self.label + '\n\n[Service]\nType=exec\n'
                'WorkingDirectory=' + _systemd_path(self.state) + '\n'
                # ':' disables dollar-variable substitution; %% escapes specifiers.
                'ExecStart=:' + ' '.join(quote(arg) for arg in arguments) + '\n'
                'Environment=' + ' '.join(quote(key + '=' + value) for key, value in environment.items()) + '\n'
                'Restart=always\nRestartSec=' + str(RESTART_SECONDS) + '\nTimeoutStopSec=20\nUMask=0077\n'
                'LimitNOFILE=' + str(OPEN_FILES) + '\n'
                'StandardOutput=append:' + _systemd_path(self.manager_log) + '\n'
                'StandardError=append:' + _systemd_path(self.manager_log) + '\n\n'
                '[Install]\nWantedBy=default.target\n').encode()

    def status(self):
        owned = self._owned()
        self._manager_ready()
        result = {'instance': str(self.state), 'manager': 'launchd' if self.platform == 'darwin' else 'systemd-user',
                  'label': self.label, 'definition': str(self.definition), 'installed': owned,
                  'loaded': False, 'running': False, 'pid': None, 'autostart_scope': 'user-session'}
        if self.platform == 'darwin':
            response = self._run('launchctl', 'print', self.target, check=False)
            result['loaded'] = response.returncode == 0
            pid = re.search(r'^\s*pid = ([0-9]+)\s*$', response.stdout, re.MULTILINE)
            if pid:
                result['pid'], result['running'] = int(pid[1]), True
        else:
            response = self._run('systemctl', '--user', 'show', self.name,
                '--property=LoadState,ActiveState,SubState,MainPID,UnitFileState', '--no-pager', check=False)
            fields = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
            result.update(state=fields.get('ActiveState', 'unknown'), substate=fields.get('SubState', 'unknown'),
                          enabled=fields.get('UnitFileState') == 'enabled')
            result['loaded'] = fields.get('LoadState') == 'loaded'
            pid = fields.get('MainPID', '0')
            result['pid'] = int(pid) if pid.isdigit() and int(pid) > 0 else None
            result['running'] = bool(result['pid'] and fields.get('ActiveState') in {'active', 'activating'})
        return result

    def install(self):
        content = self.render()  # Validate every path before writing anything.
        self._manager_ready()
        owned = self._owned()
        previous = self.definition.read_bytes() if self.definition.is_file() else None
        if previous != content and owned:
            status = self.status()
            if status['running'] or (self.platform == 'darwin' and status['loaded']):
                raise ServiceError('Stop this instance service before changing its Python environment')
        try:
            if previous is not None and previous != content:
                _private_write(self.backup, previous)
            _private_write(self.definition, content)
            self.link.parent.mkdir(parents=True, exist_ok=True)
            if not owned:
                self.link.symlink_to(self.definition)
            for log in (self.log, self.manager_log):
                if not log.exists():
                    _private_write(log, b'')
            if self.platform == 'darwin':
                self._run('launchctl', 'enable', self.target)
            else:
                self._run('systemctl', '--user', 'daemon-reload')
                self._run('systemctl', '--user', 'enable', self.name)
        except (ServiceError, OSError):
            # Preserve the previous definition; no instance was started here.
            if previous is not None:
                _private_write(self.definition, previous)
            if not owned and self.link.is_symlink() and self.link.resolve() == self.definition:
                if self.platform != 'darwin':
                    self._run('systemctl', '--user', 'disable', self.name, check=False)
                # A failed enable may have created its exact wanted-by link.
            if not owned and self.link.is_symlink() and self.link.resolve() == self.definition:
                self.link.unlink()
            if previous is None:
                self.definition.unlink(missing_ok=True)
            if self.platform != 'darwin':
                self._run('systemctl', '--user', 'daemon-reload', check=False)
            raise
        return self.status()

    def _require_installed(self):
        if not self._owned():
            raise ServiceError('Service is not installed for this instance; run protagine service install')
        self._manager_ready()

    def health(self):
        """The served health verdict, ``{'status', 'problems'}``, or None when the sidecar does not answer.

        Readiness is the sidecar answering ``/v1/host/health``; what it answers (``ok``, or
        ``degraded`` with its reasons in words) is reported as it is, so ``service start``,
        ``service status`` and ``protagine doctor`` all read the one verdict rather than each
        deciding for itself what ready means.
        """
        import httpx
        host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(self.host, self.host)
        if ':' in host:
            host = '[' + host + ']'
        key = os.environ.get('PROTAGINE_CLIENT_API_KEY') or os.environ.get('PROTAGINE_API_KEY', '')
        try:
            response = httpx.get(f'http://{host}:{self.port}/v1/host/health',
                headers={'Authorization': 'Bearer ' + key}, timeout=5, trust_env=False, follow_redirects=False)
            if response.status_code != 200:
                return None
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(body, dict) or not body.get('status'):
            return None
        return {'status': str(body['status']), 'problems': [str(item) for item in body.get('problems') or []]}

    def healthy(self):
        return self.health() is not None

    def start(self, *, restart=False, timeout=30):
        self._require_installed()
        status = self.status()
        if not status['running']:
            host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(self.host, self.host)
            try:
                with socket.create_connection((host, self.port), timeout=.2):
                    raise ServiceError('Instance port is already occupied; stop its current process before service start')
            except OSError:
                pass
        if self.platform == 'darwin':
            if status['loaded']:
                if restart:
                    self._run('launchctl', 'kickstart', '-k', self.target)
            else:
                self._run('launchctl', 'bootstrap', f'gui/{os.getuid()}', str(self.link))
        else:
            self._run('systemctl', '--user', 'restart' if restart else 'start', self.name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.status()
            served = self.health() if result['running'] else None
            if served is not None:
                return {**result, 'ready': True, 'health': served['status'], 'problems': served['problems']}
            time.sleep(.2)
        raise ServiceError(f'Service did not become HTTP-ready (no answer from /v1/host/health); inspect {self.log} '
                           f'and {self.manager_log}. It remains installed for recovery.')

    def stop(self):
        self._require_installed()
        if self.platform == 'darwin':
            if self.status()['loaded']:
                self._run('launchctl', 'bootout', self.target)
        else:
            self._run('systemctl', '--user', 'stop', self.name)
        # launchctl bootout can return while its process is still shutting down.
        # Do not remove the definition or report a stopped service prematurely.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.status()
            if not result['running'] and (self.platform != 'darwin' or not result['loaded']):
                return result
            time.sleep(.1)
        raise ServiceError('User manager has not finished stopping this instance; its registration is retained')

    def uninstall(self):
        if not self._owned():
            return self.status()
        self.stop()
        if self.platform != 'darwin':
            self._run('systemctl', '--user', 'disable', self.name)
        # systemctl disable may remove this exact link itself.
        if self.link.is_symlink() and self.link.resolve() == self.definition:
            self.link.unlink()
        if self.platform != 'darwin':
            self._run('systemctl', '--user', 'daemon-reload')
        return self.status()


def manage(action):
    service = InstanceService.selected()
    if action == 'restart':
        result = service.start(restart=True)
    else:
        result = getattr(service, action)()
    if action == 'status':
        served = service.health() if result['running'] else None
        result['ready'] = served is not None
        result['health'] = served['status'] if served else None
        result['problems'] = served['problems'] if served else []
    print(json.dumps(result, sort_keys=True))
