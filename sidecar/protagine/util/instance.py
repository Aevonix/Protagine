"""Load the selected instance's configuration into the process environment."""
from pathlib import Path
import os


def plugin_settings(config):
    """Read the plugin's settings mapping from a Hermes config without changing it."""
    plugins = config.get('plugins', {}) if isinstance(config, dict) else {}
    if not isinstance(plugins, dict):
        raise ValueError('Hermes plugins must be a mapping')
    selected = plugins.get('protagine')
    if selected is not None and not isinstance(selected, dict):
        raise ValueError('Hermes Protagine plugin settings must be a mapping')
    return selected or {}


def load_environment():
    """Export ``protagine.yaml`` (or a legacy ``.env``) to ``os.environ`` once at startup.

    Runtime reads never rewrite the process environment; this is the explicit
    instance-selection boundary used by the CLI and the service.
    """
    environment = dict(os.environ)
    if (environment.get('PROTAGINE_SKIP_DOTENV', '').lower() in {'1', 'true', 'yes', 'on'}
            and environment.get('PROTAGINE_INSTANCE_SELECTED') != '1'):
        return
    from protagine.config import CONFIG_FILE, apply_environment, instance_home, load_config
    home = instance_home()
    if (home / CONFIG_FILE).is_file():
        apply_environment(load_config(home))
        return
    # Unconfigured library use: a plain .env next to the state or in the cwd.
    for path in (home / '.env', Path.home() / '.protagine' / '.env', Path.cwd() / '.env'):
        if path.is_file():
            for line in path.read_text().splitlines():
                if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ.setdefault(key.strip(), value.strip())
            break
