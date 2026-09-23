"""Shared prompt, endpoint and owner-contact helpers for ``protagine init``."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def _prompt(prompt: str, default: str = "", non_interactive: bool = False, ask=None) -> str:
    """Prompt for input with a default value. Returns default on EOF or non-interactive mode.

    Also checks for PROTAGINE_INIT_DEFAULTS env var for scripted defaults.
    Format: PROTAGINE_INIT_DEFAULTS='key1=val1,key2=val2'

    ``ask`` is an injectable input callable (defaults to ``input``) so tests
    can script answers without monkeypatching stdin. UX is unchanged.
    """
    if non_interactive:
        return default

    # Check for scripted defaults
    defaults_env = os.environ.get("PROTAGINE_INIT_DEFAULTS", "")
    if defaults_env:
        for pair in defaults_env.split(","):
            if "=" in pair:
                key, val = pair.split("=", 1)
                # Map prompt keywords to defaults
                prompt_lower = prompt.lower()
                if key.lower() in prompt_lower or prompt_lower in key.lower():
                    return val

    suffix = f" [{default}]" if default else ""
    try:
        val = (ask or input)(f"{prompt}{suffix}: ").strip()
        return val or default
    except EOFError:
        # Gracefully handle piped input exhaustion
        print()  # Add newline for clean output
        return default
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)


def _check_port(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except (ConnectionRefusedError, OSError):
        return False


def _resolve_hermes_home(hermes_home: str | Path | None = None) -> Path:
    """Resolve the selected Hermes home without guessing profile layouts."""
    selected = hermes_home or os.environ.get("HERMES_HOME")
    return Path(selected or Path.home() / ".hermes").expanduser().resolve()


OPENAI_COMPAT_PROVIDERS = frozenset({
    "zai", "local", "custom", "lmstudio", "vllm", "openai",
    "openai-compatible", "openai_compatible",
})


def normalize_llm_base_url(url: str, provider: str) -> tuple[str, bool]:
    """Ensure ``baseUrl`` ends with ``/v1`` for OpenAI-compatible providers.

    A bare ``host:port`` base URL silently 404s on chat completions because
    LiteLLM appends ``/chat/completions`` directly. Returns ``(url, changed)``;
    non-OpenAI-compatible providers (e.g. ollama, anthropic) pass through.
    """
    if not url:
        return url, False
    if (provider or "").strip().lower() not in OPENAI_COMPAT_PROVIDERS:
        return url, False
    stripped = url.rstrip("/")
    if stripped.endswith("/v1"):
        return url, False
    return stripped + "/v1", True


def ensure_api_key(cfg: dict) -> tuple[dict, bool]:
    """Never persist an empty ``apiKey`` for OpenAI-compatible providers.

    LiteLLM requires a non-empty api_key even for keyless local servers
    (vLLM, LM Studio, llama.cpp, ...) — an empty string breaks the auth
    header. Defaults to ``"local-no-key"``. Returns ``(cfg, changed)``;
    the input dict is never mutated.
    """
    provider = (cfg.get("provider") or "").strip().lower()
    if provider in OPENAI_COMPAT_PROVIDERS and not (cfg.get("apiKey") or "").strip():
        fixed = dict(cfg)
        fixed["apiKey"] = "local-no-key"
        return fixed, True
    return cfg, False


def apply_llm_config_fixes(cfg: dict) -> tuple[dict, list[str]]:
    """Apply both LLM host-config footgun fixes. Returns ``(cfg, notes)``."""
    notes: list[str] = []
    fixed = dict(cfg)
    url, changed = normalize_llm_base_url(fixed.get("baseUrl", ""), fixed.get("provider", ""))
    if changed:
        fixed["baseUrl"] = url
        notes.append(f"baseUrl did not end with /v1 — normalized to {url}")
    fixed, changed = ensure_api_key(fixed)
    if changed:
        notes.append(
            'apiKey was empty — set to "local-no-key" '
            "(LiteLLM requires a non-empty value even for keyless servers)"
        )
    return fixed, notes


def write_llm_host_config(path: Path, cfg: dict) -> tuple[dict, list[str]]:
    """Persist the normalized LLM host config and return its repair notes."""
    import json
    fixed, notes = apply_llm_config_fixes(cfg)
    path.write_text(json.dumps(fixed, indent=2))
    return fixed, notes


def repair_persisted_llm_config() -> list[str]:
    """Normalize the saved host config for ``protagine doctor --fix``.

    Return repair notes, or an empty list when no change is needed.
    """
    import json
    from protagine import get_state_dir

    config_path = get_state_dir() / ".protagine-llm-config.json"
    if not config_path.exists():
        return []
    cfg = json.loads(config_path.read_text())
    fixed, notes = apply_llm_config_fixes(cfg)
    if notes:
        write_llm_host_config(config_path, fixed)
    return notes


async def build_owner_contact(
    store,
    display_name: str,
    handles: list[tuple[str, str]] | None = None,
    *,
    trust_tier: str = "inner_circle",
    interaction_allowed: bool = True,
    import_source: str = "wizard",
) -> str:
    """Create the owner contact (plus handles) and return its contact_id.

    The first handle becomes primary. A handle already owned by another
    contact is skipped rather than failing the owner record — identity
    fail-closed needs the owner cid to exist either way.
    """
    contact = await store.create(
        display_name=display_name,
        trust_tier=trust_tier,
        interaction_allowed=interaction_allowed,
        import_source=import_source,
    )
    for i, (gateway, address) in enumerate(handles or []):
        try:
            await store.add_handle(
                contact.contact_id,
                gateway=gateway,
                address=address,
                is_primary=(i == 0),
                source="wizard",
                verified=True,
            )
        except ValueError:
            # Address already assigned to another contact — skip it.
            continue
    return contact.contact_id


def run_init(root_dir: str | None = None, args=None) -> int:
    """Run ``protagine init`` and return its exit code."""
    from protagine.init import run_init as run
    if root_dir and args is not None and not getattr(args, "home", None):
        args.home = root_dir
    return run(args)
