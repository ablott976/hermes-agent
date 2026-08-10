from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_evaluate_after_turn_auto_rollover_keeps_active_checkpoint(hermes_home):
    from hermes_cli import goals
    from hermes_cli.goals import GoalManager

    mgr = GoalManager(session_id="eval-rollover", default_max_turns=2)
    mgr.set("durable goal")
    with patch.object(
        goals,
        "judge_goal",
        return_value=("continue", "not yet", False, None, False),
    ):
        assert mgr.evaluate_after_turn("first checkpoint", auto_rollover=True)[
            "should_continue"
        ]
        decision = mgr.evaluate_after_turn("second checkpoint", auto_rollover=True)

    assert decision["should_continue"] is False
    assert decision["should_rollover"] is True
    assert decision["continuation_prompt"] is not None
    assert "fresh bounded slice" in decision["continuation_prompt"]
    assert "second checkpoint" not in decision["continuation_prompt"]
    assert mgr.state.status == "active"
    assert mgr.state.checkpoint == "second checkpoint"


def test_rollover_migration_resets_turn_budget_and_preserves_checkpoint(hermes_home):
    from hermes_cli.goals import (
        GoalState,
        load_goal,
        migrate_goal_to_session,
        save_goal,
    )

    state = GoalState(
        goal="ship safely",
        turns_used=20,
        max_turns=20,
        checkpoint="tests passed; inspect PR next",
        checkpoint_fingerprint="abc",
        repeated_checkpoint_count=1,
    )
    save_goal("roll-parent", state)
    assert migrate_goal_to_session(
        "roll-parent",
        "roll-child",
        reason="budget",
        reset_turn_budget=True,
    )
    child = load_goal("roll-child")
    assert child is not None
    assert child.turns_used == 0
    assert child.rollovers_used == 1
    assert child.checkpoint == "tests passed; inspect PR next"
    assert child.checkpoint_fingerprint == "abc"
