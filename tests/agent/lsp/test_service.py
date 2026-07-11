"""Tests for the synchronous LSPService wrapper.

Drives the service through ``snapshot_baseline`` →
``get_diagnostics_sync`` against the mock LSP server, exercising the
delta filter that ``tools/file_operations._check_lint_delta`` relies
on.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import cast

import pytest

from agent.lsp.client import LSPClient
from agent.lsp.manager import LSPService
from agent.lsp.servers import (
    SERVERS,
    ServerContext,
    ServerDef,
    SpawnSpec,
)


MOCK_SERVER = str(Path(__file__).parent / "_mock_lsp_server.py")


def _install_mock_server(monkeypatch, script: str = "errors", server_id: str = "pyright"):
    """Replace one registered server with a wrapper that spawns the mock.

    We reuse ``pyright`` so .py files route to it.  This keeps the
    test free of any LSP toolchain dependency.
    """
    target_index = next(i for i, s in enumerate(SERVERS) if s.server_id == server_id)
    original = SERVERS[target_index]

    def _spawn(root: str, ctx: ServerContext) -> SpawnSpec:
        env = {"MOCK_LSP_SCRIPT": script}
        return SpawnSpec(
            command=[sys.executable, MOCK_SERVER],
            workspace_root=root,
            cwd=root,
            env=env,
            initialization_options={},
        )

    replacement = ServerDef(
        server_id=server_id,
        extensions=original.extensions,
        resolve_root=lambda fp, ws: ws,  # always use workspace root
        build_spawn=_spawn,
        seed_first_push=False,
        description="mock " + server_id,
    )
    # Patch the SERVERS list element directly + restore on teardown.
    SERVERS[target_index] = replacement

    yield

    SERVERS[target_index] = original


@pytest.fixture
def mock_pyright(monkeypatch, tmp_path):
    """Install the mock as ``pyright`` and create a fake git workspace."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text("")  # so pyright's root resolver finds it
    monkeypatch.chdir(str(repo))
    gen = _install_mock_server(monkeypatch, "errors", "pyright")
    next(gen)
    yield repo
    try:
        next(gen)
    except StopIteration:
        pass


def test_service_returns_empty_when_disabled(tmp_path):
    svc = LSPService(
        enabled=False,
        wait_mode="document",
        wait_timeout=2.0,
        install_strategy="auto",
    )
    assert not svc.is_active()
    f = tmp_path / "x.py"
    f.write_text("")
    assert svc.get_diagnostics_sync(str(f)) == []
    svc.shutdown()


def test_service_skips_files_outside_workspace(tmp_path):
    """Files outside any git worktree must not trigger LSP."""
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=2.0,
        install_strategy="manual",
    )
    f = tmp_path / "x.py"
    f.write_text("")
    # No .git anywhere — service should report not enabled for this file.
    assert not svc.enabled_for(str(f))
    svc.shutdown()


def test_service_e2e_delta_filter(mock_pyright):
    """End-to-end: snapshot baseline → wait → delta returned."""
    repo = mock_pyright
    f = repo / "x.py"
    f.write_text("print('hi')\n")

    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=3.0,
        install_strategy="manual",
    )
    try:
        assert svc.enabled_for(str(f))
        # Baseline first — server pushes 1 error.
        svc.snapshot_baseline(str(f))
        # Re-poll: same error is in baseline, so delta is empty.
        new_diags = svc.get_diagnostics_sync(str(f))
        assert new_diags == []
    finally:
        svc.shutdown()


def test_service_e2e_delta_filter_with_line_shift(mock_pyright):
    """End-to-end: an edit that shifts the diagnostic's line still
    filters correctly when ``line_shift`` is supplied.

    The mock LSP server emits a fixed error at line 0; for this test
    we don't need to actually shift the server's output — we just
    need to prove that supplying a line_shift through the API works
    and doesn't break the existing delta path.  The unit tests in
    test_delta_key.py cover the shift semantics in detail.
    """
    repo = mock_pyright
    f = repo / "x.py"
    f.write_text("print('hi')\n")

    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=3.0,
        install_strategy="manual",
    )
    try:
        svc.snapshot_baseline(str(f))
        # Identity shift — should behave exactly like no shift.
        new_diags = svc.get_diagnostics_sync(str(f), line_shift=lambda L: L)
        assert new_diags == []
    finally:
        svc.shutdown()


def test_service_status_includes_clients(mock_pyright):
    repo = mock_pyright
    f = repo / "x.py"
    f.write_text("")
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=3.0,
        install_strategy="manual",
    )
    try:
        svc.get_diagnostics_sync(str(f))
        info = svc.get_status()
        assert info["enabled"] is True
        assert info["idle_timeout"] == 600.0
        assert info["reaper_running"] is True
        assert any(c["server_id"] == "pyright" for c in info["clients"])
    finally:
        svc.shutdown()


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _FakeClient:
    def __init__(self, server_id: str, workspace_root: str, *, fail_shutdown: bool = False):
        self.server_id = server_id
        self.workspace_root = workspace_root
        self.state = "running"
        self.is_running = True
        self.fail_shutdown = fail_shutdown
        self.shutdown_calls = 0
        self.manager: LSPService | None = None

    async def shutdown(self):
        self.shutdown_calls += 1
        if self.manager is not None:
            assert self.manager._state_lock.acquire(blocking=False)
            self.manager._state_lock.release()
        if self.fail_shutdown:
            raise RuntimeError("expected shutdown failure")
        self.state = "stopped"
        self.is_running = False


def test_idle_reaper_runs_automatically_and_client_respawns(mock_pyright):
    repo = mock_pyright
    f = repo / "x.py"
    f.write_text("print('hi')\n")

    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=3.0,
        install_strategy="manual",
        idle_timeout=0.2,
    )
    try:
        svc.get_diagnostics_sync(str(f))
        with svc._state_lock:
            first = next(iter(svc._clients.values()))
        assert _wait_until(lambda: len(svc.get_status()["clients"]) == 0)

        svc.get_diagnostics_sync(str(f))
        with svc._state_lock:
            second = next(iter(svc._clients.values()))
        assert second is not first
        assert second.is_running
    finally:
        svc.shutdown()


def test_idle_reaper_keeps_recent_and_in_flight_clients(tmp_path):
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=1.0,
        install_strategy="manual",
        idle_timeout=100.0,
    )
    recent_key = ("recent", str(tmp_path / "recent"))
    active_key = ("active", str(tmp_path / "active"))
    recent = _FakeClient(*recent_key)
    active = _FakeClient(*active_key)
    try:
        with svc._state_lock:
            svc._clients[recent_key] = cast(LSPClient, recent)
            svc._clients[active_key] = cast(LSPClient, active)
            svc._last_used[recent_key] = time.monotonic()
            svc._last_used[active_key] = time.monotonic() - 200.0
            svc._in_flight[active_key] = 1

        svc._loop.run(svc._reap_idle_clients(), timeout=2.0)
        with svc._state_lock:
            assert svc._clients[recent_key] is recent
            assert svc._clients[active_key] is active
    finally:
        svc.shutdown()


def test_idle_reaper_tracks_workspaces_independently_and_isolates_shutdown_errors(tmp_path):
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=1.0,
        install_strategy="manual",
        idle_timeout=100.0,
    )
    failing_key = ("pyright", str(tmp_path / "failing-stale"))
    second_key = ("typescript", str(tmp_path / "second-stale"))
    fresh_key = ("pyright", str(tmp_path / "fresh"))
    failing = _FakeClient(*failing_key, fail_shutdown=True)
    second = _FakeClient(*second_key)
    fresh = _FakeClient(*fresh_key)
    failing.manager = svc
    second.manager = svc
    fresh.manager = svc
    try:
        with svc._state_lock:
            svc._clients[failing_key] = cast(LSPClient, failing)
            svc._clients[second_key] = cast(LSPClient, second)
            svc._clients[fresh_key] = cast(LSPClient, fresh)
            svc._last_used[failing_key] = time.monotonic() - 200.0
            svc._last_used[second_key] = time.monotonic() - 200.0
            svc._last_used[fresh_key] = time.monotonic()

        svc._loop.run(svc._reap_idle_clients(), timeout=2.0)
        with svc._state_lock:
            assert failing_key not in svc._clients
            assert second_key not in svc._clients
            assert svc._clients[fresh_key] is fresh
        assert failing.shutdown_calls == 1
        assert second.shutdown_calls == 1
        assert fresh.shutdown_calls == 0
    finally:
        svc.shutdown()


def test_client_claim_blocks_reaping_until_operation_finishes(mock_pyright):
    repo = mock_pyright
    f = repo / "x.py"
    f.write_text("print('hi')\n")
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=1.0,
        install_strategy="manual",
        idle_timeout=100.0,
    )
    try:
        client = svc._loop.run(svc._get_or_spawn(str(f), claim=True), timeout=3.0)
        assert client is not None
        key = (client.server_id, client.workspace_root)
        with svc._state_lock:
            assert svc._in_flight[key] == 1
            svc._last_used[key] = time.monotonic() - 200.0

        svc._loop.run(svc._reap_idle_clients(), timeout=2.0)
        with svc._state_lock:
            assert svc._clients[key] is client

        svc._finish_client_use(client)
        with svc._state_lock:
            svc._last_used[key] = time.monotonic() - 200.0
        svc._loop.run(svc._reap_idle_clients(), timeout=2.0)
        with svc._state_lock:
            assert key not in svc._clients
    finally:
        svc.shutdown()


def test_idle_timeout_zero_disables_reaper_and_start_is_singleton():
    disabled = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=1.0,
        install_strategy="manual",
        idle_timeout=0,
    )
    try:
        assert disabled.get_status()["reaper_running"] is False
    finally:
        disabled.shutdown()

    enabled = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=1.0,
        install_strategy="manual",
        idle_timeout=60,
    )
    try:
        first = enabled._reaper_task
        enabled._start_reaper()
        assert enabled._reaper_task is first
    finally:
        enabled.shutdown()


def test_idle_timeout_config_and_invalid_values(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"lsp": {"enabled": False, "idle_timeout": 42}},
    )
    configured = LSPService.create_from_config()
    assert configured is not None
    assert configured.get_status()["idle_timeout"] == 42.0
    configured.shutdown()

    for invalid in (-1, float("inf"), float("nan"), "invalid"):
        svc = LSPService(
            enabled=False,
            wait_mode="document",
            wait_timeout=1.0,
            install_strategy="manual",
            idle_timeout=invalid,
        )
        assert svc.get_status()["idle_timeout"] == 600.0
        svc.shutdown()


def test_shutdown_cancels_reaper_before_stopping_loop_and_is_idempotent():
    svc = LSPService(
        enabled=True,
        wait_mode="document",
        wait_timeout=1.0,
        install_strategy="manual",
        idle_timeout=60,
    )
    task = svc._reaper_task
    assert task is not None
    svc.shutdown()
    svc.shutdown()
    assert svc._reaper_task is None
    assert svc._reaper_running is False
    assert svc._loop._thread is None
    assert task.done()
