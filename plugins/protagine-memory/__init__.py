"""Protagine memory provider plugin for Hermes."""
# Hermes also loads installed providers under a synthetic source namespace.
# Reuse the canonical class only when it is this exact source. A selected
# standalone profile must not silently import a different installed version.
from importlib.util import find_spec
from pathlib import Path

_canonical = find_spec("protagine_memory")
if _canonical is not None and _canonical.origin and Path(_canonical.origin).resolve() == Path(__file__).resolve():
    from protagine_memory.provider import ProtagineMemoryProvider
else:
    from .provider import ProtagineMemoryProvider

__all__ = ["ProtagineMemoryProvider"]


def register(ctx):
    """Register the Protagine memory provider + a pre_llm_call lifecycle hook.

    The hook lives HERE (a Hermes plugin), not in Hermes core, so it survives
    Hermes updates — Hermes core lives under hermes-agent/ and is replaced on
    update, while ~/.hermes/plugins/ is not.

    Hermes injects returned ``str`` / ``{"context": str}`` into the user
    message's API content, which it also retains for later history replay.
    ``pre_llm_call`` is the only lifecycle hook that carries the message sender
    (``sender_id``) and ``platform``, so BOTH contact resolution and current-time
    injection happen here (the old ``agent:start`` hook is not a valid hook name
    in this Hermes build and was silently dropped).

    - resolve the real contact from the sender → per-contact memory/affect/facts
      engage instead of 'default'
    - attach a one-line clock to the turns whose recalled context carries none
    """
    provider = ProtagineMemoryProvider()
    ctx.register_memory_provider(provider)

    def _pre_llm_call(**kwargs):
        # 1) Resolve the real Protagine contact from the message sender (cached per
        #    sender inside the provider) so per-contact memory engages.
        try:
            provider.resolve_contact(
                platform=str(kwargs.get("platform", "") or ""),
                user_id=str(kwargs.get("sender_id", "") or ""),
            )
        except Exception:
            pass
        # 2) A clock only for the turns whose recalled context will not carry one (a trivial
        #    prompt, where Hermes skips recall, or a turn with no bound participant). Hermes
        #    replays this text as history on every later turn, so it is one line and rare.
        try:
            message = kwargs.get("user_message")
            context = provider.turn_clock(
                session_id=str(kwargs.get("session_id", "") or ""),
                message=message if isinstance(message, str) else "",
            )
            if context:
                return {"context": context}
        except Exception:
            pass
        return None

    if hasattr(ctx, "register_hook"):
        try:
            from hermes_cli.plugins import VALID_HOOKS as _VALID
        except Exception:
            _VALID = None
        if _VALID is None or "pre_llm_call" in _VALID:
            try:
                ctx.register_hook("pre_llm_call", _pre_llm_call)
            except Exception:
                pass
