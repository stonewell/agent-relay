"""Worker environment builder and log redaction (design section 9.4).

The child process environment is never the parent environment. It is assembled from
explicit allowlists: a per-platform base set of variables a CLI and its toolchain need,
common toolchain roots, network/trust (proxy and CA) variables, and each CLI's config-home
override — all copied only when present in the parent environment. ``env:`` additions from
``agents.yaml`` and per-invocation extras are layered on top. Anything else in the parent
environment, including the supervisor's own secrets, is never forwarded.

Redaction helpers mask supplied secret values and credentials embedded in URLs
(userinfo in ``http(s)://user:pass@host``).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from typing import Protocol

# --- allowlists (design section 9.4) -----------------------------------------------------------

WINDOWS_BASE_VARS: frozenset[str] = frozenset(
    {
        "SystemRoot",
        "SystemDrive",
        "WinDir",
        "ComSpec",
        "PATH",
        "PATHEXT",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "ProgramData",
        "ProgramFiles",
        "ProgramFiles(x86)",
        "USERPROFILE",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "SSH_AUTH_SOCK",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
    }
)

POSIX_BASE_VARS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "SSH_AUTH_SOCK",
        "GIT_SSH_COMMAND",
    }
)

# Common toolchain roots, passed on every platform when present (design section 9.4).
TOOLCHAIN_VARS: frozenset[str] = frozenset(
    {"JAVA_HOME", "GOROOT", "GOPATH", "CARGO_HOME", "RUSTUP_HOME", "DOTNET_ROOT"}
)

# Proxy variables are matched case-insensitively ("either case" in section 9.4); the
# value is copied under the parent's original key.
PROXY_VARS: frozenset[str] = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})

# TLS/trust variables, matched exactly as named.
CA_VARS: frozenset[str] = frozenset(
    {"SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}
)

# Each CLI's config-home override, passed only when set in the parent environment.
CONFIG_HOME_VARS: frozenset[str] = frozenset({"CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME"})

# The supervisor owns the ``RELAY_`` namespace: nothing from it (IPC token, endpoint
# address, run secrets) is ever forwarded to a worker, whatever the source (section 9.4).
SUPERVISOR_PREFIX = "RELAY_"

# The supervisor's own secrets are excluded by the allowlist already; this denylist is the
# belt-and-suspenders guarantee that also covers ``env:`` additions and ``extra_env``.
SUPERVISOR_SECRET_VARS: frozenset[str] = frozenset(
    {
        "RELAY_IPC_TOKEN",
        "RELAY_IPC_ENDPOINT",
        "RELAY_OBSERVER_TOKEN",
        "RELAY_RUN_SECRET",
        "RELAY_SECRET",
        "RELAY_TOKEN",
    }
)


def is_supervisor_var(name: str) -> bool:
    """True when `name` is a supervisor-owned variable that must never reach a worker."""
    upper = name.upper()
    return upper.startswith(SUPERVISOR_PREFIX) or upper in SUPERVISOR_SECRET_VARS


class CompleteEnv(dict[str, str]):
    """A ``str -> str`` mapping that is the *complete* child environment, not additions.

    :func:`build_env` returns this type. ``run_invocation`` treats a ``CompleteEnv`` as the
    whole environment even when ``Invocation.full_env`` is left ``False``, so an adapter that
    forgets the flag still fails closed: the parent environment is not merged in. A plain
    ``dict`` keeps the legacy merge-with-parent behaviour. Pass the result through unchanged
    (do not wrap it in ``dict(...)``), or the marker is lost.
    """


class AgentEnv(Protocol):
    """Anything carrying the ``env:`` additions of an agent (e.g. ``registry.Agent``)."""

    @property
    def env(self) -> Mapping[str, str]: ...


def _is_windows(platform: str) -> bool:
    return platform == "win32"


def build_env(
    agent_config: AgentEnv | None = None,
    extra_env: Mapping[str, str] | None = None,
    parent_env: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> CompleteEnv:
    """Build the environment for one agent invocation (design section 9.4).

    The result contains only allowlisted variables copied from `parent_env` (never the
    parent environment wholesale), layered in this order, later sources winning:

    1. the platform base set (Windows or POSIX),
    2. toolchain roots, proxy/CA variables and config-home overrides present in `parent_env`,
    3. ``env:`` additions from `agent_config` (``agents.yaml``),
    4. `extra_env` (per-invocation values, e.g. provider keys).

    Supervisor-owned variables (the ``RELAY_`` namespace) are stripped from every source.
    `parent_env` defaults to ``os.environ``, `platform` to ``sys.platform``.

    The returned :class:`CompleteEnv` is a ``dict`` subclass that marks itself as the whole
    environment, so a caller that passes it to ``Invocation.env`` without setting
    ``full_env`` still gets fail-closed behaviour (no parent merge).
    """
    platform = sys.platform if platform is None else platform
    parent = dict(os.environ if parent_env is None else parent_env)
    windows = _is_windows(platform)
    lookup = {name.upper(): value for name, value in parent.items()} if windows else parent

    base: dict[str, str] = {}
    base_vars = WINDOWS_BASE_VARS if windows else POSIX_BASE_VARS
    named_vars = base_vars | TOOLCHAIN_VARS | CA_VARS | CONFIG_HOME_VARS
    for name in named_vars:
        key = name.upper() if windows else name
        if key in lookup:
            base[name] = lookup[key]
    for name in parent:
        if name.upper() in PROXY_VARS:
            base[name] = parent[name]

    layers = [base]
    if agent_config is not None:
        layers.append(dict(agent_config.env))
    if extra_env:
        layers.append(dict(extra_env))
    if windows:
        canonical = {name.upper(): name for name in named_vars}
        folded: dict[str, tuple[str, str]] = {}
        for layer in layers:
            for name, value in layer.items():
                folded[name.upper()] = (canonical.get(name.upper(), name), value)
        base = dict(folded.values())
    else:
        for layer in layers[1:]:
            base.update(layer)

    return CompleteEnv({name: value for name, value in base.items() if not is_supervisor_var(name)})


# --- redaction ---------------------------------------------------------------------------------

REDACTED = "<redacted>"

# `scheme://` followed by userinfo up to `@`: user:pass, user, or :pass are all masked.
_URL_USERINFO = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@")

# Values this short are never value-substituted: replacing "1" or "on" would corrupt logs.
_MIN_SECRET_LEN = 4


def redact_url(value: str) -> str:
    """Mask userinfo credentials in URLs inside `value` (``http://user:pass@host``)."""
    return _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", value)


def redact(text: str, *, secrets: Mapping[str, str] | None = None) -> str:
    """Redact credentials from `text` for logging.

    URL userinfo is always masked. Each value in `secrets` that is long enough to be
    meaningful is replaced wherever it appears. Returns a new string.
    """
    result = redact_url(text)
    for value in (secrets or {}).values():
        if len(value) >= _MIN_SECRET_LEN:
            result = result.replace(value, REDACTED)
    return result
