import json

from hermes_cli.cli_commands_mixin import CLICommandsMixin


def test_cron_slash_create_forwards_persistent_session_mode(monkeypatch, capsys):
    captured = {}

    def fake_cronjob(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "success": True,
                "job_id": "job-1",
                "schedule": "every 1m",
                "next_run_at": "soon",
                "skills": [],
            }
        )

    monkeypatch.setattr("tools.cronjob_tools.cronjob", fake_cronjob)

    CLICommandsMixin()._handle_cron_command(
        '/cron add "every 1m" "Continue the plan" --session-mode persistent'
    )

    assert captured["action"] == "create"
    assert captured["session_mode"] == "persistent"
    assert "Created job" in capsys.readouterr().out


def test_cron_slash_rejects_unknown_session_mode(monkeypatch, capsys):
    called = False

    def fake_cronjob(**_kwargs):
        nonlocal called
        called = True
        return json.dumps({"success": True})

    monkeypatch.setattr("tools.cronjob_tools.cronjob", fake_cronjob)

    CLICommandsMixin()._handle_cron_command(
        '/cron add "every 1m" "Continue" --session-mode endless'
    )

    assert called is False
    assert "must be fresh or persistent" in capsys.readouterr().out
