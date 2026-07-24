"""Regression tests for Codex refresh_token self-heal (cross-store rotation).

Hermes keeps its OWN copy of the Codex OAuth token (per profile + top-level),
separate from the Codex CLI's ``~/.codex/auth.json``. OAuth refresh_tokens are
single-use, so when the Codex CLI (or another Hermes process) rotates the shared
token, the frozen copy's refresh_token goes stale and ``refresh_codex_oauth_pure``
fails with a relogin-required error. ``_refresh_codex_auth_tokens`` must then
recover by re-importing the canonical token from ``~/.codex/auth.json`` instead of
surfacing a hard 401 — but ONLY for relogin-required failures, never for transient
ones (e.g. 429 quota, where the stored token is still valid).
"""

import json

import pytest

import hermes_cli.auth as auth
from hermes_cli.auth import AuthError, _refresh_codex_auth_tokens, resolve_codex_runtime_credentials

STALE = {"access_token": "stale-access", "refresh_token": "stale-refresh"}


def test_self_heals_on_stale_refresh_token(monkeypatch):
    """invalid_grant (relogin-required) → reimport from ~/.codex and persist it."""
    saved = {}
    fresh = {
        "access_token": "fresh-access",
        "refresh_token": "fresh-refresh",
        "last_refresh": "2026-06-12T00:00:00Z",
    }

    def _rejected(*_a, **_k):
        raise AuthError(
            "refresh token rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rejected)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", lambda: dict(fresh))
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda t, *a, **k: saved.update(t))

    out = _refresh_codex_auth_tokens(STALE, 20.0)

    assert out["access_token"] == "fresh-access"
    assert out["refresh_token"] == "fresh-refresh"
    # the recovered token was persisted to the Hermes auth store
    assert saved["access_token"] == "fresh-access"


def test_does_not_self_heal_on_rate_limit(monkeypatch):
    """429 quota keeps relogin_required=False — token still valid, must NOT reimport."""
    import_calls = {"n": 0}

    def _rate_limited(*_a, **_k):
        raise AuthError(
            "quota exhausted",
            provider="openai-codex",
            code="codex_rate_limited",
            relogin_required=False,
        )

    def _import_spy():
        import_calls["n"] += 1
        return {"access_token": "should-not-be-used"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rate_limited)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda *a, **k: None)

    with pytest.raises(AuthError) as ei:
        _refresh_codex_auth_tokens(STALE, 20.0)

    assert ei.value.code == "codex_rate_limited"
    assert import_calls["n"] == 0  # never touched ~/.codex on a transient failure


def test_reraises_when_codex_cli_token_absent(monkeypatch):
    """relogin-required but ~/.codex unavailable/expired → propagate original error."""

    def _reused(*_a, **_k):
        raise AuthError(
            "refresh token reused",
            provider="openai-codex",
            code="refresh_token_reused",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _reused)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", lambda: None)
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda *a, **k: None)

    with pytest.raises(AuthError) as ei:
        _refresh_codex_auth_tokens(STALE, 20.0)

    assert ei.value.code == "refresh_token_reused"


def test_happy_path_unchanged(monkeypatch):
    """Normal refresh succeeds → rotated tokens persisted, ~/.codex never consulted."""
    saved = {}
    import_calls = {"n": 0}

    def _import_spy():
        import_calls["n"] += 1
        return None

    monkeypatch.setattr(
        auth,
        "refresh_codex_oauth_pure",
        lambda *a, **k: {"access_token": "rotated", "refresh_token": "rotated-r"},
    )
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", _import_spy)
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda t, *a, **k: saved.update(t))

    out = _refresh_codex_auth_tokens({"access_token": "a", "refresh_token": "b"}, 20.0)

    assert out["access_token"] == "rotated"
    assert out["refresh_token"] == "rotated-r"
    assert saved["access_token"] == "rotated"
    assert import_calls["n"] == 0  # happy path must not consult ~/.codex


def test_reraises_when_imported_token_lacks_refresh_token(monkeypatch):
    """relogin-required, but ~/.codex returns an access_token with NO refresh_token →
    re-raise rather than persist a half-token that would break the next refresh."""
    saved = {}

    def _rejected(*_a, **_k):
        raise AuthError(
            "refresh token rejected",
            provider="openai-codex",
            code="invalid_grant",
            relogin_required=True,
        )

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rejected)
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", lambda: {"access_token": "fresh-only"})
    monkeypatch.setattr(auth, "_save_codex_tokens", lambda t, *a, **k: saved.update(t))

    with pytest.raises(AuthError) as ei:
        _refresh_codex_auth_tokens(STALE, 20.0)

    assert ei.value.code == "invalid_grant"
    assert saved == {}  # nothing was persisted


def test_self_heals_missing_singleton_access_token_from_codex_cli(tmp_path, monkeypatch):
    """Exact cron failure path: Hermes auth has refresh_token but missing access_token."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {"refresh_token": "stale-refresh"},
                "last_refresh": "2026-06-01T00:00:00Z",
                "auth_mode": "chatgpt",
            },
        },
    }))
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    resolved = resolve_codex_runtime_credentials()

    assert resolved["api_key"] == "fresh-access"
    assert resolved["source"] == "hermes-auth-store"
    stored = json.loads((hermes_home / "auth.json").read_text())
    tokens = stored["providers"]["openai-codex"]["tokens"]
    assert tokens["access_token"] == "fresh-access"
    assert tokens["refresh_token"] == "fresh-refresh"


def test_missing_singleton_access_token_reraises_when_codex_cli_half_token(tmp_path, monkeypatch):
    """Missing access_token must not be masked by a malformed Codex CLI import."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "tokens": {"refresh_token": "stale-refresh"},
                "auth_mode": "chatgpt",
            },
        },
    }))
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": "fresh-only"},
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(AuthError) as ei:
        resolve_codex_runtime_credentials()

    assert ei.value.code == "codex_auth_missing_access_token"


def test_pool_only_singleton_does_not_import_codex_cli_tokens(tmp_path, monkeypatch):
    """An explicit pool-only shadow must never be promoted from CODEX_HOME.

    Regression: the missing-singleton self-heal ran before the credential-pool
    fallback.  A scheduled canary therefore copied an unrelated Codex CLI
    account into ``providers.openai-codex``, changed ``auth_mode`` to
    ``chatgpt``, and bypassed the profile's selected pool credential.
    """
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex"
    hermes_home.mkdir()
    codex_home.mkdir()
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "auth_mode": "pool_only",
                "credential_role": "pool-only",
                "source": "manual:pool-only-shadow",
                "tokens": {},
            },
        },
        "credential_pool": {
            "openai-codex": [{
                "id": "pool-primary",
                "label": "pool-primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:test-independent-primary",
                "access_token": "pool-access",
                "refresh_token": "pool-refresh",
            }],
        },
    }))
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {
            "access_token": "unrelated-cli-access",
            "refresh_token": "unrelated-cli-refresh",
        },
    }))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(auth, "_codex_access_token_is_expiring", lambda *_args, **_kwargs: False)

    resolved = resolve_codex_runtime_credentials()

    assert resolved["api_key"] == "pool-access"
    assert resolved["source"] == "credential_pool"
    stored = json.loads((hermes_home / "auth.json").read_text())
    shadow = stored["providers"]["openai-codex"]
    assert shadow["auth_mode"] == "pool_only"
    assert shadow["credential_role"] == "pool-only"
    assert not shadow.get("tokens")


def _write_pool_only_shadow(hermes_home):
    hermes_home.mkdir(exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {
            "openai-codex": {
                "auth_mode": "pool_only",
                "credential_role": "pool-only",
                "source": "manual:pool-only-shadow",
                "tokens": {},
            },
        },
    }))


def test_common_codex_cli_recovery_skips_pool_only_shadow(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _write_pool_only_shadow(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    import_calls = []
    save_calls = []
    monkeypatch.setattr(
        auth,
        "_import_codex_cli_tokens",
        lambda: import_calls.append(True) or {
            "access_token": "unrelated-cli-access",
            "refresh_token": "unrelated-cli-refresh",
        },
    )
    monkeypatch.setattr(
        auth,
        "_save_codex_tokens",
        lambda tokens, *args, **kwargs: save_calls.append(dict(tokens)),
    )

    recovered = auth._recover_codex_tokens_from_cli("automatic-test")

    assert recovered is None
    assert import_calls == []
    assert save_calls == []


def test_automatic_codex_save_preserves_pool_only_shadow(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _write_pool_only_shadow(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    auth._save_codex_tokens({
        "access_token": "unrelated-access",
        "refresh_token": "unrelated-refresh",
    })

    stored = json.loads((hermes_home / "auth.json").read_text())
    shadow = stored["providers"]["openai-codex"]
    assert shadow["auth_mode"] == "pool_only"
    assert shadow["credential_role"] == "pool-only"
    assert not shadow.get("tokens")


def test_explicit_codex_login_can_convert_pool_only_shadow(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _write_pool_only_shadow(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    auth._save_codex_tokens(
        {
            "access_token": "explicit-access",
            "refresh_token": "explicit-refresh",
        },
        allow_pool_only=True,
    )

    stored = json.loads((hermes_home / "auth.json").read_text())
    singleton = stored["providers"]["openai-codex"]
    assert singleton["auth_mode"] == "chatgpt"
    assert "credential_role" not in singleton
    assert "source" not in singleton
    assert singleton["tokens"]["access_token"] == "explicit-access"
