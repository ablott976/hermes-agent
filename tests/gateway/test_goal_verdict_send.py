"""Tests for gateway /goal verdict-message delivery.

The judge verdict message ("✓ Goal achieved", "⏸ budget exhausted", etc.)
must reach the user after each turn. Before this fix the code checked
``hasattr(adapter, "send_message")`` — but adapters expose ``send()``,
never ``send_message``, so the check always evaluated False and users
never saw verdicts. This test locks in the fix.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


class _RecordingAdapter:
    """Minimal adapter that records send() invocations."""

    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list[dict] = []

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        self.sends.append({
            "chat_id": chat_id,
            "content": content,
            "metadata": metadata,
        })

        class _R:
            success = True
            message_id = "mock-msg"

        return _R()


def _make_runner_with_adapter(session_id: str = None):
    from gateway.run import GatewayRunner
    import uuid

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._queued_events = {}

    src = _make_source()
    # Default to a unique session_id so xdist parallel runs on the same worker
    # don't see each other's GoalManager state (DEFAULT_DB_PATH gets frozen at
    # module-import time, defeating per-test HERMES_HOME monkeypatches).
    session_entry = SessionEntry(
        session_key=build_session_key(src),
        session_id=session_id or f"goal-sess-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )

    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)

    adapter = _RecordingAdapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter, session_entry, src


@pytest.mark.asyncio
async def test_goal_verdict_done_sent_via_adapter_send(hermes_home):
    """When the judge says done, the '✓ Goal achieved' message must reach
    the user through the adapter's ``send()`` method."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_entry.session_id)
    mgr.set("ship the feature")

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("done", "the feature shipped", False, None, False),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="I shipped the feature.",
        )
        # fire-and-forget create_task — give the loop a tick
        await asyncio.sleep(0.05)

    assert len(adapter.sends) == 1, (
        f"expected 1 send, got {len(adapter.sends)}: {adapter.sends}"
    )
    msg = adapter.sends[0]
    assert msg["chat_id"] == "c1"
    assert "Goal achieved" in msg["content"]
    assert "the feature shipped" in msg["content"]


@pytest.mark.asyncio
async def test_goal_verdict_continue_enqueues_continuation(hermes_home):
    """When the judge says continue, both the 'continuing' status and the
    continuation-prompt event must be delivered. The continuation prompt is
    routed through the adapter's pending-messages FIFO so the goal loop
    proceeds on the next turn."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_entry.session_id)
    mgr.set("polish the docs")

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "still needs work", False, None, False),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="here's a partial edit",
        )
        await asyncio.sleep(0.05)

    # Status line sent back
    assert len(adapter.sends) == 1
    assert "Continuing toward goal" in adapter.sends[0]["content"]
    # Continuation prompt enqueued for next turn
    assert adapter._pending_messages, (
        "continuation prompt must be enqueued in pending_messages"
    )


@pytest.mark.asyncio
async def test_goal_verdict_budget_exhausted_sends_pause(hermes_home):
    """When the budget is exhausted, a '⏸ Goal paused' message must be sent
    and no further continuation enqueued."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager, save_goal

    mgr = GoalManager(session_entry.session_id, default_max_turns=2)
    state = mgr.set("tiny goal", max_turns=2)
    state.turns_used = 2
    save_goal(session_entry.session_id, state)

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "keep going", False, None, False),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="still partial",
        )
        await asyncio.sleep(0.05)

    assert len(adapter.sends) == 1
    content = adapter.sends[0]["content"]
    assert "paused" in content.lower()
    assert "turns used" in content.lower()
    # No continuation enqueued when budget is exhausted
    assert not adapter._pending_messages


@pytest.mark.asyncio
async def test_goal_budget_rollover_is_deferred_until_fifo_head(hermes_home):
    runner, adapter, session_entry, src = _make_runner_with_adapter()
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager, load_goal

    runner._goal_max_turns_from_config = lambda: 2
    runner._goal_auto_rollover_from_config = lambda: True
    runner._goal_repeat_checkpoint_limit_from_config = lambda: 3

    mgr = GoalManager(session_entry.session_id, default_max_turns=2)
    state = mgr.set("continue across a fresh root", max_turns=2)
    state.turns_used = 1
    goals.save_goal(session_entry.session_id, state)

    with patch(
        "hermes_cli.goals.judge_goal",
        return_value=("continue", "more work", False, None, False),
    ):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="first slice completed; inspect the repository next",
        )
        await asyncio.sleep(0.05)

    # A normal user event already ahead of this marker remains on the old root;
    # migration happens only when the marker is actually consumed.
    assert load_goal("fresh-goal-session") is None
    original = load_goal(session_entry.session_id)
    assert original is not None and original.status == "active"
    pending = next(iter(adapter._pending_messages.values()))
    assert pending.metadata["goal_session_rollover"] is True
    assert pending.metadata["goal_rollover_from_session_id"] == session_entry.session_id
    assert "gateway_session_id" not in pending.metadata


@pytest.mark.asyncio
async def test_user_stop_before_rollover_marker_cancels_fresh_session(hermes_home):
    runner, _adapter, session_entry, src = _make_runner_with_adapter()
    from hermes_cli.goals import GoalManager, clear_goal

    class _Store:
        def __init__(self):
            self.created = 0

        def get_or_create_session(self, source, force_new=False):
            self.created += 1
            raise AssertionError("rollover must not create a session after user stop")

    runner.session_store = _Store()
    GoalManager(session_entry.session_id).set("stop before rollover")
    # This represents /goal stop being handled by the FIFO before its marker.
    clear_goal(session_entry.session_id)

    result = await runner._rollover_goal_at_fifo_head(
        old_session_id=session_entry.session_id,
        session_key=session_entry.session_key,
        source=src,
    )
    assert result is None
    assert runner.session_store.created == 0


@pytest.mark.asyncio
async def test_goal_rollover_at_fifo_head_migrates_state(hermes_home):
    runner, _adapter, session_entry, src = _make_runner_with_adapter()
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager, load_goal

    class _Store:
        def __init__(self):
            self.new_entry = SessionEntry(
                session_key=session_entry.session_key,
                session_id="fresh-goal-session",
                created_at=session_entry.created_at,
                updated_at=session_entry.updated_at,
                platform=Platform.TELEGRAM,
                chat_type="dm",
            )

        def get_or_create_session(self, source, force_new=False):
            assert force_new is True
            return self.new_entry

        def switch_session(self, session_key, target_session_id):
            return self.new_entry

    runner.session_store = _Store()
    state = GoalManager(session_entry.session_id, default_max_turns=2).set(
        "continue across a fresh root",
        max_turns=2,
    )
    state.turns_used = 2
    state.checkpoint = "first slice completed; inspect the repository next"
    goals.save_goal(session_entry.session_id, state)

    child = await runner._rollover_goal_at_fifo_head(
        old_session_id=session_entry.session_id,
        session_key=session_entry.session_key,
        source=src,
    )
    assert child is not None
    migrated = load_goal("fresh-goal-session")
    assert migrated is not None
    assert migrated.status == "active"
    assert migrated.turns_used == 0
    assert migrated.rollovers_used == 1
    assert migrated.checkpoint.startswith("first slice completed")


@pytest.mark.asyncio
async def test_top_level_rollover_marker_migrates_before_agent_turn(hermes_home):
    """A FIFO marker drained as a fresh event must rotate before agent startup."""
    runner, _adapter, session_entry, src = _make_runner_with_adapter()
    fresh_entry = SessionEntry(
        session_key=session_entry.session_key,
        session_id="fresh-goal-session",
        created_at=session_entry.created_at,
        updated_at=session_entry.updated_at,
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner._rollover_goal_at_fifo_head = AsyncMock(return_value=fresh_entry)
    from gateway.run import MessageEvent, MessageType

    marker = MessageEvent(
        text="Continue the active goal from its checkpoint.",
        message_type=MessageType.TEXT,
        source=src,
        metadata={
            "goal_session_rollover": True,
            "goal_rollover_from_session_id": session_entry.session_id,
        },
    )

    entry = await runner._consume_goal_rollover_marker(
        marker,
        session_entry=session_entry,
        source=src,
    )

    assert entry is fresh_entry
    runner._rollover_goal_at_fifo_head.assert_awaited_once_with(
        old_session_id=session_entry.session_id,
        session_key=session_entry.session_key,
        source=src,
    )


@pytest.mark.asyncio
async def test_goal_rollover_marker_rejects_a_stale_root(hermes_home):
    """A marker must never revive a goal after the session has moved on."""
    runner, _adapter, session_entry, src = _make_runner_with_adapter()
    runner._rollover_goal_at_fifo_head = AsyncMock()
    from gateway.run import MessageEvent, MessageType

    marker = MessageEvent(
        text="Continue the active goal from its checkpoint.",
        message_type=MessageType.TEXT,
        source=src,
        metadata={
            "goal_session_rollover": True,
            "goal_rollover_from_session_id": "superseded-session",
        },
    )

    entry = await runner._consume_goal_rollover_marker(
        marker,
        session_entry=session_entry,
        source=src,
    )

    assert entry is None
    runner._rollover_goal_at_fifo_head.assert_not_awaited()


@pytest.mark.asyncio
async def test_topic_binding_failure_rolls_back_before_goal_migration(hermes_home):
    runner, _adapter, session_entry, _src = _make_runner_with_adapter()
    from hermes_cli.goals import GoalManager, load_goal

    topic_source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="group",
        thread_id="topic-1",
    )

    class _Store:
        def __init__(self):
            self.new_entry = SessionEntry(
                session_key=session_entry.session_key,
                session_id="topic-fresh-session",
                created_at=session_entry.created_at,
                updated_at=session_entry.updated_at,
                platform=Platform.TELEGRAM,
                chat_type="group",
            )
            self.switches = []

        def get_or_create_session(self, source, force_new=False):
            assert force_new is True
            return self.new_entry

        def switch_session(self, session_key, target_session_id):
            self.switches.append((session_key, target_session_id))
            return session_entry

    runner.session_store = _Store()
    runner._sync_telegram_topic_binding = MagicMock(
        side_effect=[RuntimeError("binding unavailable"), None],
    )
    GoalManager(session_entry.session_id).set("do not orphan topic goal")

    result = await runner._rollover_goal_at_fifo_head(
        old_session_id=session_entry.session_id,
        session_key=session_entry.session_key,
        source=topic_source,
    )
    assert result is None
    assert load_goal(session_entry.session_id).status == "active"
    assert load_goal("topic-fresh-session") is None
    assert runner.session_store.switches == [
        (session_entry.session_key, session_entry.session_id)
    ]
    assert runner._sync_telegram_topic_binding.call_count == 2


@pytest.mark.asyncio
async def test_goal_verdict_skipped_when_no_active_goal(hermes_home):
    """No goal set → the hook is a no-op. Nothing is sent, nothing enqueued."""
    runner, adapter, session_entry, src = _make_runner_with_adapter()

    await runner._post_turn_goal_continuation(
        session_entry=session_entry,
        source=src,
        final_response="anything",
    )
    await asyncio.sleep(0.05)

    assert adapter.sends == []
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_goal_verdict_survives_adapter_without_send(hermes_home):
    """Bad adapter (no ``send`` attribute) must not crash the judge hook."""
    runner, _adapter, session_entry, src = _make_runner_with_adapter()

    from hermes_cli.goals import GoalManager

    GoalManager(session_entry.session_id).set("survive missing send")

    class _NoSendAdapter:
        def __init__(self):
            self._pending_messages: dict = {}

    runner.adapters[Platform.TELEGRAM] = _NoSendAdapter()

    with patch(
        "hermes_cli.goals.judge_goal", return_value=("done", "ok", False, None, False)
    ):
        # must not raise
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="whatever",
        )
        await asyncio.sleep(0.05)
