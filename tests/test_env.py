"""Environment builder and redaction (task 1.8, design section 9.4)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from relay.adapters.base import Invocation, run_invocation
from relay.core.env import (
    CA_VARS,
    CONFIG_HOME_VARS,
    POSIX_BASE_VARS,
    PROXY_VARS,
    REDACTED,
    TOOLCHAIN_VARS,
    WINDOWS_BASE_VARS,
    CompleteEnv,
    build_env,
    is_secret_name,
    is_supervisor_var,
    redact,
    redact_env,
    redact_url,
)


@dataclass(frozen=True)
class _Agent:
    env: dict[str, str]


def _posix_parent(**extra: str) -> dict[str, str]:
    """A parent environment with the whole POSIX base set plus any `extra` variables."""
    parent = {name: f"parent-{name}" for name in POSIX_BASE_VARS}
    parent.update(extra)
    return parent


def _full_parent() -> dict[str, str]:
    """A parent environment with every allowlisted variable set plus deliberate noise."""
    parent: dict[str, str] = {}
    for name in (
        WINDOWS_BASE_VARS
        | POSIX_BASE_VARS
        | TOOLCHAIN_VARS
        | PROXY_VARS
        | CA_VARS
        | CONFIG_HOME_VARS
    ):
        parent[name] = f"parent-{name}"
    # Noise: ordinary variables that must never be forwarded wholesale.
    parent.update(
        {
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "DOCKER_CONFIG": "/noise/docker",
            "GITHUB_TOKEN": "ghp-noise",
            "PYTHONPATH": "/noise/py",
            "SOME_RANDOM_VAR": "noise",
        }
    )
    return parent


# --- allowlist constants match the design (section 9.4) ----------------------------------------


def test_windows_base_set_matches_the_design() -> None:
    assert {
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
    } == WINDOWS_BASE_VARS


def test_posix_base_set_matches_the_design() -> None:
    assert {
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
    } == POSIX_BASE_VARS


# --- platform allowlists -----------------------------------------------------------------------


def test_posix_build_returns_exactly_the_posix_allowlist() -> None:
    parent = _full_parent()
    built = build_env(parent_env=parent, platform="linux")
    assert set(built) == POSIX_BASE_VARS | TOOLCHAIN_VARS | PROXY_VARS | CA_VARS | CONFIG_HOME_VARS
    assert built["PATH"] == "parent-PATH"
    assert built["HOME"] == "parent-HOME"


def test_windows_build_returns_exactly_the_windows_allowlist() -> None:
    parent = _full_parent()
    built = build_env(parent_env=parent, platform="win32")
    assert (
        set(built) == WINDOWS_BASE_VARS | TOOLCHAIN_VARS | PROXY_VARS | CA_VARS | CONFIG_HOME_VARS
    )
    assert built["APPDATA"] == "parent-APPDATA"
    assert built["USERPROFILE"] == "parent-USERPROFILE"
    assert built["PATHEXT"] == "parent-PATHEXT"


def test_windows_variable_absent_from_parent_is_not_invented() -> None:
    parent = {name: "x" for name in WINDOWS_BASE_VARS if name != "WinDir"}
    built = build_env(parent_env=parent, platform="win32")
    assert "WinDir" not in built


def test_no_wholesale_parent_forwarding() -> None:
    parent = _full_parent()
    built = build_env(parent_env=parent, platform="linux")
    for noise in (
        "AWS_SECRET_ACCESS_KEY",
        "DOCKER_CONFIG",
        "GITHUB_TOKEN",
        "PYTHONPATH",
        "SOME_RANDOM_VAR",
    ):
        assert noise not in built


def test_toolchain_roots_only_when_present() -> None:
    built = build_env(parent_env=_posix_parent(JAVA_HOME="/jdk", GOPATH="/go"), platform="linux")
    assert built["JAVA_HOME"] == "/jdk"
    assert built["GOPATH"] == "/go"
    assert "CARGO_HOME" not in built


def test_proxy_variables_match_either_case() -> None:
    parent = _posix_parent(http_proxy="http://proxy:8080", HTTPS_PROXY="https://s:8443")
    built = build_env(parent_env=parent, platform="linux")
    assert built["http_proxy"] == "http://proxy:8080"  # copied under the original key
    assert built["HTTPS_PROXY"] == "https://s:8443"


def test_ca_and_config_home_variables_are_passed_when_set() -> None:
    parent = _posix_parent(
        SSL_CERT_FILE="/ca.pem",
        NODE_EXTRA_CA_CERTS="/node-ca.pem",
        CLAUDE_CONFIG_DIR="/claude-home",
        CODEX_HOME="/codex-home",
    )
    built = build_env(parent_env=parent, platform="linux")
    assert built["SSL_CERT_FILE"] == "/ca.pem"
    assert built["NODE_EXTRA_CA_CERTS"] == "/node-ca.pem"
    assert built["CLAUDE_CONFIG_DIR"] == "/claude-home"
    assert built["CODEX_HOME"] == "/codex-home"
    assert "GEMINI_CLI_HOME" not in built  # not set in the parent


# --- `env:` additions and `extra_env` ----------------------------------------------------------


def test_env_additions_and_extra_env_are_layered() -> None:
    agent = _Agent(env={"MY_AGENT_FLAG": "1", "PATH": "/from-config"})
    built = build_env(
        agent_config=agent,
        extra_env={"CURSOR_API_KEY": "cursor-secret", "MY_AGENT_FLAG": "2"},
        parent_env=_posix_parent(),
        platform="linux",
    )
    assert built["MY_AGENT_FLAG"] == "2"  # extra_env wins over env: additions
    assert built["CURSOR_API_KEY"] == "cursor-secret"
    assert built["PATH"] == "/from-config"  # env: addition overrides the base variable


def test_registry_agent_config_satisfies_the_protocol() -> None:
    """The real `agents.yaml` agent model can be passed straight in (section 3 `env:`)."""
    from relay.core.registry import Agent

    agent = Agent(cmd="claude", env={"CLAUDE_FLAG": "1"})
    built = build_env(agent_config=agent, parent_env=_posix_parent(), platform="linux")
    assert built["CLAUDE_FLAG"] == "1"


# --- supervisor secrets never forwarded ---------------------------------------------------------


def test_supervisor_secrets_are_never_forwarded_from_the_parent() -> None:
    parent = _posix_parent(RELAY_IPC_TOKEN="tok", RELAY_IPC_ENDPOINT="ep", RELAY_EXTRA="x")
    built = build_env(parent_env=parent, platform="linux")
    assert "RELAY_IPC_TOKEN" not in built
    assert "RELAY_IPC_ENDPOINT" not in built
    assert "RELAY_EXTRA" not in built  # the whole RELAY_ namespace is supervisor-owned


def test_supervisor_secrets_are_stripped_even_when_injected() -> None:
    agent = _Agent(env={"RELAY_RUN_SECRET": "run-secret", "OK_VAR": "yes"})
    built = build_env(
        agent_config=agent,
        extra_env={"RELAY_TOKEN": "extra-token"},
        parent_env=_posix_parent(),
        platform="linux",
    )
    assert "RELAY_RUN_SECRET" not in built
    assert "RELAY_TOKEN" not in built
    assert built["OK_VAR"] == "yes"


def test_is_supervisor_var() -> None:
    assert is_supervisor_var("RELAY_IPC_TOKEN")
    assert is_supervisor_var("relay_foo")
    assert not is_supervisor_var("PATH")


# --- redaction ---------------------------------------------------------------------------------


def test_redact_url_masks_userinfo() -> None:
    assert redact_url("http://user:pass@proxy.example.invalid:8080") == (
        f"http://{REDACTED}@proxy.example.invalid:8080"
    )
    assert redact_url("https://token@example.invalid/x") == f"https://{REDACTED}@example.invalid/x"
    assert redact_url("https://example.com/path") == "https://example.com/path"


def test_redact_replaces_secret_values() -> None:
    text = "the key is sk-ant-secret-123, really sk-ant-secret-123"
    assert redact(text, secrets={"ANTHROPIC_API_KEY": "sk-ant-secret-123"}) == (
        f"the key is {REDACTED}, really {REDACTED}"
    )


def test_redact_ignores_short_values() -> None:
    assert redact("x=1 y=1", secrets={"X": "1"}) == "x=1 y=1"


def test_redact_env_masks_token_bearing_names() -> None:
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "OPENAI_API_KEY": "sk-openai",
        "GITHUB_TOKEN": "ghp-secret",
        "CODECOV_TOKEN": "cc-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "MY_PASSWORD": "hunter2secret",
    }
    assert redact_env(env) == {name: REDACTED for name in env}


def test_redact_env_never_logs_proxy_credentials() -> None:
    proxy = "http://alice:correct-horse-battery@proxy.example.invalid:3128"
    env = {"HTTPS_PROXY": proxy, "http_proxy": proxy, "PATH": "/bin", "HOME": "/home"}
    rendered = redact_env(env)
    for value in rendered.values():
        assert "correct-horse-battery" not in value
        assert "alice" not in value
    assert rendered["HTTPS_PROXY"] == REDACTED  # proxy values are masked outright
    assert rendered["PATH"] == "/bin"  # non-secret values pass through


def test_redact_env_masks_userinfo_under_a_harmless_name() -> None:
    env = {"SOME_URL": "http://bob:s3cret@host:80"}
    assert redact_env(env) == {"SOME_URL": f"http://{REDACTED}@host:80"}


def test_build_then_redact_env_leaks_nothing() -> None:
    """End-to-end: the built environment renders to a log with no credentials."""
    proxy = "http://user:pass@proxy.example.invalid:8080"
    agent = _Agent(env={"ANTHROPIC_API_KEY": "sk-ant-built-secret"})
    built = build_env(
        agent_config=agent,
        extra_env={"CURSOR_API_KEY": "cursor-built-secret"},
        parent_env=_posix_parent(HTTPS_PROXY=proxy),
        platform="linux",
    )
    rendered = redact_env(built)
    for value in rendered.values():
        assert "pass" not in value
        assert "sk-ant-built-secret" not in value
        assert "cursor-built-secret" not in value
    assert rendered["ANTHROPIC_API_KEY"] == REDACTED
    assert rendered["CURSOR_API_KEY"] == REDACTED


def test_is_secret_name() -> None:
    assert is_secret_name("ANTHROPIC_API_KEY")
    assert is_secret_name("GITHUB_TOKEN")
    assert is_secret_name("HTTPS_PROXY")
    assert is_secret_name("http_proxy")
    assert is_secret_name("SSL_CERT_FILE")
    assert not is_secret_name("PATH")
    assert not is_secret_name("USER")


# --- the `full_env` contract in `run_invocation` (integration point for `build_env`) -----------


def _spawn_env(**extra: str) -> dict[str, str]:
    """A valid, allowlisted child environment (spawn-safe on Windows and POSIX)."""
    return {**build_env(parent_env=os.environ, platform=sys.platform), **extra}


def test_run_invocation_full_env_passes_the_exact_environment(tmp_path: Path) -> None:
    inv = Invocation(
        cmd=sys.executable,
        args=("-c", "import os,sys; sys.stdout.write(os.environ.get('TEST_FULL_ENV_MARKER', ''))"),
        cwd=tmp_path,
        env=_spawn_env(TEST_FULL_ENV_MARKER="child"),
        full_env=True,
    )
    result = run_invocation(inv)
    assert result.exit_code == 0
    assert result.stdout == "child"


def test_run_invocation_full_env_does_not_merge_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_ONLY_PARENT_MARKER", "parent-value")
    inv = Invocation(
        cmd=sys.executable,
        args=(
            "-c",
            "import os,sys; sys.stdout.write(repr(os.environ.get('TEST_ONLY_PARENT_MARKER')))",
        ),
        cwd=tmp_path,
        env=_spawn_env(),
        full_env=True,
    )
    result = run_invocation(inv)
    assert result.exit_code == 0
    assert result.stdout == repr(None)
    assert "parent-value" not in result.stdout


# --- fail-closed contract: a `build_env` result is complete even without `full_env=True` -------


def test_build_env_returns_a_complete_env_type() -> None:
    built = build_env(parent_env=_posix_parent(), platform="linux")
    assert isinstance(built, CompleteEnv)
    assert isinstance(built, dict)  # still an ordinary mapping for callers
    assert dict(built) == built


def test_build_env_result_is_complete_without_full_env_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2 regression: a forgotten `full_env=True` must not re-merge the parent environment."""
    monkeypatch.setenv("TEST_ONLY_PARENT_MARKER", "parent-value")
    assert "TEST_ONLY_PARENT_MARKER" in os.environ
    built = build_env(parent_env=os.environ, platform=sys.platform)
    assert "TEST_ONLY_PARENT_MARKER" not in built  # allowlist already excludes it
    inv = Invocation(
        cmd=sys.executable,
        args=(
            "-c",
            "import os,sys; sys.stdout.write(repr(os.environ.get('TEST_ONLY_PARENT_MARKER')))",
        ),
        cwd=tmp_path,
        env=built,  # no full_env=True
    )
    result = run_invocation(inv)
    assert result.exit_code == 0
    assert result.stdout == repr(None)


def test_plain_dict_env_still_merges_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back-compat: an ordinary dict env (not a `build_env` result) still merges."""
    monkeypatch.setenv("TEST_ONLY_PARENT_MARKER", "parent-value")
    inv = Invocation(
        cmd=sys.executable,
        args=(
            "-c",
            "import os,sys; sys.stdout.write(os.environ.get('TEST_ONLY_PARENT_MARKER', ''))",
        ),
        cwd=tmp_path,
        env={"TEST_CHILD_ONLY": "child"},
    )
    result = run_invocation(inv)
    assert result.exit_code == 0
    assert result.stdout == "parent-value"
