import asyncio
from pathlib import Path


from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    db_path = tmp_path / "profile-no-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


def test_notifier_uses_active_named_profile_adapter(tmp_path, monkeypatch):
    """A dedicated named gateway must deliver its own stamped subscription.

    Dedicated gateways keep their live adapters in ``self.adapters``.  The
    subscription still carries the concrete profile name so another gateway
    cannot send it through the wrong bot.  Resolution must therefore treat a
    stamp matching the runner's active profile as the local adapter map.
    """
    db_path = tmp_path / "dedicated-profile.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by maker", assignee="maker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-maker",
            notifier_profile="maker",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    runner._kanban_notifier_profile = "maker"
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "maker"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "chat-maker"
    assert tid in adapter.sent[0]["text"]

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn) == []
    finally:
        conn.close()


def test_named_multiplexer_uses_secondary_default_profile_adapter(tmp_path, monkeypatch):
    """A named multiplexer delivers default-owned subs via the default adapter."""
    db_path = tmp_path / "multiplexed-default-profile.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by default", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-default",
            notifier_profile="default",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    maker_adapter = RecordingAdapter()
    default_adapter = RecordingAdapter()
    runner = _make_runner(maker_adapter)
    runner.adapters = {Platform.DISCORD: maker_adapter}  # type: ignore[assignment]
    runner._kanban_notifier_profile = "maker"
    runner._profile_adapters = {  # type: ignore[assignment]
        "default": {Platform.TELEGRAM: default_adapter},
    }
    runner._active_profile_name = lambda: "maker"

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert maker_adapter.sent == []
    assert len(default_adapter.sent) == 1
    assert default_adapter.sent[0]["chat_id"] == "chat-default"
    assert tid in default_adapter.sent[0]["text"]

    conn = kb.connect()
    try:
        assert kb.list_notify_subs(conn) == []
    finally:
        conn.close()


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def _create_running_progress_subscription(*, title="Human task", notifier_profile=None):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee=notifier_profile or "worker")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task is not None and task.current_run_id is not None
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="progress-chat",
            notifier_profile=notifier_profile,
        )
        assert kb.record_worker_progress(
            conn,
            tid,
            text="Terminado: el análisis. Ahora: validando el flujo.",
            expected_run_id=task.current_run_id,
        ) == "recorded"
        return tid, int(task.current_run_id)
    finally:
        conn.close()


def test_notifier_delivers_human_progress_without_task_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress.db"))
    kb.init_db()
    tid, _ = _create_running_progress_subscription(title="Publicar vídeo")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert text.startswith("Kanban update")
    assert "Publicar vídeo" in text
    assert "Ahora: validando el flujo" in text
    assert tid not in text


def test_notifier_force_redacts_progress_title(tmp_path, monkeypatch):
    import agent.redact as redact_module

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-title.db"))
    monkeypatch.setattr(redact_module, "_REDACT_ENABLED", False)
    kb.init_db()
    secret = "ghp_" + "1234567890abcdefghijklmnop"
    _create_running_progress_subscription(title=f"Publicar {secret}")

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert secret not in adapter.sent[0]["text"]


def test_notifier_coalesces_progress_backlog_to_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-coalesce.db"))
    kb.init_db()
    tid, run_id = _create_running_progress_subscription()
    conn = kb.connect()
    try:
        assert kb.record_worker_progress(
            conn, tid, text="Ahora: segundo hito.", expected_run_id=run_id
        ) == "recorded"
        assert kb.record_worker_progress(
            conn, tid, text="Ahora: último hito.", expected_run_id=run_id
        ) == "recorded"
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "último hito" in adapter.sent[0]["text"]
    assert "segundo hito" not in adapter.sent[0]["text"]


def test_notifier_terminal_event_takes_priority_over_queued_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-terminal.db"))
    kb.init_db()
    tid, _ = _create_running_progress_subscription()
    conn = kb.connect()
    try:
        assert kb.complete_task(conn, tid, summary="Entrega completada")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "done" in adapter.sent[0]["text"].lower()
    assert "Kanban update" not in adapter.sent[0]["text"]


def test_progress_notification_never_falls_back_to_default_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-profile.db"))
    kb.init_db()
    tid, _ = _create_running_progress_subscription(notifier_profile="beta")

    default_adapter = RecordingAdapter()
    discord_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: default_adapter}  # type: ignore[assignment]
    runner._profile_adapters = {  # type: ignore[assignment]
        "beta": {Platform.DISCORD: discord_adapter}  # type: ignore[dict-item]
    }
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert default_adapter.sent == []
    assert discord_adapter.sent == []
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="progress-chat",
            kinds=["progress"],
        )
        assert [e.kind for e in events] == ["progress"]
    finally:
        conn.close()


def test_failed_progress_send_cannot_delay_later_terminal_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-rewind.db"))
    kb.init_db()
    tid, _ = _create_running_progress_subscription()

    failing = FailingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(failing)))
    assert failing.attempts == 1

    conn = kb.connect()
    try:
        assert kb.complete_task(conn, tid, summary="Entrega completada")
    finally:
        conn.close()

    recording = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(recording)))

    assert len(recording.sent) == 1
    assert "done" in recording.sent[0]["text"].lower()
    assert "Kanban update" not in recording.sent[0]["text"]


def test_multiplexer_routes_progress_through_owning_profile_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-multiplexer.db"))
    kb.init_db()
    _create_running_progress_subscription(notifier_profile="beta")

    default_adapter = RecordingAdapter()
    beta_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: default_adapter}  # type: ignore[assignment]
    runner._profile_adapters = {  # type: ignore[assignment]
        "beta": {Platform.TELEGRAM: beta_adapter}  # type: ignore[dict-item]
    }
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert default_adapter.sent == []
    assert len(beta_adapter.sent) == 1
    assert beta_adapter.sent[0]["text"].startswith("Kanban update")


def test_progress_recheck_suppresses_completion_between_claim_and_send(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-interleave.db"))
    kb.init_db()
    tid, _ = _create_running_progress_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    def complete_before_delivery(_platform, _profile=None):
        conn = kb.connect()
        try:
            assert kb.complete_task(conn, tid, summary="Entrega completada")
        finally:
            conn.close()
        return adapter

    setattr(runner, "_authorization_adapter", complete_before_delivery)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert adapter.sent == []

    terminal_adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(terminal_adapter)))
    assert len(terminal_adapter.sent) == 1
    assert "done" in terminal_adapter.sent[0]["text"].lower()


def test_progress_send_serializes_terminal_transition_at_network_boundary(
    tmp_path, monkeypatch
):
    import threading
    import time

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-send-guard.db"))
    kb.init_db()
    tid, _ = _create_running_progress_subscription()

    class CompletingDuringSendAdapter(RecordingAdapter):
        def __init__(self):
            super().__init__()
            self.transition_started = threading.Event()
            self.transition_completed = threading.Event()
            self.transition_thread = None

        async def send(self, chat_id, text, metadata=None):
            def complete_while_send_is_open():
                self.transition_started.set()
                conn = kb.connect()
                try:
                    assert kb.complete_task(conn, tid, summary="Entrega completada")
                finally:
                    conn.close()
                self.transition_completed.set()

            self.transition_thread = threading.Thread(
                target=complete_while_send_is_open,
                daemon=True,
            )
            self.transition_thread.start()
            assert await asyncio.to_thread(self.transition_started.wait, 1.0)
            # The terminal writer has started but must be blocked behind the
            # progress guard until this send returns.
            await asyncio.to_thread(time.sleep, 0.05)
            assert not self.transition_completed.is_set()
            await super().send(chat_id, text, metadata=metadata)

    progress_adapter = CompletingDuringSendAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(progress_adapter)))
    assert len(progress_adapter.sent) == 1
    assert progress_adapter.sent[0]["text"].startswith("Kanban update")
    assert progress_adapter.transition_thread is not None
    progress_adapter.transition_thread.join(timeout=2.0)
    assert progress_adapter.transition_completed.is_set()

    terminal_adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(terminal_adapter)))
    assert len(terminal_adapter.sent) == 1
    assert "done" in terminal_adapter.sent[0]["text"].lower()


def test_notifier_replaces_technical_progress_with_safe_copy(tmp_path, monkeypatch):
    from agent.progress_copy import DEFAULT_HUMAN_PROGRESS_TEXT

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-copy.db"))
    kb.init_db()
    tid, run_id = _create_running_progress_subscription()
    unsafe = (
        "Ahora: ejecuto `pytest /private/repo/test_flow.py` con Claude; "
        "reviso traceback y logs de stderr."
    )
    conn = kb.connect()
    try:
        assert kb.record_worker_progress(
            conn,
            tid,
            text=unsafe,
            expected_run_id=run_id,
        ) == "recorded"
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert DEFAULT_HUMAN_PROGRESS_TEXT in text
    for internal in ("pytest", "/private/repo", "Claude", "traceback", "stderr"):
        assert internal not in text
