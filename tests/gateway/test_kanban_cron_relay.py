from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading
from unittest.mock import patch

from cron.jobs import get_job, load_jobs, update_job
from gateway.kanban_cron_relay import (
    RELAY_SCRIPT_NAME,
    RelayReconcileResult,
    progress_relay_key,
    reconcile_kanban_progress_crons,
    relay_script_main,
    resolve_progress_cron_settings,
)
from hermes_constants import get_hermes_home
from hermes_cli import kanban_db as kb


def _running_subscription(tmp_path, monkeypatch, *, profile="maker"):
    db_path = tmp_path / "relay.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_PROFILE", profile)
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Publicar vídeo de Instagram", assignee=profile)
        assert kb.claim_task(conn, task_id)
        task = kb.get_task(conn, task_id)
        assert task is not None and task.current_run_id is not None
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="5049627574",
            thread_id="77",
            user_id="5049627574",
            notifier_profile=profile,
        )
        assert kb.record_worker_progress(
            conn,
            task_id,
            text="Terminado: auditoría visual. Ahora: validando la entrega.",
            expected_run_id=task.current_run_id,
        ) == "recorded"
    finally:
        conn.close()
    return task_id


def test_resolve_progress_cron_settings_is_opt_in_and_bounded():
    assert resolve_progress_cron_settings({}) == (False, 5)
    assert resolve_progress_cron_settings(
        {"kanban": {"progress_delivery": "cron_no_agent"}}
    ) == (True, 5)
    assert resolve_progress_cron_settings(
        {
            "kanban": {
                "progress_delivery": "cron_no_agent",
                "progress_cron_interval_minutes": 0,
            }
        }
    ) == (True, 1)
    assert resolve_progress_cron_settings(
        {
            "kanban": {
                "progress_delivery": "cron_no_agent",
                "progress_cron_interval_minutes": 999,
            }
        }
    ) == (True, 60)


def test_reconcile_creates_exactly_one_profile_owned_no_agent_cron(
    tmp_path, monkeypatch
):
    task_id = _running_subscription(tmp_path, monkeypatch)

    result = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    expected_key = progress_relay_key(
        board=kb.DEFAULT_BOARD,
        task_id=task_id,
        platform="telegram",
        chat_id="5049627574",
        thread_id="77",
        notifier_profile="maker",
    )
    assert result == RelayReconcileResult(ready_keys=frozenset({expected_key}))

    jobs = load_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["no_agent"] is True
    assert job["script"] == RELAY_SCRIPT_NAME
    assert job["schedule"]["kind"] == "interval"
    assert job["schedule"]["minutes"] == 5
    assert job["deliver"] == "origin"
    assert job["origin"] == {
        "platform": "telegram",
        "chat_id": "5049627574",
        "thread_id": "77",
        "user_id": "5049627574",
        "profile": "maker",
    }
    assert job["skills"] == []
    assert job["model"] is None
    assert job["provider"] is None
    assert job["wrap_response"] is False
    assert expected_key in job["prompt"]

    script = get_hermes_home() / "scripts" / RELAY_SCRIPT_NAME
    assert script.is_file()
    assert script.stat().st_mode & 0o777 == 0o600

    # Reconciliation is idempotent: no second job is created.
    assert reconcile_kanban_progress_crons(
        "maker", interval_minutes=5
    ) == RelayReconcileResult(ready_keys=frozenset({expected_key}))
    assert len(load_jobs()) == 1

    # Exercise the actual scheduler subprocess path, not only the in-process
    # renderer. The generated profile script must import the installed runtime,
    # receive the opaque job id, and return clean stdout with no agent involved.
    from cron.scheduler import _run_job_script

    success, output = _run_job_script(RELAY_SCRIPT_NAME, job_id=job["id"])
    assert success is True, output
    assert "Kanban en curso" in output
    assert "Publicar vídeo de Instagram" in output
    assert task_id not in output
    assert job["id"] not in output


def test_reconcile_pauses_orphan_after_unsubscribe(tmp_path, monkeypatch):
    task_id = _running_subscription(tmp_path, monkeypatch)
    result = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert len(result.ready_keys) == 1
    job_id = load_jobs()[0]["id"]

    conn = kb.connect()
    try:
        assert kb.remove_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="5049627574",
            thread_id="77",
        )
    finally:
        conn.close()

    assert reconcile_kanban_progress_crons(
        "maker", interval_minutes=5
    ) == RelayReconcileResult()
    job = get_job(job_id)
    assert job is not None
    assert job["enabled"] is False
    assert job["state"] == "paused"
    assert job["paused_reason"] == "Kanban subscription ended"


def test_switching_back_to_direct_mode_pauses_existing_relay(tmp_path, monkeypatch):
    _running_subscription(tmp_path, monkeypatch)
    created = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert len(created.ready_keys) == 1
    job_id = load_jobs()[0]["id"]

    assert reconcile_kanban_progress_crons(
        "maker", interval_minutes=5, enabled=False
    ) == RelayReconcileResult()
    stopped = get_job(job_id)
    assert stopped is not None
    assert stopped["enabled"] is False
    assert stopped["state"] == "paused"
    assert stopped["paused_reason"] == "Kanban cron delivery disabled"


def test_unmanaged_script_and_stale_origin_are_never_treated_as_ready(
    tmp_path, monkeypatch
):
    _running_subscription(tmp_path, monkeypatch)
    created = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert len(created.ready_keys) == 1
    job = load_jobs()[0]
    update_job(
        job["id"],
        {"origin": {"platform": "telegram", "chat_id": "wrong-chat"}},
    )
    script = get_hermes_home() / "scripts" / RELAY_SCRIPT_NAME
    script.write_text("print('unmanaged')\n", encoding="utf-8")

    result = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert result == RelayReconcileResult()
    stopped = get_job(job["id"])
    assert stopped is not None
    assert stopped["enabled"] is False
    assert stopped["state"] == "paused"
    assert stopped["origin"]["chat_id"] == "wrong-chat"


def test_no_agent_flag_mutation_is_silenced_and_repaired_without_duplicate(
    tmp_path, monkeypatch
):
    _running_subscription(tmp_path, monkeypatch)
    created = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert len(created.ready_keys) == 1
    job = load_jobs()[0]
    update_job(job["id"], {"no_agent": False})

    # The same script is the wake-gate precheck on an accidentally agent-enabled
    # job. Run the real scheduler path and prove it exits before constructing an
    # agent or invoking a model.
    from cron.scheduler import SILENT_MARKER, run_job

    mutated = get_job(job["id"])
    assert mutated is not None
    with patch("run_agent.AIAgent") as agent_cls:
        success, _, final, error = run_job(mutated)
    assert success is True
    assert final == SILENT_MARKER
    assert error is None
    agent_cls.assert_not_called()
    stopped = get_job(job["id"])
    assert stopped is not None
    assert stopped["enabled"] is False

    repaired = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert len(repaired.ready_keys) == 1
    jobs = load_jobs()
    assert len(jobs) == 1
    assert jobs[0]["id"] == job["id"]
    assert jobs[0]["no_agent"] is True
    assert jobs[0]["enabled"] is True


def test_malformed_managed_metadata_is_quarantined_before_safe_replacement(
    tmp_path, monkeypatch
):
    _running_subscription(tmp_path, monkeypatch)
    created = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert len(created.ready_keys) == 1
    old = load_jobs()[0]
    malformed = json.loads(old["prompt"])
    malformed.pop("task_id")
    update_job(
        old["id"],
        {
            "prompt": json.dumps(malformed),
            "no_agent": False,
        },
    )

    repaired = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert len(repaired.ready_keys) == 1
    jobs = load_jobs()
    assert len(jobs) == 2
    old_after = next(job for job in jobs if job["id"] == old["id"])
    replacement = next(job for job in jobs if job["id"] != old["id"])
    assert old_after["enabled"] is False
    assert old_after["state"] == "paused"
    assert replacement["enabled"] is True
    assert replacement["no_agent"] is True
    assert replacement["id"] != old_after["id"]


def test_concurrent_reconcile_cannot_create_duplicate_jobs(tmp_path, monkeypatch):
    _running_subscription(tmp_path, monkeypatch)
    import gateway.kanban_cron_relay as relay_module

    real_install = relay_module._ensure_relay_script
    entered = threading.Event()
    release = threading.Event()

    def slow_install():
        entered.set()
        assert release.wait(timeout=5)
        return real_install()

    monkeypatch.setattr(relay_module, "_ensure_relay_script", slow_install)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(reconcile_kanban_progress_crons, "maker", interval_minutes=5)
        assert entered.wait(timeout=5)
        second = reconcile_kanban_progress_crons("maker", interval_minutes=5)
        release.set()
        first_result = first.result(timeout=10)

    assert len(load_jobs()) == 1
    assert len(first_result.ready_keys) == 1
    assert second.ready_keys == frozenset()
    assert second.deferred_keys == first_result.ready_keys


def test_relay_renders_progress_then_confirms_final_delivery_before_pause(
    tmp_path, monkeypatch, capsys
):
    task_id = _running_subscription(tmp_path, monkeypatch)
    reconcile_kanban_progress_crons("maker", interval_minutes=5)
    job = load_jobs()[0]
    monkeypatch.setenv("HERMES_CRON_JOB_ID", job["id"])

    assert relay_script_main() == 0
    progress = capsys.readouterr().out
    assert "Kanban en curso" in progress
    assert "Publicar vídeo de Instagram" in progress
    assert "Ahora: validando la entrega" in progress
    assert task_id not in progress
    assert job["id"] not in progress

    conn = kb.connect()
    try:
        assert kb.block_task(conn, task_id, reason="Falta aprobación del owner")
    finally:
        conn.close()

    # Blocked is reportable but recoverable: keep both relay and subscription.
    assert relay_script_main() == 0
    blocked = capsys.readouterr().out
    assert "Kanban bloqueado" in blocked
    assert "Falta aprobación del owner" in blocked
    still_running = get_job(job["id"])
    assert still_running is not None
    assert still_running["enabled"] is True
    conn = kb.connect()
    try:
        assert len(kb.list_notify_subs(conn, task_id)) == 1
        assert kb.unblock_task(conn, task_id)
        claimed = kb.claim_task(conn, task_id, claimer="worker-2")
        assert claimed is not None
        assert kb.complete_task(conn, task_id, summary="Entrega aprobada")
    finally:
        conn.close()

    # The first truly terminal tick prints the final report but stays enabled;
    # delivery happens only after the script returns.
    assert relay_script_main() == 0
    final = capsys.readouterr().out
    assert "Kanban completado" in final
    assert "Entrega aprobada" in final

    # Simulate the scheduler recording successful delivery for that terminal
    # tick. The next silent tick confirms delivery and self-pauses, but the
    # notifier still owns terminal bookkeeping and subscription cleanup.
    update_job(
        job["id"],
        {
            "last_run_at": (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(),
            "last_status": "ok",
            "last_delivery_error": None,
        },
    )
    assert relay_script_main() == 0
    assert capsys.readouterr().out == ""
    stopped = get_job(job["id"])
    assert stopped is not None
    assert stopped["enabled"] is False
    assert stopped["state"] == "paused"
    assert stopped["paused_reason"] == "Kanban final update delivered"
    conn = kb.connect()
    try:
        assert len(kb.list_notify_subs(conn, task_id)) == 1
    finally:
        conn.close()
    confirmed = reconcile_kanban_progress_crons("maker", interval_minutes=5)
    assert confirmed.ready_keys == frozenset()
    assert confirmed.terminal_confirmed_keys == frozenset(
        {json.loads(job["prompt"])["relay_key"]}
    )
