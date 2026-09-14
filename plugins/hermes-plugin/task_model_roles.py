"""Current named native-task roles shared by gateway and private transports.

These reads resolve configured roles only. Callers own participant authorization
and durable admission. Returned snapshots contain no provider credentials.
"""
from .task_handoffs import TaskHandoffError


def _task_role_config(platform_name, error_type):
    """Read only role settings from the selected profile for new work."""
    from hermes_cli.config import load_config_readonly
    platforms = load_config_readonly().get('platforms', {})
    if not isinstance(platforms, dict):
        raise error_type('Native task platform configuration is invalid')
    platform = platforms.get(platform_name, {})
    if not isinstance(platform, dict) or not isinstance(platform.get('extra', {}), dict):
        raise error_type('Native task role configuration is invalid')
    return platform.get('extra', {})


def configured_task_model_roles(platform_name='pacomind_task', *, error_type=TaskHandoffError):
    roles = _task_role_config(platform_name, error_type).get('task_model_roles', {})
    if not isinstance(roles, dict):
        raise error_type('Native task model roles are invalid')
    return roles


def resolve_task_model_role(platform_name='pacomind_task', retained=None, *, error_type=TaskHandoffError):
    """One explicit native projection; credentials remain native-owned."""
    selected = retained if retained is not None else _task_role_config(platform_name, error_type).get('task_model_role')
    if selected is None:
        return None
    if (not isinstance(selected, dict) or set(selected) != {'role', 'provider', 'model'}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 256
                   for value in selected.values())
            or selected['provider'] in {'auto', 'custom'}):
        raise error_type('Native task model role is invalid')
    from hermes_cli.config import load_config_readonly
    providers = load_config_readonly().get('providers') or {}
    if not isinstance(providers, dict) or not isinstance(providers.get(selected['provider']), dict):
        raise error_type('Native task model role provider is unavailable')
    from hermes_cli.runtime_provider import has_named_custom_provider
    if not has_named_custom_provider(selected['provider']):
        raise error_type('Native task model role provider has no enabled native route')
    return dict(selected)


def select_task_model_role(role, platform_name='pacomind_task', *, error_type=TaskHandoffError):
    """Resolve a named task role from the profile, before durable admission."""
    roles = configured_task_model_roles(platform_name, error_type=error_type)
    if (not isinstance(role, str) or not role.strip() or len(role) > 256
            or not isinstance(roles, dict) or role not in roles):
        raise error_type('The requested task model role is not configured')
    selected = roles[role]
    if not isinstance(selected, dict) or selected.get('role') != role:
        raise error_type('The configured task model role name does not match')
    return resolve_task_model_role(platform_name, selected, error_type=error_type)
