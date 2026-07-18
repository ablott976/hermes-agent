from __future__ import annotations

import importlib
import json
import stat
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def contract_env(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    home = root / "profiles" / "dev-owner"
    (home / "cron").mkdir(parents=True)
    (home / "scripts").mkdir()
    workdir = tmp_path / "project"
    plans = workdir / ".hermes" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "implementation.md"
    state = plans / "state.json"
    plan.write_text("# Plan\n")
    state.write_text("{}\n")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "dev-owner")

    import hermes_constants
    import cron.jobs

    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)

    yield {
        "home": home,
        "workdir": workdir,
        "plan": plan,
        "state": state,
        "jobs": cron.jobs,
    }

    monkeypatch.undo()
    importlib.reload(hermes_constants)
    importlib.reload(cron.jobs)


def _valid_prompt(env) -> str:
    return f"""Continue the implementation.
Plan: `{env['plan']}`
State: `{env['state']}`
No recursive crons may be created or scheduled.
PAUSE_TECHNICAL_VALIDATION_V1
When complete, execute `hermes --profile {{{{OWNER_PROFILE}}}} cron pause {{{{JOB_ID}}}}`.
Keep `.hermes/plans`, state, funciones.txt, and notes/scratch local-only; never stage, commit, or push them.
"""


def _valid_create_kwargs(env) -> dict:
    return {
        "prompt": _valid_prompt(env),
        "schedule": "every 1m",
        "session_mode": "persistent",
        "skills": [],
        "enabled_toolsets": ["terminal", "file", "skills"],
        "workdir": str(env["workdir"]),
        "deliver": "local",
    }


def _raw_store(env) -> dict:
    return json.loads((env["home"] / "cron" / "jobs.json").read_text())


def test_valid_persistent_create_resolves_placeholders_and_schedules_once(contract_env):
    jobs = contract_env["jobs"]

    job = jobs.create_job(**_valid_create_kwargs(contract_env))

    assert job["enabled"] is True
    assert job["state"] == "scheduled"
    assert job["next_run_at"]
    assert job["validation"]["status"] == "valid"
    assert job["validation"]["codes"] == []
    assert "{{JOB_ID}}" not in job["prompt"]
    assert "{{OWNER_PROFILE}}" not in job["prompt"]
    assert f"hermes --profile dev-owner cron pause {job['id']}" in job["prompt"]
    raw_jobs = _raw_store(contract_env)["jobs"]
    assert [stored["id"] for stored in raw_jobs] == [job["id"]]
    registry_path = contract_env["home"] / "cron" / "creation_contract.json"
    registry = json.loads(registry_path.read_text())
    assert registry["job_kinds"] == {job["id"]: "persistent_development"}
    assert registry["job_statuses"] == {job["id"]: "valid"}
    assert stat.S_IMODE(registry_path.stat().st_mode) == 0o600


def test_invalid_persistent_create_returns_real_paused_draft_with_exact_codes(contract_env):
    jobs = contract_env["jobs"]

    job = jobs.create_job(
        prompt="Do the work.",
        schedule="every 5m",
        session_mode="persistent",
        skills=["plan"],
        enabled_toolsets=["cronjob"],
        deliver="local",
    )

    assert len(job["id"]) == 12
    assert job["enabled"] is False
    assert job["state"] == "paused"
    assert job["next_run_at"] is None
    assert job["validation"]["status"] == "invalid_draft"
    assert job["validation"]["codes"] == [
        "E_SCHEDULE_NOT_1M",
        "E_WORKDIR_NOT_ABSOLUTE",
        "E_SKILLS_NOT_EMPTY",
        "E_TOOLSET_MISSING",
        "E_TOOLSET_FORBIDDEN",
        "E_PLAN_PATH_INVALID",
        "E_STATE_PATH_INVALID",
        "E_NO_RECURSION_CLAUSE_MISSING",
        "E_LOCAL_ARTIFACT_PROTECTION_MISSING",
        "E_SELF_PAUSE_MISSING",
        "E_PAUSE_TOKEN_MISSING",
    ]
    grace = datetime.fromisoformat(job["validation"]["grace_until"])
    validated = datetime.fromisoformat(job["validation"]["validated_at"])
    assert 0 < (grace - validated).total_seconds() <= 1800


def test_tool_and_cli_create_surface_the_same_core_validation(contract_env, monkeypatch, capsys):
    from tools.cronjob_tools import cronjob

    tool_result = json.loads(cronjob(action="create", **_valid_create_kwargs(contract_env)))
    assert tool_result["success"] is True
    assert tool_result["job"]["validation"]["status"] == "valid"

    import hermes_cli.cron as cli_cron

    monkeypatch.setattr(cli_cron, "_warn_if_gateway_not_running", lambda: None)
    args = SimpleNamespace(
        schedule="every 1m",
        prompt=_valid_prompt(contract_env),
        name="CLI contract",
        deliver="local",
        repeat=None,
        skill=None,
        skills=[],
        script=None,
        workdir=str(contract_env["workdir"]),
        no_agent=False,
        session_mode="persistent",
        enabled_toolsets=["terminal", "file", "skills"],
    )
    assert cli_cron.cron_create(args) == 0
    output = capsys.readouterr().out
    assert "Created job:" in output
    created = contract_env["jobs"].list_jobs(include_disabled=True)
    cli_job = next(job for job in created if job["name"] == "CLI contract")
    assert cli_job["validation"]["status"] == tool_result["job"]["validation"]["status"]


def test_invalid_draft_cannot_resume_trigger_or_run_until_explicitly_repaired(contract_env):
    jobs = contract_env["jobs"]
    draft = jobs.create_job(
        prompt="Incomplete.",
        schedule="every 5m",
        session_mode="persistent",
        deliver="local",
    )

    with pytest.raises(ValueError, match="E_SCHEDULE_NOT_1M"):
        jobs.resume_job(draft["id"])
    with pytest.raises(ValueError, match="E_SCHEDULE_NOT_1M"):
        jobs.trigger_job(draft["id"])

    from tools.cronjob_tools import cronjob

    run_result = json.loads(cronjob(action="run", job_id=draft["id"]))
    assert run_result["success"] is True
    assert run_result["job"]["executed"] is False
    assert "E_SCHEDULE_NOT_1M" in run_result["job"]["execution_skipped"]

    repaired = jobs.update_job(
        draft["id"],
        {
            "prompt": _valid_prompt(contract_env),
            "schedule": "every 1m",
            "skills": [],
            "enabled_toolsets": ["terminal", "file", "skills"],
            "workdir": str(contract_env["workdir"]),
        },
    )
    assert repaired["validation"]["status"] == "valid_draft"
    assert repaired["enabled"] is False
    assert repaired["next_run_at"] is None

    resumed = jobs.resume_job(draft["id"])
    assert resumed["validation"]["status"] == "valid"
    assert resumed["enabled"] is True
    assert resumed["state"] == "scheduled"
    assert resumed["next_run_at"]
    assert len(_raw_store(contract_env)["jobs"]) == 1


def test_lifecycle_guard_remains_active_on_create_update_and_activation(contract_env):
    jobs = contract_env["jobs"]
    from cron.lifecycle_guard import GatewayLifecycleBlocked

    with pytest.raises(GatewayLifecycleBlocked):
        jobs.create_job(
            prompt="Run hermes gateway restart.",
            schedule="every 5m",
            deliver="local",
        )

    job = jobs.create_job(prompt="Safe report.", schedule="every 5m", deliver="local")
    with pytest.raises(GatewayLifecycleBlocked):
        jobs.update_job(job["id"], {"prompt": "Run hermes gateway stop."})

    path = contract_env["home"] / "cron" / "jobs.json"
    payload = json.loads(path.read_text())
    payload["jobs"][0]["prompt"] = "Run hermes gateway restart."
    path.write_text(json.dumps(payload))
    assert jobs.claim_job_for_fire(job["id"]) is False


def test_direct_status_tamper_cannot_activate_repaired_draft(contract_env):
    jobs = contract_env["jobs"]
    draft = jobs.create_job(
        prompt="Incomplete.",
        schedule="every 5m",
        session_mode="persistent",
        deliver="local",
    )
    repaired = jobs.update_job(
        draft["id"],
        {
            "prompt": _valid_prompt(contract_env),
            "schedule": "every 1m",
            "skills": [],
            "enabled_toolsets": ["terminal", "file", "skills"],
            "workdir": str(contract_env["workdir"]),
        },
    )
    assert repaired["validation"]["status"] == "valid_draft"

    path = contract_env["home"] / "cron" / "jobs.json"
    payload = json.loads(path.read_text())
    payload["jobs"][0]["validation"]["status"] = "valid"
    payload["jobs"][0]["enabled"] = True
    payload["jobs"][0]["state"] = "scheduled"
    payload["jobs"][0]["next_run_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload, indent=2))
    before = path.read_bytes()

    view = jobs.get_job(draft["id"])
    assert view["validation"]["status"] == "drift"
    assert "E_VALIDATION_STATUS_DRIFT" in view["validation"]["codes"]
    assert jobs.get_due_jobs() == []
    assert jobs.claim_job_for_fire(draft["id"]) is False
    assert path.read_bytes() == before

    resumed = jobs.resume_job(draft["id"])
    assert resumed["validation"]["status"] == "valid"
    assert resumed["enabled"] is True


def test_scheduler_execution_guard_blocks_drift_before_dispatch(contract_env, monkeypatch):
    jobs = contract_env["jobs"]
    job = jobs.create_job(
        prompt=_valid_prompt(contract_env),
        schedule="every 1m",
        session_mode="persistent",
        skills=[],
        enabled_toolsets=["terminal", "file", "skills"],
        workdir=str(contract_env["workdir"]),
        deliver="local",
    )
    path = contract_env["home"] / "cron" / "jobs.json"
    payload = json.loads(path.read_text())
    payload["jobs"][0]["prompt"] = "Drifted after validation."
    path.write_text(json.dumps(payload, indent=2))

    import cron.scheduler as scheduler

    def unexpected_dispatch(*args, **kwargs):
        pytest.fail("dispatch must not run for validation drift")

    monkeypatch.setattr(scheduler, "claim_dispatch", unexpected_dispatch)
    assert scheduler.run_one_job(payload["jobs"][0]) is False
    assert jobs.get_job(job["id"]).get("run_count", 0) == 0


@pytest.mark.parametrize("missing_registry_field", ["job_kinds", "job_statuses"])
def test_incomplete_registry_blocks_every_execution_gate(
    contract_env,
    monkeypatch,
    missing_registry_field,
):
    jobs = contract_env["jobs"]
    if missing_registry_field == "job_statuses":
        job = jobs.create_job(
            prompt="Incomplete.",
            schedule="every 5m",
            session_mode="persistent",
            deliver="local",
        )
        job = jobs.update_job(
            job["id"],
            {
                "prompt": _valid_prompt(contract_env),
                "schedule": "every 1m",
                "skills": [],
                "enabled_toolsets": ["terminal", "file", "skills"],
                "workdir": str(contract_env["workdir"]),
            },
        )
        assert job["validation"]["status"] == "valid_draft"
    else:
        job = jobs.create_job(**_valid_create_kwargs(contract_env))

    registry_path = contract_env["home"] / "cron" / "creation_contract.json"
    registry = json.loads(registry_path.read_text())
    registry[missing_registry_field].pop(job["id"])
    registry_path.write_text(json.dumps(registry, indent=2))

    path = contract_env["home"] / "cron" / "jobs.json"
    payload = json.loads(path.read_text())
    raw = payload["jobs"][0]
    if missing_registry_field == "job_statuses":
        raw["validation"]["status"] = "valid"
        raw["enabled"] = True
        raw["state"] = "scheduled"
    else:
        raw["session_mode"] = "fresh"
        raw["validation"]["contract_kind"] = "fresh"
    raw["next_run_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload, indent=2))
    before = path.read_bytes()

    view = jobs.get_job(job["id"])
    assert "E_CREATION_REGISTRY_INCOMPLETE" in view["validation"]["codes"]
    assert jobs.get_due_jobs() == []
    assert jobs.claim_job_for_fire(job["id"]) is False

    from tools.cronjob_tools import _execute_job_now

    manual = _execute_job_now(raw)
    assert manual["success"] is False
    assert "E_CREATION_REGISTRY_INCOMPLETE" in manual["error"]

    import cron.scheduler as scheduler

    def unexpected_dispatch(*args, **kwargs):
        pytest.fail("dispatch must not run with an incomplete registry")

    monkeypatch.setattr(scheduler, "claim_dispatch", unexpected_dispatch)
    assert scheduler.run_one_job(raw) is False
    assert path.read_bytes() == before


def test_legacy_job_is_logically_exempt_without_read_or_unrelated_update_demotion(contract_env):
    jobs = contract_env["jobs"]
    path = contract_env["home"] / "cron" / "jobs.json"
    legacy = {
        "id": "legacy123456",
        "name": "Legacy",
        "prompt": "Continue legacy work.",
        "skills": [],
        "skill": None,
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "schedule_display": "every 5m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "session_mode": "persistent",
        "next_run_at": "2099-01-01T00:00:00+00:00",
    }
    path.write_text(json.dumps({"jobs": [legacy]}, indent=2))
    before = path.read_bytes()

    view = jobs.get_job(legacy["id"])
    assert view["validation"]["status"] == "legacy_exempt"
    assert path.read_bytes() == before

    updated = jobs.update_job(legacy["id"], {"name": "Legacy renamed"})
    assert updated["enabled"] is True
    raw = _raw_store(contract_env)["jobs"][0]
    assert raw["name"] == "Legacy renamed"
    assert "validation" not in raw


def test_direct_json_drift_is_surfaced_and_never_mutated_by_scheduler(contract_env):
    jobs = contract_env["jobs"]
    job = jobs.create_job(**_valid_create_kwargs(contract_env))
    path = contract_env["home"] / "cron" / "jobs.json"
    payload = json.loads(path.read_text())
    payload["jobs"][0]["prompt"] = "Drifted prompt without contract."
    payload["jobs"][0]["session_mode"] = "fresh"
    payload["jobs"][0].pop("validation")
    payload["jobs"][0]["next_run_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload, indent=2))
    before = path.read_bytes()

    view = jobs.get_job(job["id"])
    assert view["validation"]["status"] == "drift"
    assert "E_CONTRACT_KIND_DRIFT" in view["validation"]["codes"]
    assert jobs.get_due_jobs() == []
    assert jobs.claim_job_for_fire(job["id"]) is False
    with pytest.raises(ValueError, match="E_CONTRACT_KIND_DRIFT"):
        jobs.trigger_job(job["id"])
    assert path.read_bytes() == before


def test_no_agent_requires_readable_script_and_rejects_model_or_persistent_modes(contract_env):
    jobs = contract_env["jobs"]

    with pytest.raises(ValueError, match="no_agent=True requires a script"):
        jobs.create_job(prompt=None, schedule="every 5m", no_agent=True, deliver="local")
    with pytest.raises(ValueError, match="E_SCRIPT_NOT_READABLE"):
        jobs.create_job(
            prompt=None,
            schedule="every 5m",
            script="missing.sh",
            no_agent=True,
            deliver="local",
        )

    script = contract_env["home"] / "scripts" / "watchdog.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(ValueError, match="E_NO_AGENT_MODEL_CONFIG"):
        jobs.create_job(
            prompt=None,
            schedule="every 5m",
            script="watchdog.sh",
            no_agent=True,
            model="should-not-run",
            deliver="local",
        )

    with pytest.raises(ValueError, match="E_NO_AGENT_PERSISTENT"):
        jobs.create_job(
            prompt=None,
            schedule="every 1m",
            script="watchdog.sh",
            no_agent=True,
            session_mode="persistent",
            deliver="local",
        )


def test_no_agent_rejects_traversal_and_absolute_scripts_outside_sandbox(contract_env):
    jobs = contract_env["jobs"]
    outside = contract_env["home"] / "outside.py"
    outside.write_text("print('outside')\n")

    for script in ("../outside.py", str(outside)):
        with pytest.raises(ValueError, match="E_SCRIPT_OUTSIDE_SANDBOX"):
            jobs.create_job(
                prompt=None,
                schedule="every 5m",
                script=script,
                no_agent=True,
                deliver="local",
            )

    assert jobs.list_jobs(include_disabled=True) == []


@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks require elevated privileges on Windows")
def test_no_agent_rejects_script_symlink_that_escapes_sandbox(contract_env):
    jobs = contract_env["jobs"]
    outside = contract_env["home"] / "outside.py"
    outside.write_text("print('outside')\n")
    (contract_env["home"] / "scripts" / "escape.py").symlink_to(outside)

    with pytest.raises(ValueError, match="E_SCRIPT_OUTSIDE_SANDBOX"):
        jobs.create_job(
            prompt=None,
            schedule="every 5m",
            script="escape.py",
            no_agent=True,
            deliver="local",
        )

    assert jobs.list_jobs(include_disabled=True) == []
