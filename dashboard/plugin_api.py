"""AI Usage Tracker backend — mounted at ``/api/plugins/ai-usage-tracker/``.

Read-only live subscription-quota aggregation, per Hermes profile:

* ``GET /usage?profile=<name>&refresh=0`` — that profile's provider quota windows.
  Omitted ``profile`` = the profile this server runs in.
* ``GET /profiles`` — the profiles this backend can reach (local machine).
* ``GET /health`` — mount probe.

Every probe runs with the *target profile's* ``HERMES_HOME`` bound through
``hermes_constants``' context-local override, which is per-thread — so one
request can resolve another profile's credentials without touching the server's
own. Secrets are resolved in-process and never serialized: the wire carries
``configured``/``source`` labels only.
"""

from __future__ import annotations

import contextvars
import inspect
import logging
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, NamedTuple, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

log = logging.getLogger(__name__)


def _reset_plugin_enabled() -> bool:
    """Read the server process's plugin allow/deny lists without host config loaders.

    Hermes' dashboard mounts user plugins from config.yaml's plugins.enabled;
    plugins.disabled vetoes them and defaults to an empty list. Its config
    loaders can create homes and good/corrupt backups even for a 'readonly'
    call. An automatic spending gate must not cause those writes or inherit a
    cached last-known-good allow-list after a malformed edit.
    """
    try:
        import yaml
        from hermes_constants import get_process_hermes_home

        home = Path(get_process_hermes_home())  # never a request's selected profile
        if not home.is_absolute():
            return False
        # Bound parsing cost; an oversized/partial file cannot authorise spending.
        with (home / 'config.yaml').open('rb') as config_file:
            raw = config_file.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return False

        class UniqueKeysLoader(yaml.SafeLoader):
            """Reject ambiguous YAML keys, including nested plugin policy keys."""

        def unique_mapping(loader, node):
            loader.flatten_mapping(node)
            result = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=True)
                if not isinstance(key, str) or key in result:
                    raise ValueError('ambiguous configuration mapping')
                result[key] = loader.construct_object(value_node, deep=True)
            return result

        UniqueKeysLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
        config = yaml.load(raw.decode('utf-8'), Loader=UniqueKeysLoader)
        if not isinstance(config, dict):
            return False
        plugins = config.get('plugins')
        if not isinstance(plugins, dict):
            return False
        enabled, disabled = plugins.get('enabled'), plugins.get('disabled', [])
        if not isinstance(enabled, list) or not isinstance(disabled, list):
            return False
        if not all(isinstance(name, str) for name in (*enabled, *disabled)):
            return False
        return 'ai-usage-tracker' in enabled and 'ai-usage-tracker' not in disabled
    except Exception:
        log.debug('Codex reset monitor could not verify plugin activation')
        return False


@asynccontextmanager
async def _reset_lifespan(_app):
    # FastAPI merges child-router lifespans into the app lifespan. A background
    # observer survives closing the page; it reads only opted-in profiles.
    codex_resets = None
    try:
        from _hermes_ai_usage_ledger_v2 import codex_resets
        codex_resets.start_monitor(_profile_rows, _run_reset_home, _reset_plugin_enabled)
    except Exception:
        # Missing optional ledger code must not take the existing quota UI down.
        log.warning("Codex reset observer unavailable")
    try:
        yield
    finally:
        if codex_resets is not None:
            codex_resets.stop_monitor()


router = APIRouter(lifespan=_reset_lifespan)

_CACHE_TTL_SECONDS = 60.0
_PROBE_TIMEOUT = 12.0
_MAX_WORKERS = 6
# How far back a provider's activity still counts as "active" (drives inclusion).
_ACTIVITY_WINDOW_DAYS = 30

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {}

_FALLBACK_LABELS = {
    "nous": "Nous Portal",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai-codex": "ChatGPT or Codex Subscription",
    "copilot": "GitHub Copilot",
    "gemini": "Google AI Studio",
    "zai": "Z.AI / GLM",
    "minimax": "MiniMax",
    "kimi-coding": "Kimi / Kimi Coding Plan",
    "deepseek": "DeepSeek",
    "xai": "xAI",
    "opencode-zen": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "lmstudio": "LM Studio",
    "custom": "Custom endpoint",
}

# Providers whose subscription quota can be read live.
_QUOTA_CAPABLE = frozenset({
    "anthropic", "openai-codex", "openrouter", "nous", "copilot",
    "opencode-go", "opencode-zen", "zai", "kimi-coding", "minimax", "deepseek",
})

_ENV_HINTS = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "copilot": "COPILOT_GITHUB_TOKEN",
    "gemini": "GEMINI_API_KEY",
    "vertex": "GOOGLE_APPLICATION_CREDENTIALS",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "zai": "GLM_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY",
    "kilocode": "KILOCODE_API_KEY",
    "huggingface": "HF_TOKEN",
    "alibaba": "DASHSCOPE_API_KEY",
    "xiaomi": "XIAOMI_API_KEY",
    "nous": "NOUS_API_KEY",
}


# --------------------------------------------------------------------------
# Profile scoping
# --------------------------------------------------------------------------


class _ScopeUnavailable(RuntimeError):
    """The host cannot bind the selected profile's secret scope; credentials were not read."""


class _SecretBinding(NamedTuple):
    """One prepared, immutable secret-scope snapshot for a selected profile.

    Prepared once (hydration + scope build happen then); ``bind`` installs a
    private copy in the calling worker so one worker's host-side mirror write
    cannot reach a sibling. Never stored beyond the operation that prepared it.
    (NamedTuple, not dataclass: the host loads this file without registering it
    in ``sys.modules``.)
    """

    secrets: Mapping[str, str]
    stamp: Optional[str]
    set_scope: Callable[..., Any]
    reset: Callable[[Any], None]

    def __repr__(self) -> str:  # never render secret values into logs/tracebacks
        return f'_SecretBinding(stamp={self.stamp!r}, keys={len(self.secrets)})'

    def bind(self) -> Any:
        if self.stamp and 'profile_home' in inspect.signature(self.set_scope).parameters:
            return self.set_scope(dict(self.secrets), profile_home=self.stamp)
        return self.set_scope(dict(self.secrets))


class _PreparedHome(NamedTuple):
    """A selected home plus its secret binding prepared once for one payload build.

    Passed explicitly as ``home`` to every worker of that build (never through
    inherited thread context); ``_bound_call`` unwraps it, binds the real home
    path and the prepared scope, and never hydrates again. ``binding`` is
    ``None`` only where the host has no scope API (home-only binding).
    """

    path: Path
    binding: Optional[_SecretBinding]


def _home_path(home: Any) -> Any:
    return home.path if isinstance(home, _PreparedHome) else home


def _home_and_scope(home: Any) -> tuple[Path, Optional[_SecretBinding]]:
    """A prepared home's path and binding; a plain home is prepared now (reset/monitor ops)."""
    if isinstance(home, _PreparedHome):
        return home.path, home.binding
    return home, _selected_secret_scope(home)


def _prepare_home(home: Optional[Path]) -> Optional[Any]:
    """Prepare ``home``'s secret scope once for a whole ``/usage`` build.

    Mirrors the host route, which enters one scope per request: at most one
    hydration of the selected profile's external secret sources per build, so
    a failing helper is not re-run by every probe worker. Raises
    ``_ScopeUnavailable`` exactly where per-worker binding would have.
    """
    if home is None:
        return None
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override  # noqa: F401
    except Exception:  # noqa: BLE001 - same legacy fallback as _run_in_home: nothing is bound
        return home
    return _PreparedHome(Path(home), _selected_secret_scope(home))


def _selected_secret_scope(home: Path) -> Optional[_SecretBinding]:
    """Prepared binding for ``home``'s host secret scope; ``None`` on a pre-scope host.

    Mirrors ``hermes_cli.web_server_profiles._config_profile_scope``: the
    process's own profile gets the host launch scope (its ``.env`` over the
    launch env, frozen once multi-profile hosting is active); any other profile
    gets a scope built only from that profile's own files, stamped with its home.
    ``get_secret`` then never falls back to another profile's ``os.environ``.
    A host that ships the guard module but not this binding API fails closed.
    """
    try:
        import agent.secret_scope as scope
    except ModuleNotFoundError as exc:
        if exc.name not in ('agent', 'agent.secret_scope'):
            raise _ScopeUnavailable('Selected profile credential scope unavailable.') from exc
        return None  # pre-scope host: no guard, credentials follow the home override
    try:
        set_scope, reset_scope = scope.set_secret_scope, scope.reset_secret_scope
        from hermes_constants import get_process_hermes_home
        try:
            from hermes_constants import get_routing_process_hermes_home as own_home
        except ImportError:
            own_home = get_process_hermes_home
        if Path(home).resolve() == Path(own_home()).resolve():
            try:
                from tui_gateway.launch_profile_policy import launch_secret_scope
            except ImportError:
                # Older host without launch policy: unscoped reads are the launch
                # env unless multiplexing is on, when they must not proceed.
                if getattr(scope, 'is_multiplex_active', lambda: True)():
                    raise
                return None
            secrets, stamp = launch_secret_scope(Path(home)), None  # own home: no foreign stamp
        else:
            build = scope.build_profile_secret_scope
            try:
                from hermes_cli.env_loader import hydrate_profile_secret_sources
            except ImportError:
                hydrate_profile_secret_sources = None
            if hydrate_profile_secret_sources is not None:
                hydrate_profile_secret_sources(Path(home))  # host-owned, never touches os.environ
            secrets, stamp = build(Path(home)), str(home)
    except (ImportError, AttributeError) as exc:
        raise _ScopeUnavailable('Selected profile credential scope unavailable.') from exc
    return _SecretBinding(MappingProxyType(dict(secrets)), stamp, set_scope, reset_scope)


def _bound_call(home: Any, fn: Callable[[], Any], set_home: Callable[[Any], Any],
                reset_home: Callable[[Any], None], check: Optional[Callable[[], None]] = None) -> Any:
    """Bind home override then secret scope in this worker; reset both in reverse.

    A ``_PreparedHome`` reuses its build-wide binding; a plain home is prepared
    here, once per separate operation (Codex reset routes and monitor).
    """
    home, secret = _home_and_scope(home)
    home_token = set_home(home)
    secret_token = None
    try:
        if secret is not None:
            secret_token = secret.bind()
        if check is not None:
            check()
        return fn()
    finally:
        if secret is not None and secret_token is not None:
            secret.reset(secret_token)
        reset_home(home_token)


def _run_in_home(home: Optional[Path], fn: Callable[[], Any]) -> Any:
    """Run ``fn`` with ``home``'s Hermes home and secret scope bound to this worker.

    Both are ContextVars, so a request can resolve another profile's
    credentials/state without disturbing the server's own profile; executor
    workers do NOT inherit the caller's context, so every worker binds them.
    ``home`` may be a ``_PreparedHome`` (bind its build-wide scope, no
    re-hydration) or a plain path (prepare now, for a standalone operation).
    Home alone is not enough on a multi-profile host: ``get_secret`` refuses
    unscoped reads and the host quota fetcher swallows that as ``None``.
    """
    if home is None:
        return fn()
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except Exception:  # noqa: BLE001
        return fn()
    return _bound_call(home, fn, set_hermes_home_override, reset_hermes_home_override)


def _run_in_home_only(home: Optional[Path], fn: Callable[[], Any]) -> Any:
    """Bind only ``home``'s Hermes home override — no secret scope, no hydration.

    For source-file reads that never resolve credentials (activity discovery from
    the selected profile's ``state.db``). Same fallback as the historical
    ``_run_in_home``; the selected home is reset in ``finally``.
    """
    if home is None:
        return fn()
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except Exception:  # noqa: BLE001
        return fn()
    token = set_hermes_home_override(_home_path(home))
    try:
        return fn()
    finally:
        reset_hermes_home_override(token)


def _run_reset_home(home: Path, fn: Callable[[], Any]) -> Any:
    """Fail closed on reset operations; legacy quota probes keep their fallback."""
    try:
        from hermes_constants import (get_hermes_home, reset_hermes_home_override,
                                      set_hermes_home_override)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError('Selected profile context unavailable.') from exc

    def check() -> None:
        if Path(get_hermes_home()).resolve() != Path(_home_path(home)).resolve():
            raise RuntimeError('Selected profile context unavailable.')
    return _bound_call(home, fn, set_hermes_home_override, reset_hermes_home_override, check)


def _server_home() -> Path:
    try:
        from hermes_constants import get_process_hermes_home

        return Path(get_process_hermes_home())
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _profile_rows() -> list[dict[str, Any]]:
    """Every local profile this backend can probe, default first."""
    server_home = _server_home()
    try:
        server_home = server_home.resolve()
    except OSError:
        pass
    rows: list[dict[str, Any]] = []
    try:
        from hermes_cli.profiles import list_profiles

        for info in list_profiles():
            try:
                path = Path(info.path).resolve()
            except OSError:
                continue
            rows.append({
                "name": str(info.name),
                "path": str(path),
                "is_default": bool(getattr(info, "is_default", False)),
                "is_server": path == server_home,
                "gateway_running": bool(getattr(info, "gateway_running", False)),
            })
    except Exception:  # noqa: BLE001 - never fail the page because profile listing broke
        log.debug("profile enumeration failed", exc_info=True)
    if not rows:
        rows = [{"name": "default", "path": str(server_home), "is_default": True,
                 "is_server": True, "gateway_running": False}]
    rows.sort(key=lambda row: (not row["is_default"], row["name"].lower()))
    return rows


def _resolve_profile(name: Optional[str]) -> tuple[Optional[Path], Optional[str], Optional[str]]:
    """``(home, resolved_name, error)`` — ``home=None`` means the server's own profile."""
    wanted = str(name or "").strip()
    rows = _profile_rows()
    if not wanted:
        for row in rows:
            if row["is_server"]:
                return Path(row["path"]), row["name"], None
        return None, None, None
    for row in rows:
        if row["name"] == wanted:
            return Path(row["path"]), row["name"], None
    known = ", ".join(row["name"] for row in rows)
    return None, None, f"Unknown profile {wanted!r} on this machine (known: {known})"


# --------------------------------------------------------------------------
# Provider catalog
# --------------------------------------------------------------------------


def _provider_labels() -> dict[str, str]:
    try:
        from hermes_cli.models_catalog_static import _PROVIDER_LABELS  # type: ignore

        labels = {str(k): str(v) for k, v in dict(_PROVIDER_LABELS).items()}
        return labels or dict(_FALLBACK_LABELS)
    except Exception:  # noqa: BLE001
        log.debug("provider catalog unavailable, using fallback labels", exc_info=True)
        return dict(_FALLBACK_LABELS)


def _normalize_provider(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return "unattributed"
    if value.startswith("custom:"):
        return "custom"
    aliases = {"openai": "openai-api", "gemini-code-assist": "gemini", "glm": "zai", "moonshot": "kimi-coding"}
    return aliases.get(value, value)


def _active_providers(days: int = _ACTIVITY_WINDOW_DAYS) -> tuple[set[str], dict[str, float]]:
    """Providers with recorded usage in the window — ``{id: last_seen}`` (no token math)."""
    db_path = Path(str(_hermes_home())) / "state.db"
    if not db_path.is_file():
        return set(), {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)) as conn:
            rows = conn.execute(
                "SELECT billing_provider, MAX(last_seen) FROM session_model_usage "
                "WHERE last_seen >= ? GROUP BY billing_provider",
                (cutoff,),
            ).fetchall()
    except Exception:  # noqa: BLE001 - a locked/absent DB must not 500 the pane
        log.debug("activity read failed", exc_info=True)
        return set(), {}
    last_seen: dict[str, float] = {}
    for billing_provider, seen in rows:
        pid = _normalize_provider(billing_provider)
        if pid == "unattributed":
            continue
        last_seen[pid] = max(last_seen.get(pid, 0.0), float(seen or 0.0))
    return set(last_seen), last_seen


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


# --------------------------------------------------------------------------
# Live quota probes
# --------------------------------------------------------------------------


def _window(label: str, used: Optional[float], reset_at: Any = None, detail: Optional[str] = None) -> dict[str, Any]:
    used_value = None if used is None else max(0.0, min(100.0, float(used)))
    return {
        "label": label,
        "used_percent": used_value,
        "remaining_percent": None if used_value is None else max(0.0, min(100.0, 100.0 - used_value)),
        "reset_at": reset_at if isinstance(reset_at, str) else _iso(reset_at),
        "detail": detail,
    }


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return None


def _unavailable(reason: str, *, source: str = "") -> dict[str, Any]:
    return {"available": False, "source": source, "title": "Account limits", "plan": None,
            "windows": [], "details": [], "unavailable_reason": reason, "fetched_at": None}


class _ProbeAuthError(Exception):
    """Credential missing/rejected — reported as an auth reason, never a stack trace."""


class _ProbeUnsupported(Exception):
    """Endpoint exists in the wild but not for this account — report plainly."""


def _runtime_key(provider: str) -> Optional[str]:
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=provider) or {}
        key = str(runtime.get("api_key") or "").strip()
        return key or None
    except Exception:  # noqa: BLE001 - missing creds are a normal state, not an error
        log.debug("runtime key resolution failed for %s", provider, exc_info=True)
        return None


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    with httpx.Client(timeout=_PROBE_TIMEOUT) as client:
        response = client.get(url, headers=headers)
        if response.status_code in (401, 403):
            raise _ProbeAuthError(f"HTTP {response.status_code} — credential rejected or expired")
        response.raise_for_status()
        body = response.text
        if body.strip() == "Not Found":
            raise _ProbeUnsupported("endpoint not offered for this account tier")
        return response.json() or {}


def _serialize_snapshot(snapshot: Any) -> dict[str, Any]:
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        used = getattr(window, "used_percent", None)
        windows.append({
            "label": str(getattr(window, "label", "") or "Window"),
            "used_percent": None if used is None else max(0.0, min(100.0, float(used))),
            "remaining_percent": None if used is None else max(0.0, min(100.0, 100.0 - float(used))),
            "reset_at": _iso(getattr(window, "reset_at", None)),
            "detail": getattr(window, "detail", None),
        })
    return {
        "available": bool(getattr(snapshot, "available", False)),
        "source": str(getattr(snapshot, "source", "") or ""),
        "title": str(getattr(snapshot, "title", "") or "Account limits"),
        "plan": getattr(snapshot, "plan", None),
        "windows": windows,
        "details": [str(d) for d in (getattr(snapshot, "details", ()) or ())],
        "unavailable_reason": getattr(snapshot, "unavailable_reason", None),
        "fetched_at": _iso(getattr(snapshot, "fetched_at", None)),
    }


def _probe_standard(provider: str) -> dict[str, Any]:
    from agent.account_usage import fetch_account_usage

    snapshot = fetch_account_usage(provider)
    if snapshot is None:
        # Host account_usage also returns None when its quota request fails;
        # it does not establish whether this profile has usable credentials.
        return _unavailable("Subscription limits unavailable — quota could not be read for this profile.",
                            source="usage_api")
    return _serialize_snapshot(snapshot)


def _probe_nous() -> dict[str, Any]:
    from agent.account_usage import build_nous_credits_snapshot

    def _fetch():
        from hermes_cli.nous_account import get_nous_portal_account_info

        return get_nous_portal_account_info(force_fresh=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        # Carry the caller's bound profile home and secret scope into this worker.
        info = pool.submit(contextvars.copy_context().run, _fetch).result(timeout=_PROBE_TIMEOUT)
    snapshot = build_nous_credits_snapshot(info)
    if snapshot is None:
        return _unavailable("Not signed in to Nous Portal, or the portal returned no subscription data.",
                            source="portal-account")
    return _serialize_snapshot(snapshot)


def _probe_copilot() -> dict[str, Any]:
    from hermes_cli.copilot_auth import resolve_copilot_token

    try:
        token, _ = resolve_copilot_token()
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"No GitHub Copilot token available ({type(exc).__name__}).")
    if not token:
        return _unavailable("No GitHub Copilot token available. Run `hermes auth` to sign in.")
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/json",
        "Editor-Version": "vscode/1.96.2",
        "Editor-Plugin-Version": "copilot-chat/0.23.0",
        "User-Agent": "GitHubCopilotChat/0.23.0",
    }
    try:
        with httpx.Client(timeout=_PROBE_TIMEOUT) as client:
            response = client.get("https://api.github.com/copilot_internal/user", headers=headers)
            if response.status_code in (401, 403):
                return _unavailable("GitHub rejected the Copilot token (HTTP %d) — re-run `hermes auth`."
                                    % response.status_code, source="copilot_internal")
            if response.status_code >= 400:
                return _unavailable(f"Copilot quota endpoint returned HTTP {response.status_code}.",
                                    source="copilot_internal")
            payload = response.json() or {}
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"Copilot quota probe failed: {type(exc).__name__}: {exc}", source="copilot_internal")

    snapshots = payload.get("quota_snapshots") or {}
    reset_at = payload.get("quota_reset_date") or payload.get("limited_user_reset_date")
    windows: list[dict[str, Any]] = []
    details: list[str] = []
    for key, label in (("premium_interactions", "Premium requests"), ("chat", "Chat"), ("completions", "Completions")):
        entry = snapshots.get(key) or {}
        percent_remaining = entry.get("percent_remaining")
        entitlement, remaining = entry.get("entitlement"), entry.get("remaining")
        if entry.get("unlimited"):
            windows.append({"label": label, "used_percent": None, "remaining_percent": 100.0,
                            "reset_at": reset_at, "detail": "unlimited"})
            continue
        if isinstance(entitlement, (int, float)) and entitlement <= 0:
            details.append(f"{label}: not included in this plan")
            continue
        if percent_remaining is None:
            continue
        detail = (f"{remaining:g} of {entitlement:g} left"
                  if isinstance(entitlement, (int, float)) and isinstance(remaining, (int, float)) else None)
        windows.append(_window(label, 100.0 - float(percent_remaining), reset_at, detail))
    plan = payload.get("copilot_plan") or payload.get("access_type_sku")
    if isinstance(plan, str) and plan:
        details.append(f"Plan: {plan}")
    overage = (snapshots.get("premium_interactions") or {}).get("overage_count")
    if isinstance(overage, (int, float)) and overage > 0:
        details.append(f"Overage requests used: {overage:g}")
    if not windows and not details:
        return _unavailable("Copilot returned no quota windows for this account.", source="copilot_internal")
    return {"available": True, "source": "copilot_internal", "title": "GitHub Copilot limits",
            "plan": plan if isinstance(plan, str) else None, "windows": windows, "details": details,
            "unavailable_reason": None, "fetched_at": datetime.now(timezone.utc).isoformat()}


def _probe_opencode_go() -> dict[str, Any]:
    key = _runtime_key("opencode-go")
    if not key:
        return _unavailable("No OpenCode Go API key (OPENCODE_GO_API_KEY) in this profile.", source="zen_go_usage")
    try:
        payload = _get_json("https://opencode.ai/zen/go/v1/usage",
                            {"Authorization": f"Bearer {key}", "Accept": "application/json"})
    except _ProbeAuthError as exc:  # noqa: BLE001
        return _unavailable(f"OpenCode Go rejected the API key ({exc}).", source="zen_go_usage")
    except _ProbeUnsupported as exc:  # noqa: BLE001
        return _unavailable(f"OpenCode Go quota not available for this account ({exc}).", source="zen_go_usage")
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"OpenCode Go quota probe failed: {type(exc).__name__}: {exc}", source="zen_go_usage")
    usage = payload.get("usage") or {}
    windows = []
    for field, label in (("rolling", "Rolling (5h)"), ("weekly", "Weekly"), ("monthly", "Monthly")):
        entry = usage.get(field) or {}
        percent = entry.get("percent")
        if percent is None:
            continue
        status = entry.get("status")
        windows.append(_window(label, float(percent), entry.get("resetsAt"),
                               None if status in (None, "ok") else f"status: {status}"))
    if not windows:
        return _unavailable("OpenCode Go returned no usage windows.", source="zen_go_usage")
    return {"available": True, "source": "zen_go_usage", "title": "OpenCode Go limits", "plan": "Go",
            "windows": windows, "details": [], "unavailable_reason": None,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def _probe_opencode_zen() -> dict[str, Any]:
    key = _runtime_key("opencode-zen")
    if not key:
        return _unavailable("No OpenCode Zen API key (OPENCODE_ZEN_API_KEY) in this profile.", source="zen_credits")
    for url in ("https://api.opencode.ai/v1/credits", "https://opencode.ai/zen/v1/credits"):
        try:
            payload = _get_json(url, {"Authorization": f"Bearer {key}", "Accept": "application/json"})
        except _ProbeUnsupported:
            continue
        except _ProbeAuthError as exc:  # noqa: BLE001
            return _unavailable(f"OpenCode Zen rejected the API key ({exc}).", source="zen_credits")
        except Exception:  # noqa: BLE001
            continue
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        total, used, remaining = data.get("total_credits"), data.get("used_credits"), data.get("remaining_credits")
        windows: list[dict[str, Any]] = []
        details: list[str] = []
        if isinstance(total, (int, float)) and isinstance(used, (int, float)) and total > 0:
            windows.append(_window("Credits", float(used) / float(total) * 100.0, None,
                                   f"${float(remaining or 0):,.2f} of ${float(total):,.2f} left"))
        elif isinstance(remaining, (int, float)):
            details.append(f"Credits remaining: ${float(remaining):,.2f}")
        if windows or details:
            return {"available": True, "source": "zen_credits", "title": "OpenCode Zen credits", "plan": None,
                    "windows": windows, "details": details, "unavailable_reason": None,
                    "fetched_at": datetime.now(timezone.utc).isoformat()}
    return _unavailable("OpenCode Zen exposes no reachable credits/usage endpoint for this key — "
                        "it is pay-as-you-go.", source="zen_credits")


def _probe_zai() -> dict[str, Any]:
    key = _runtime_key("zai")
    if not key:
        return _unavailable("No Z.AI / GLM API key (GLM_API_KEY) in this profile.", source="zai_quota")
    try:
        payload = _get_json("https://api.z.ai/api/monitor/usage/quota/limit",
                            {"Authorization": f"Bearer {key}", "Accept": "application/json"})
    except _ProbeAuthError as exc:  # noqa: BLE001
        return _unavailable(f"Z.AI rejected the API key ({exc}).", source="zai_quota")
    except _ProbeUnsupported as exc:  # noqa: BLE001
        return _unavailable(f"Z.AI quota not available for this account ({exc}).", source="zai_quota")
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"Z.AI quota probe failed: {type(exc).__name__}: {exc}", source="zai_quota")
    windows: list[dict[str, Any]] = []
    for item in payload.get("limits") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").upper()
        percent = item.get("percentage")
        total, current, usage_value = item.get("total"), item.get("currentValue"), item.get("usage")
        if percent is None and isinstance(current, (int, float)) and isinstance(total or usage_value, (int, float)):
            base = total or usage_value
            if base:
                percent = float(current) / float(base) * 100.0
        if kind == "TOKENS_LIMIT":
            label = "Session (5h)"
        elif kind == "TIME_LIMIT":
            label = "Tool quota"
        elif item.get("unit") == 6:
            label = "Weekly"
        else:
            label = kind.replace("_", " ").title() or "Limit"
        remaining = item.get("remaining")
        window = _window(label, percent, None, f"{remaining} left" if remaining is not None else None)
        if window["used_percent"] is not None:
            windows.append(window)
    if not windows:
        return _unavailable("Z.AI returned no parseable quota limits.", source="zai_quota")
    return {"available": True, "source": "zai_quota", "title": "Z.AI coding plan limits", "plan": None,
            "windows": windows, "details": [], "unavailable_reason": None,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def _probe_kimi() -> dict[str, Any]:
    key = _runtime_key("kimi-coding")
    if not key:
        return _unavailable("No Kimi API key (KIMI_API_KEY) in this profile.", source="kimi_usages")
    try:
        payload = _get_json("https://api.kimi.com/coding/v1/usages",
                            {"Authorization": f"Bearer {key}", "Accept": "application/json"})
    except _ProbeAuthError as exc:  # noqa: BLE001
        return _unavailable(f"Kimi rejected the API key ({exc}).", source="kimi_usages")
    except _ProbeUnsupported as exc:  # noqa: BLE001
        return _unavailable(f"Kimi coding plan quota not available for this account ({exc}).", source="kimi_usages")
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"Kimi quota probe failed: {type(exc).__name__}: {exc}", source="kimi_usages")

    windows: list[dict[str, Any]] = []
    usage = payload.get("usage") or {}

    def _num(value: Any) -> Optional[float]:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None

    used, limit = _num(usage.get("used")), _num(usage.get("limit"))
    if used is not None and limit:
        windows.append(_window("Weekly", used / limit * 100.0, usage.get("resetTime"),
                               f"{max(0.0, limit - used):g} of {limit:g} left"))
    for entry in payload.get("limits") or []:
        if isinstance(entry, dict) and entry.get("percentage") is not None:
            windows.append(_window(str(entry.get("window") or entry.get("name") or "Limit"),
                                   float(entry["percentage"]),
                                   entry.get("resetTime") or entry.get("reset_time")))
    if not windows:
        return _unavailable("Kimi returned no parseable usage windows.", source="kimi_usages")
    return {"available": True, "source": "kimi_usages", "title": "Kimi coding plan limits", "plan": None,
            "windows": windows, "details": [], "unavailable_reason": None,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def _probe_minimax() -> dict[str, Any]:
    key = _runtime_key("minimax")
    if not key:
        return _unavailable("No MiniMax API key (MINIMAX_API_KEY) in this profile.", source="minimax_remains")
    payload = None
    last_error: Optional[str] = None
    for url in ("https://api.minimax.io/v1/api/openplatform/coding_plan/remains",
                "https://www.minimax.io/v1/api/openplatform/coding_plan/remains"):
        try:
            payload = _get_json(url, {"Authorization": f"Bearer {key}", "Accept": "application/json"})
            break
        except _ProbeAuthError as exc:  # noqa: BLE001
            return _unavailable(f"MiniMax rejected the API key ({exc}).", source="minimax_remains")
        except _ProbeUnsupported as exc:  # noqa: BLE001
            return _unavailable(f"MiniMax coding plan quota not available for this account ({exc}).",
                                source="minimax_remains")
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
    if payload is None:
        return _unavailable(f"MiniMax quota probe failed: {last_error}", source="minimax_remains")

    windows: list[dict[str, Any]] = []
    for row in payload.get("model_remains") or []:
        if not isinstance(row, dict):
            continue
        model_name = str(row.get("model_name") or "Model")
        for total_key, remaining_key, label in (("current_interval_total_count", "current_interval_usage_count", "5h"),
                                                ("current_weekly_total_count", "current_weekly_usage_count", "weekly")):
            total, remaining = row.get(total_key), row.get(remaining_key)
            if isinstance(total, (int, float)) and total > 0 and isinstance(remaining, (int, float)):
                left = min(max(remaining, 0), total)
                windows.append(_window(f"{model_name} · {label}", (total - left) / total * 100.0, None,
                                       f"{left:g} of {total:g} left"))
    if not windows:
        return _unavailable("MiniMax returned no parseable coding-plan rows.", source="minimax_remains")
    return {"available": True, "source": "minimax_remains", "title": "MiniMax coding plan limits", "plan": None,
            "windows": windows, "details": [], "unavailable_reason": None,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def _probe_deepseek() -> dict[str, Any]:
    key = _runtime_key("deepseek")
    if not key:
        return _unavailable("No DeepSeek API key (DEEPSEEK_API_KEY) in this profile.", source="deepseek_balance")
    try:
        payload = _get_json("https://api.deepseek.com/user/balance",
                            {"Authorization": f"Bearer {key}", "Accept": "application/json"})
    except Exception as exc:  # noqa: BLE001
        return _unavailable(f"DeepSeek balance probe failed: {type(exc).__name__}: {exc}", source="deepseek_balance")
    details = [f"{info.get('currency') or ''} {info.get('total_balance')} balance".strip()
               for info in payload.get("balance_infos") or []
               if isinstance(info, dict) and info.get("total_balance") is not None]
    if not details:
        return _unavailable("DeepSeek returned no balance rows.", source="deepseek_balance")
    return {"available": True, "source": "deepseek_balance", "title": "DeepSeek balance", "plan": None,
            "windows": [], "details": details, "unavailable_reason": None,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


_PROBES: dict[str, Callable[[], dict[str, Any]]] = {
    "anthropic": lambda: _probe_standard("anthropic"),
    "openai-codex": lambda: _probe_standard("openai-codex"),
    "openrouter": lambda: _probe_standard("openrouter"),
    "nous": _probe_nous,
    "copilot": _probe_copilot,
    "opencode-go": _probe_opencode_go,
    "opencode-zen": _probe_opencode_zen,
    "zai": _probe_zai,
    "kimi-coding": _probe_kimi,
    "minimax": _probe_minimax,
    "deepseek": _probe_deepseek,
}


def _configured_providers() -> set[str]:
    """Providers with resolvable credentials in the bound profile — names only."""
    configured: set[str] = set()
    for provider in _provider_labels():
        try:
            from hermes_cli.auth import get_provider_auth_state

            state = get_provider_auth_state(provider)
        except Exception:  # noqa: BLE001
            state = None
        if state:
            configured.add(provider)
            continue
        try:
            from hermes_cli.auth import read_credential_pool

            pool = read_credential_pool(provider)
            entries = pool if isinstance(pool, list) else [
                e for e in ((pool or {}).get(provider) or (pool or {}).get("entries") or []) if isinstance(e, dict)
            ]
            if entries:
                configured.add(provider)
                continue
        except Exception:  # noqa: BLE001
            log.debug("credential pool read failed for %s", provider, exc_info=True)
        env_hint = _ENV_HINTS.get(provider)
        if env_hint and os.environ.get(env_hint):
            configured.add(provider)
    return configured


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


def _build_payload(home: Optional[Path], profile_name: Optional[str]) -> dict[str, Any]:
    started = time.time()
    labels = _provider_labels()
    active, last_seen = _run_in_home_only(home, _active_providers)
    configured = _run_in_home(home, _configured_providers)

    targets = sorted((active | configured) & _QUOTA_CAPABLE)
    probes: dict[str, dict[str, Any]] = {}
    if targets:
        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(targets))) as pool:
            futures = {
                pool.submit(_run_in_home, home, _PROBES[pid]): pid
                for pid in targets
            }
            for future in as_completed(futures):
                pid = futures[future]
                try:
                    probes[pid] = future.result()
                except Exception as exc:  # noqa: BLE001 - one probe must never sink the page
                    log.debug("quota probe failed for %s", pid, exc_info=True)
                    probes[pid] = _unavailable(f"Probe failed: {type(exc).__name__}: {exc}")

    names = sorted(active | configured, key=lambda pid: labels.get(pid, pid).lower())
    providers = []
    for pid in names:
        quota = probes.get(pid) or _unavailable(
            "No public subscription-quota API for this provider." if pid in labels
            else "Provider not in the Hermes catalog.",
            source="none",
        )
        providers.append({
            "id": pid,
            "label": labels.get(pid, pid.replace("-", " ").title()),
            "configured": pid in configured,
            "quota_capable": pid in _QUOTA_CAPABLE,
            "active": pid in active,
            "last_active_at": last_seen.get(pid),
            "quota": quota,
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile_name,
        "probe_seconds": round(time.time() - started, 2),
        "profiles": _profile_rows(),
        "providers": providers,
    }


_SCOPE_UNAVAILABLE_REASON = ("Subscription limits not read — this Hermes host cannot bind the selected "
                             "profile's credential scope, so no credential was accessed.")


def _scope_unavailable_payload(home: Optional[Path], profile_name: Optional[str]) -> dict[str, Any]:
    """Truthful result when the host's secret-scope binding API is partial.

    Credential discovery and every quota probe stay fail-closed (never attempted).
    Source-only activity discovery reads the selected home's ``state.db`` under a
    home-only binding, so recently used providers still render as unavailable.
    With no such activity the provider set is unknown, not empty: refuse (503).
    """
    unknown = HTTPException(503, "Selected profile credential scope unavailable on this Hermes host; "
                                 "provider discovery was not performed.")
    if home is None:
        raise unknown
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except (ImportError, AttributeError):
        raise unknown from None
    token = set_hermes_home_override(home)
    try:
        active, last_seen = _active_providers()
    finally:
        reset_hermes_home_override(token)
    if not active:
        raise unknown
    labels = _provider_labels()
    providers = [{
        "id": pid,
        "label": labels.get(pid, pid.replace("-", " ").title()),
        "configured": None,  # unknown: credential discovery was not performed
        "quota_capable": pid in _QUOTA_CAPABLE,
        "active": True,
        "last_active_at": last_seen.get(pid),
        "quota": _unavailable(_SCOPE_UNAVAILABLE_REASON, source="scope_unavailable"),
    } for pid in sorted(active, key=lambda pid: labels.get(pid, pid).lower())]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": profile_name,
        "probe_seconds": 0.0,
        "profiles": _profile_rows(),
        "providers": providers,
        "credential_discovery": {"status": "unavailable", "reason": "secret_scope_unavailable",
                                 "note": "Only providers with recent recorded activity are listed."},
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/profiles")
def get_profiles() -> dict[str, Any]:
    return {"profiles": _profile_rows()}


@router.get("/usage")
def get_usage(
    profile: Optional[str] = Query(None, description="Hermes profile name; omit for this server's own"),
    refresh: bool = Query(False),
    profile_scope: str = "selected",
) -> dict[str, Any]:
    if profile_scope not in ("selected", "all"):
        raise HTTPException(400, "Invalid profile scope.")
    if profile_scope == "all":
        return {"profile_scope": "all", "read_only": True, "providers": [],
                "quota": {"available": False, "reason": "aggregate_quota_unavailable",
                          "note": "Shared subscription quotas cannot be summed; no probes performed."}}
    home, resolved, error = _resolve_profile(profile)
    if error:
        raise HTTPException(status_code=404, detail=error)
    key = f"profile={resolved or ''}"
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and not refresh and (now - entry["at"]) < _CACHE_TTL_SECONDS:
            payload = dict(entry["payload"])
            payload["cached"] = True
            payload["cache_age_seconds"] = round(now - entry["at"], 1)
            return payload
    try:
        # One prepared scope per build, passed explicitly to every worker.
        payload = _build_payload(_prepare_home(home), resolved)
        degraded = False
    except _ScopeUnavailable:
        # Raised only by discovery binding (probe failures are caught per probe);
        # any other exception still propagates.
        payload, degraded = _scope_unavailable_payload(home, resolved), True
    try:
        if _ledger_store is not None and not degraded:  # not a quota observation
            _ledger_store(home or _server_home()).save_quota(payload)
    except Exception:
        log.warning("AI usage quota snapshot could not be stored; quota display still works.")
    payload["cached"] = False
    payload["cache_age_seconds"] = 0.0
    with _cache_lock:
        _cache[key] = {"at": time.time(), "payload": payload}
    return payload


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "plugin": "ai-usage-tracker", "cached_profiles": sorted(_cache)}


def _reset_profile(profile: Optional[str]) -> tuple[Path, str]:
    home, name, error = _resolve_profile(profile)
    if error or not home or not name:
        raise HTTPException(404, "Unknown profile.")
    return home, name


def _reset_error(exc: Exception):
    from _hermes_ai_usage_ledger_v2.codex_resets import Unavailable
    if isinstance(exc, Unavailable):
        raise HTTPException(409, str(exc)) from exc
    log.warning("Codex reset operation unavailable: %s", type(exc).__name__)
    raise HTTPException(503, "Codex reset service unavailable.") from exc


@router.get("/codex/resets")
def codex_resets_read(profile: Optional[str] = None) -> dict:
    home, name = _reset_profile(profile)
    try:
        from _hermes_ai_usage_ledger_v2 import codex_resets
        return {"profile": name, **_run_reset_home(home, lambda: codex_resets.observe(home))}
    except Exception as exc:
        _reset_error(exc)


@router.post("/codex/resets/auto")
def codex_resets_auto(body: dict) -> dict:
    home, name = _reset_profile(body.get("profile"))
    if type(body.get("enabled")) is not bool or not isinstance(body.get("binding"), str):
        raise HTTPException(400, "Invalid opt-in request.")
    try:
        from _hermes_ai_usage_ledger_v2 import codex_resets
        return {"profile": name, **_run_reset_home(home, lambda: codex_resets.set_auto(
            home, body["binding"], body["enabled"]))}
    except Exception as exc:
        _reset_error(exc)


@router.post("/codex/resets/redeem")
def codex_resets_redeem(body: dict) -> dict:
    home, name = _reset_profile(body.get("profile"))
    if (not isinstance(body.get("binding"), str) or not isinstance(body.get("episode"), str)
            or type(body.get("count")) is not int or body["count"] <= 0):
        raise HTTPException(400, "Invalid redemption request.")
    try:
        from _hermes_ai_usage_ledger_v2 import codex_resets
        result = _run_reset_home(home, lambda: codex_resets.redeem(home,
            expected_binding=body["binding"], expected_episode=body["episode"],
            expected_count=body["count"]))
        return {"profile": name, **result}
    except Exception as exc:
        _reset_error(exc)


# Personal ledger extension. An unavailable recorder must not remove quota routes.
_ledger_store = None
_quota_routes = [route for route in router.routes if route.path in ('/usage', '/profiles', '/health')]
try:
    import importlib.util as _ledger_importlib
    _spec = _ledger_importlib.spec_from_file_location(
        "_ai_usage_backend_bootstrap", Path(__file__).resolve().parent.parent / "bootstrap.py")
    _boot = _ledger_importlib.module_from_spec(_spec)
    _spec.loader.exec_module(_boot)
    from _hermes_ai_usage_ledger_v2.storage import Store as _ledger_store
    from _hermes_ai_usage_ledger_v2.api import add_routes as _ledger_add_routes
    _ledger_add_routes(router, _resolve_profile, _server_home)
except Exception:
    _ledger_store = None
    router.routes[:] = _quota_routes
    log.warning("AI usage ledger extension unavailable; original quota routes remain active.")
