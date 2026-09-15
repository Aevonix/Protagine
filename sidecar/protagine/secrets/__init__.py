"""Protagine secrets management — pluggable credential vault."""

from protagine.secrets.manager import SecretsManager
from protagine.secrets.types import SecretType, ALL_SECRET_KEYS, VAULT_NAME

__all__ = ["SecretsManager", "SecretType", "ALL_SECRET_KEYS", "VAULT_NAME"]
