"""``protagine doctor``: install checks.

Seven things can be wrong with an install, and each has one remedy:

- the Hermes version is outside the supported range
- ``protagine.yaml``, ``api.key`` or the Hermes keys ``init`` writes are missing
- the adapter version in Hermes' environment does not match the sidecar
- ``pip check`` in Hermes' environment is not clean
- the sidecar is not reachable with the key
- the plugin is not loaded (not enabled, or not installed where Hermes runs)
- semantic recall is configured (``router.embed_url``) but the embedder is not serving
- the vector store library is missing from the sidecar's own environment

Local checks read files and run Hermes' Python; the sidecar check talks HTTP
and degrades to a failure with the start command when the sidecar is down.
Every check runs defensively: an exception inside a check becomes a ``fail``
result, never a crashed run.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

PASS = "pass"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

#: Providers that route through LiteLLM's openai/* path; their baseUrl must end with /v1.
OPENAI_COMPAT_PROVIDERS = frozenset({"zai", "local", "custom", "lmstudio", "vllm", "openai"})


@dataclass
class CheckResult:
    """One diagnostic verdict."""

    name: str
    status: str  # "pass" | "warn" | "fail" | "skip"
    detail: str = ""
    remedy: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _run(name: str, fn: Callable[..., Any], *args: Any) -> List[CheckResult]:
    """Run one check defensively; an exception becomes a fail result."""
    try:
        result = fn(*args)
    except Exception as exc:  # noqa: BLE001 - the whole point
        return [CheckResult(name=name, status=FAIL,
                            detail=f"check crashed: {type(exc).__name__}: {exc}")]
    if isinstance(result, CheckResult):
        return [result]
    return list(result)


def _http_get(url: str, api_key: str = "", timeout: float = 10.0) -> Tuple[int, Any]:
    request = urllib.request.Request(url)
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


# ---------------------------------------------------------------------------
# Local checks
# ---------------------------------------------------------------------------

def check_config() -> CheckResult:
    """``protagine.yaml`` exists and validates."""
    from protagine.config import ConfigError, config_path, load_config
    path = config_path()
    if not path.is_file():
        return CheckResult("config", FAIL, detail=f"{path} is missing",
                           remedy="run 'protagine init'")
    try:
        config = load_config(required=True)
    except ConfigError as exc:
        return CheckResult("config", FAIL, detail=str(exc), remedy=f"fix {path} and re-run 'protagine doctor'")
    environment = config.get("environment") or {}
    detail = f"{path} valid"
    if environment:
        detail += f" (environment: {len(environment)} entr{'y' if len(environment) == 1 else 'ies'})"
    return CheckResult("config", PASS, detail=detail)


def check_api_key() -> CheckResult:
    """``api.key`` exists, has one token and is private."""
    from protagine.config import key_file_is_private, key_path, read_api_key
    path = key_path()
    if os.environ.get("PROTAGINE_API_KEY", "").strip():
        return CheckResult("api-key", PASS, detail="PROTAGINE_API_KEY is set in the environment")
    if not path.is_file():
        return CheckResult("api-key", FAIL, detail=f"{path} is missing", remedy="run 'protagine init'")
    if not read_api_key():
        return CheckResult("api-key", FAIL, detail=f"{path} is empty", remedy="run 'protagine init'")
    if not key_file_is_private(path):
        return CheckResult("api-key", WARN, detail=f"{path} is readable by other users",
                           remedy=f"chmod 600 {path}")
    return CheckResult("api-key", PASS, detail=f"{path} present (mode 600)")


def check_identity() -> CheckResult:
    from protagine.config import identity_path, load_identity
    path = identity_path()
    if not path.is_file():
        return CheckResult("identity", WARN, detail=f"{path} is missing", remedy="run 'protagine init'")
    identity = load_identity()
    if not identity.get("owner", {}).get("name") or not identity.get("agent", {}).get("name"):
        return CheckResult("identity", WARN, detail=f"{path} lacks the owner or agent name",
                           remedy="run 'protagine init'")
    return CheckResult("identity", PASS, detail=f"{path} names the owner and the agent")


def check_llm_config() -> CheckResult:
    """The router's persisted host config points at an OpenAI-compatible root."""
    from protagine import get_state_dir
    from protagine.config import LLM_CONFIG_FILE
    path = get_state_dir() / LLM_CONFIG_FILE
    if not path.exists():
        return CheckResult("llm-config", WARN, detail=f"{path} not found; the router has no endpoint",
                           remedy="run 'protagine init --model-url <root>' or POST /v1/host/configure")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("top-level JSON value is not an object")
    except (OSError, ValueError) as exc:
        return CheckResult("llm-config", FAIL, detail=f"{path} is unreadable: {exc}",
                           remedy="fix or delete the file, then re-run 'protagine init'")
    provider = str(cfg.get("provider", "") or "").strip().lower()
    base_url = str(cfg.get("baseUrl", "") or "").strip()
    if provider in OPENAI_COMPAT_PROVIDERS and base_url and not base_url.rstrip("/").endswith("/v1"):
        return CheckResult("llm-config", WARN,
                           detail=f"baseUrl {base_url!r} does not end with /v1",
                           remedy="run 'protagine doctor --fix'")
    if provider in OPENAI_COMPAT_PROVIDERS and not str(cfg.get("apiKey") or "").strip():
        return CheckResult("llm-config", WARN, detail="apiKey is empty", remedy="run 'protagine doctor --fix'")
    return CheckResult("llm-config", PASS, detail=f"{path} parsed (provider={provider or 'unknown'})")


def _hermes_python() -> Optional[Path]:
    from protagine.config import load_config
    from protagine.init import find_hermes_executable, python_of_executable
    config = load_config()
    configured = str(config.get("hermes.python") or "")
    if configured and Path(configured).exists():
        return Path(configured)
    executable = find_hermes_executable()
    return python_of_executable(executable) if executable else None


def check_hermes_version() -> CheckResult:
    from protagine.init import SUPPORTED_HERMES_RANGE, hermes_python_version, hermes_version_supported
    python = _hermes_python()
    if python is None:
        return CheckResult("hermes-version", FAIL, detail="no Hermes interpreter found",
                           remedy="install Hermes and re-run 'protagine init'")
    version = hermes_python_version(python)
    if not version:
        return CheckResult("hermes-version", FAIL, detail=f"hermes-agent is not installed in {python}",
                           remedy="re-run 'protagine init --hermes-python <interpreter>'")
    if not hermes_version_supported(version):
        return CheckResult("hermes-version", FAIL,
                           detail=f"Hermes {version} is outside {SUPPORTED_HERMES_RANGE}",
                           remedy="install a supported Hermes release, then 'protagine upgrade'")
    return CheckResult("hermes-version", PASS, detail=f"Hermes {version} at {python}")


def check_adapter_version() -> CheckResult:
    from protagine import __version__
    from protagine.init import ADAPTER_DISTRIBUTION, installed_adapter_version
    python = _hermes_python()
    if python is None:
        return CheckResult("adapter-version", SKIP, detail="no Hermes interpreter found")
    installed = installed_adapter_version(python)
    if installed is None:
        return CheckResult("adapter-version", FAIL, detail=f"{ADAPTER_DISTRIBUTION} is not installed in {python}",
                           remedy="run 'protagine init' (or 'protagine upgrade')")
    if installed != __version__:
        return CheckResult("adapter-version", FAIL,
                           detail=f"{ADAPTER_DISTRIBUTION} {installed} does not match the sidecar {__version__}",
                           remedy="run 'protagine upgrade', then 'hermes gateway restart'")
    return CheckResult("adapter-version", PASS, detail=f"{ADAPTER_DISTRIBUTION} {installed} matches the sidecar")


def check_pip_check() -> CheckResult:
    from protagine.init import InitError, pip_check
    python = _hermes_python()
    if python is None:
        return CheckResult("pip-check", SKIP, detail="no Hermes interpreter found")
    try:
        ok, output = pip_check(python)
    except InitError as exc:
        return CheckResult("pip-check", WARN, detail=str(exc))
    if not ok:
        return CheckResult("pip-check", FAIL, detail=output.splitlines()[0] if output else "pip check failed",
                           remedy="run 'protagine upgrade'; if it persists, reinstall Hermes")
    return CheckResult("pip-check", PASS, detail=f"pip check clean in {python}")


def check_hermes_keys() -> CheckResult:
    """The Hermes config carries every key ``init`` writes."""
    from protagine.config import KEY_FILE, load_config
    from protagine.init import SKILLS_DIR, read_hermes_config, reconcile_hermes_config
    config = load_config()
    path = config.hermes_home / "config.yaml"
    if not path.is_file():
        return CheckResult("hermes-keys", FAIL, detail=f"{path} is missing", remedy="run 'protagine init'")
    current = read_hermes_config(path)
    _, changes = reconcile_hermes_config(current, sidecar_url=config.sidecar_url,
                                         key_file=config.home / KEY_FILE,
                                         skills_dir=config.home / SKILLS_DIR)
    if changes:
        return CheckResult("hermes-keys", FAIL, detail="missing: " + "; ".join(changes),
                           remedy="run 'protagine upgrade', then 'hermes gateway restart'")
    return CheckResult("hermes-keys", PASS, detail=f"{path} carries the adapter keys")


def check_worker_profile() -> CheckResult:
    from protagine.config import load_config
    from protagine.init import WORKER_PROFILE, profiles_root
    config = load_config()
    path = profiles_root(config.hermes_home) / WORKER_PROFILE / "config.yaml"
    if not path.is_file():
        return CheckResult("worker-profile", FAIL, detail=f"{path} is missing",
                           remedy="run 'protagine upgrade'")
    return CheckResult("worker-profile", PASS, detail=f"profile {WORKER_PROFILE} present")


def check_plugin_loaded() -> CheckResult:
    """The plugin is enabled in Hermes' config and registered where Hermes runs."""
    from protagine.config import load_config
    from protagine.init import PLUGIN_NAME, read_hermes_config
    config = load_config()
    current = read_hermes_config(config.hermes_home / "config.yaml")
    enabled = (current.get("plugins") or {}).get("enabled") if isinstance(current.get("plugins"), dict) else None
    if not isinstance(enabled, list) or PLUGIN_NAME not in enabled:
        return CheckResult("plugin-loaded", FAIL, detail="plugins.enabled lacks protagine",
                           remedy="run 'protagine upgrade', then 'hermes gateway restart'")
    python = _hermes_python()
    if python is None:
        return CheckResult("plugin-loaded", SKIP, detail="no Hermes interpreter found")
    import subprocess
    result = subprocess.run(
        [str(python), "-I", "-c",
         "import importlib.metadata as m, json; "
         "print(json.dumps(sorted({e.name for e in m.entry_points(group='hermes_agent.plugins')} | "
         "{e.name for e in m.entry_points(group='hermes_agent.memory_providers')})))"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    names = set()
    if result.returncode == 0 and result.stdout.strip():
        try:
            names = set(json.loads(result.stdout.strip().splitlines()[-1]))
        except ValueError:
            names = set()
    missing = {PLUGIN_NAME, "protagine-memory"} - names
    if missing:
        return CheckResult("plugin-loaded", FAIL,
                           detail=f"entry points not registered in {python}: {', '.join(sorted(missing))}",
                           remedy="run 'protagine upgrade', then 'hermes gateway restart'")
    return CheckResult("plugin-loaded", PASS, detail="plugin enabled and its entry points registered")


def check_vector_store() -> CheckResult:
    """The vector store library imports in the sidecar's own interpreter."""
    import sys
    from protagine.init import VECTOR_STORE_MODULE, VECTOR_STORE_REMEDY, vector_store_available
    if not vector_store_available():
        return CheckResult("vector-store", FAIL, detail=f"{VECTOR_STORE_MODULE} is not importable in {sys.executable}",
                           remedy=VECTOR_STORE_REMEDY)
    return CheckResult("vector-store", PASS, detail=f"{VECTOR_STORE_MODULE} importable in {sys.executable}")


def run_local_checks() -> List[CheckResult]:
    results: List[CheckResult] = []
    results += _run("vector-store", check_vector_store)
    results += _run("config", check_config)
    results += _run("api-key", check_api_key)
    results += _run("identity", check_identity)
    results += _run("llm-config", check_llm_config)
    results += _run("hermes-version", check_hermes_version)
    results += _run("adapter-version", check_adapter_version)
    results += _run("pip-check", check_pip_check)
    results += _run("hermes-keys", check_hermes_keys)
    results += _run("worker-profile", check_worker_profile)
    results += _run("plugin-loaded", check_plugin_loaded)
    return results


# ---------------------------------------------------------------------------
# Sidecar check (HTTP against the running sidecar)
# ---------------------------------------------------------------------------

def check_sidecar(base_url: str, api_key: str, timeout: float) -> List[CheckResult]:
    base_url = base_url.rstrip("/")
    try:
        status, body = _http_get(f"{base_url}/v1/host/health", api_key, timeout)
    except Exception as exc:  # noqa: BLE001 - URLError, OSError, timeouts
        return [CheckResult("sidecar", FAIL, detail=f"sidecar not reachable at {base_url}: {exc}",
                            remedy="start it with 'protagine service start' (or 'protagine start')")]
    if status != 200 or not isinstance(body, dict):
        return [CheckResult("sidecar", FAIL, detail=f"/v1/host/health returned HTTP {status}")]
    detail = f"sidecar at {base_url} reports status={body.get('status', 'unknown')}"
    problems = [str(item) for item in body.get("problems") or []]
    if problems:
        detail += ": " + "; ".join(problems)
    results = [CheckResult("sidecar", PASS if body.get("status") == "ok" else WARN, detail=detail)]
    results.append(_check_open_files((body.get("notes") or {}).get("fd_limit")))
    try:
        status, _ = _http_get(f"{base_url}/v1/mind/state", api_key, timeout)
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("sidecar-auth", FAIL, detail=f"authenticated request failed: {exc}"))
        return results
    if status in (401, 403):
        results.append(CheckResult("sidecar-auth", FAIL, detail=f"the key was rejected (HTTP {status})",
                                   remedy="the running sidecar reads another api.key; restart it with "
                                          "'protagine service restart'"))
    elif not 200 <= status < 300:
        results.append(CheckResult("sidecar-auth", WARN, detail=f"authenticated request returned HTTP {status}"))
    else:
        results.append(CheckResult("sidecar-auth", PASS,
                                   detail="authenticated request accepted" + ("" if api_key else " (no key: dev mode)")))
    return results


def _check_open_files(reported: Any) -> CheckResult:
    """The running sidecar's open-file limit, as its health reports it."""
    from protagine.resources import OPEN_FILES
    text = str(reported or "").strip()
    if not text:
        return CheckResult("open-files", SKIP, detail="the sidecar did not report its open file limit")
    if text == "unlimited":
        return CheckResult("open-files", PASS, detail="open file limit unlimited")
    try:
        limit = int(text)
    except ValueError:
        return CheckResult("open-files", SKIP, detail=f"unreadable open file limit {text!r}")
    if limit < OPEN_FILES:
        return CheckResult("open-files", WARN,
                           detail=f"the sidecar runs with {limit} open files; the vector store wants {OPEN_FILES} "
                                  "under load",
                           remedy="raise the hard limit of the session the sidecar starts from (the generated "
                                  "service unit asks for it; 'protagine service install' then 'protagine "
                                  "service restart' apply it)")
    return CheckResult("open-files", PASS, detail=f"open file limit {limit}")


def check_semantic_recall(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """When ``router.embed_url`` is set, the running sidecar's embedder answers.

    A configured embedder that failed to initialise leaves the sidecar serving
    with keyword recall only; this check makes that a failure with the reason.
    """
    from protagine.config import load_config
    config = load_config()
    if not config.get("router.embed_url"):
        return CheckResult("semantic-recall", SKIP, detail="off: no router.embed_url in protagine.yaml")
    base_url = base_url.rstrip("/")
    try:
        status, body = _http_get(f"{base_url}/v1/host/embed/health", api_key, timeout)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("semantic-recall", FAIL, detail=f"embed/health not reachable: {exc}")
    if status != 200 or not isinstance(body, dict):
        return CheckResult("semantic-recall", FAIL, detail=f"/v1/host/embed/health returned HTTP {status}")
    if body.get("status") != "ok":
        return CheckResult(
            "semantic-recall", FAIL,
            detail=f"router.embed_url is set but the embedder is not serving: {body.get('error') or 'unknown'}",
            remedy="check router.embed_url, router.embed_model and router.embed_dims in protagine.yaml against "
                   "the endpoint (the sidecar log holds the first failure), then 'protagine service restart'")
    return CheckResult("semantic-recall", PASS,
                       detail=f"embedder serving (model={body.get('model') or 'unknown'}, dims={body.get('dims')}, "
                              f"{body.get('latency_ms', 0)} ms)")


# ---------------------------------------------------------------------------
# Engine entry point + reporting
# ---------------------------------------------------------------------------

def run_doctor(
    protagine_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = 10.0,
) -> List[CheckResult]:
    """Run every install check; never raises."""
    url = protagine_url or default_protagine_url()
    key = api_key if api_key is not None else default_api_key()
    results = run_local_checks()
    sidecar = _run("sidecar", check_sidecar, url, key, timeout)
    results += sidecar
    if sidecar and sidecar[0].status != FAIL:
        results += _run("semantic-recall", check_semantic_recall, url, key, timeout)
    return results


def default_api_key() -> str:
    from protagine.config import read_api_key
    return read_api_key() or ""


def default_protagine_url() -> str:
    """Resolve the sidecar URL the same way the other CLI commands do."""
    explicit = os.environ.get("PROTAGINE_URL") or os.environ.get("PROTAGINE_SIDECAR_URL")
    if explicit:
        return explicit
    from protagine.config import load_config
    return load_config().sidecar_url


def summarize(results: List[CheckResult]) -> dict:
    counts = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def exit_code(results: List[CheckResult]) -> int:
    """0 when nothing failed (warns are OK), 1 otherwise."""
    return 1 if any(r.status == FAIL for r in results) else 0


def skip_dominated(results: List[CheckResult]) -> bool:
    """True when more checks skipped than passed; the run verified almost nothing."""
    counts = summarize(results)
    return counts[SKIP] > counts[PASS]


def results_to_json(results: List[CheckResult]) -> dict:
    counts = summarize(results)
    return {
        "results": [r.to_dict() for r in results],
        "summary": counts,
        "ok": exit_code(results) == 0 and not skip_dominated(results),
    }


_ICONS = {PASS: "ok  ", WARN: "warn", FAIL: "FAIL", SKIP: "skip"}
_COLORS = {PASS: "\033[92m", WARN: "\033[93m", FAIL: "\033[91m", SKIP: "\033[90m"}
_RESET = "\033[0m"


def format_report(results: List[CheckResult], protagine_url: str = "", color: bool = True) -> str:
    """Human-readable report: aligned status lines, remedies indented."""
    lines: List[str] = []
    header = "Protagine doctor"
    if protagine_url:
        header += f" ({protagine_url})"
    lines.append(header)
    lines.append("")

    width = max((len(r.name) for r in results), default=0)
    for r in results:
        label = _ICONS.get(r.status, r.status)
        if color:
            label = f"{_COLORS.get(r.status, '')}{label}{_RESET}"
        line = f"  {label} {r.name.ljust(width)}"
        if r.detail:
            line += f"  {r.detail}"
        lines.append(line)
        if r.remedy and r.status in (WARN, FAIL):
            lines.append(f"       -> {r.remedy}")

    counts = summarize(results)
    lines.append("")
    lines.append(
        f"  {counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[SKIP]} skip"
    )
    if counts[FAIL]:
        verdict = f"  {counts[FAIL]} check(s) failing; fix the remedies above"
    elif skip_dominated(results):
        verdict = f"  inconclusive: {counts[SKIP]} check(s) skipped, only {counts[PASS]} verified"
    elif counts[WARN]:
        verdict = f"  healthy with {counts[WARN]} warning(s)"
    else:
        verdict = "  all checks healthy"
    lines.append(verdict)
    return "\n".join(lines)
