"""Environment builder and redaction (task 1.8, design section 9.4)."""

from __future__ import annotations

from dataclasses import dataclass

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
    is_supervisor_var,
    redact,
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


def test_windows_names_are_case_insensitive_and_use_canonical_spellings() -> None:
    built = build_env(
        parent_env={"SYSTEMROOT": "system-root", "Path": "tool-path", "codex_home": "config-home"},
        platform="win32",
    )
    assert built == {
        "SystemRoot": "system-root",
        "PATH": "tool-path",
        "CODEX_HOME": "config-home",
    }


def test_windows_overrides_replace_keys_without_case_duplicates() -> None:
    built = build_env(
        parent_env={"SYSTEMROOT": "original", "PATH": "original"},
        agent_config=_Agent(env={"systemroot": "configured", "Path": "configured"}),
        extra_env={"pAtH": "invocation", "relay_token": "must-not-leak"},
        platform="win32",
    )
    assert built == {"SystemRoot": "configured", "PATH": "invocation"}


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


def test_build_env_returns_a_complete_env_type() -> None:
    built = build_env(parent_env=_posix_parent(), platform="linux")
    assert isinstance(built, CompleteEnv)
    assert isinstance(built, dict)
