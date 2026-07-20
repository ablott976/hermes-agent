"""Temporary no-agent cron relays for user-visible Kanban progress.

The gateway remains the owner of notification subscriptions.  When the
``cron_no_agent`` delivery mode is enabled, this module projects each owned
subscription into one profile-local cron job.  The job executes a small script
inside the existing cron sandbox and therefore never constructs an agent or
spends model tokens.
"""

from __future__ import annotations

import hashlib
import json

import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from agent.progress_copy import (
    DEFAULT_HUMAN_PROGRESS_TEXT,
    DEFAULT_HUMAN_PROGRESS_TITLE,
    sanitize_human_progress_text,
)
from hermes_constants import get_hermes_home

RELAY_SCRIPT_NAME = "hermes-kanban-progress-relay-v1.py"
_RELAY_MARKER = "hermes_kanban_progress_relay_v1"
_RELAY_VERSION = 1
_RELAY_LOCK_NAME = ".kanban-progress-relay.lock"
_RELAY_STATE_DIR = "kanban-progress-relays"
_INVALID_RELAY_BUCKET = "__invalid_managed_relays__"
_TERMINAL_STATUSES = frozenset({"done", "archived", "blocked"})
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TASK_RE = re.compile(r"^t_[0-9a-f]+$")
_JOB_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SILENT_WAKE_GATE = '{"wakeAgent":false}'


@dataclass(frozen=True)
class RelayReconcileResult:
    """Transport ownership decided by one reconciliation attempt.

    ``ready_keys`` have a fully verified active cron and may suppress direct
    delivery. ``deferred_keys`` are in a concurrent create/update window; the
    notifier skips only that tick so it cannot race the first relay delivery.
    """

    ready_keys: frozenset[str] = frozenset()
    deferred_keys: frozenset[str] = frozenset()



def _relay_script_source() -> str:
    # Cron intentionally executes from the profile scripts directory. Pin the
    # managed shim to the runtime tree that installed it so source checkouts and
    # non-editable deployments can both import the shared implementation without
    # widening PYTHONPATH for every unrelated cron script.
    runtime_root = str(Path(__file__).resolve().parent.parent)
    return f'''#!/usr/bin/env python3
# Hermes-managed Kanban no-agent relay v1. Do not edit this generated shim.
import sys

sys.path.insert(0, {runtime_root!r})

from gateway.kanban_cron_relay import relay_script_main

raise SystemExit(relay_script_main())
'''


def resolve_progress_cron_settings(config: Any) -> tuple[bool, int]:
    """Return ``(enabled, interval_minutes)`` with safe defaults and bounds."""
    kanban_cfg = config.get("kanban", {}) if isinstance(config, dict) else {}
    mode = str(kanban_cfg.get("progress_delivery") or "direct").strip().lower()
    enabled = mode == "cron_no_agent"
    raw_interval = kanban_cfg.get("progress_cron_interval_minutes", 5)
    try:
        interval = int(float(raw_interval))
    except (TypeError, ValueError, OverflowError):
        interval = 5
    return enabled, min(60, max(1, interval))


def progress_relay_key(
    *,
    board: str,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: str,
    notifier_profile: str,
) -> str:
    """Stable internal identity for one board notification subscription."""
    identity = {
        "board": str(board or "default"),
        "task_id": str(task_id),
        "platform": str(platform or "").lower(),
        "chat_id": str(chat_id),
        "thread_id": str(thread_id or ""),
        "notifier_profile": str(notifier_profile),
    }
    packed = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()[:24]


def _relay_spec_from_job(job: Any) -> Optional[dict[str, Any]]:
    if not isinstance(job, dict):
        return None
    prompt = job.get("prompt")
    if not isinstance(prompt, str) or len(prompt) > 4096:
        return None
    try:
        spec = json.loads(prompt)
    except (TypeError, ValueError):
        return None
    if not isinstance(spec, dict) or spec.get("kind") != _RELAY_MARKER:
        return None
    if spec.get("version") != _RELAY_VERSION:
        return None
    required = {
        "relay_key",
        "board",
        "task_id",
        "platform",
        "chat_id",
        "thread_id",
        "notifier_profile",
        "title",
    }
    if not required.issubset(spec):
        return None
    if not _TASK_RE.fullmatch(str(spec["task_id"])):
        return None
    if not _PROFILE_RE.fullmatch(str(spec["notifier_profile"])):
        return None
    expected = progress_relay_key(
        board=str(spec["board"]),
        task_id=str(spec["task_id"]),
        platform=str(spec["platform"]),
        chat_id=str(spec["chat_id"]),
        thread_id=str(spec.get("thread_id") or ""),
        notifier_profile=str(spec["notifier_profile"]),
    )
    if str(spec.get("relay_key")) != expected:
        return None
    return spec


def _looks_like_managed_relay(job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    if job.get("script") == RELAY_SCRIPT_NAME:
        return True
    prompt = job.get("prompt")
    if not isinstance(prompt, str) or len(prompt) > 4096:
        return False
    try:
        value = json.loads(prompt)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("kind") == _RELAY_MARKER


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _ensure_relay_script() -> Path:
    scripts_dir = get_hermes_home() / "scripts"
    path = scripts_dir / RELAY_SCRIPT_NAME
    expected = _relay_script_source()
    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("cannot read managed Kanban relay script") from exc
        if current != expected and "Hermes-managed Kanban no-agent relay" not in current:
            raise RuntimeError(
                f"refusing to overwrite unmanaged profile script {RELAY_SCRIPT_NAME}"
            )
        if current == expected:
            os.chmod(path, 0o600)
            return path
    _atomic_write(path, expected, mode=0o600)
    return path


def _relay_state_path(relay_key: str) -> Path:
    return get_hermes_home() / "cron" / "state" / _RELAY_STATE_DIR / f"{relay_key}.json"


def _clear_relay_state(relay_key: str) -> None:
    try:
        _relay_state_path(relay_key).unlink(missing_ok=True)
    except OSError:
        pass


def _safe_title(value: Any) -> str:
    return (
        sanitize_human_progress_text(value, max_chars=120)
        or DEFAULT_HUMAN_PROGRESS_TITLE
    )


def _relay_prompt(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _owned_subscription_specs(notifier_profile: str) -> dict[str, dict[str, Any]]:
    from hermes_cli import kanban_db as kb

    specs: dict[str, dict[str, Any]] = {}
    try:
        boards = kb.list_boards(include_archived=False)
    except Exception:
        boards = [kb.read_board_metadata(kb.DEFAULT_BOARD)]
    seen_db_paths: set[str] = set()
    for board_meta in boards:
        board = str(board_meta.get("slug") or kb.DEFAULT_BOARD)
        db_path = board_meta.get("db_path")
        try:
            resolved_db = str(
                Path(db_path).expanduser().resolve()
                if db_path
                else kb.kanban_db_path(board).resolve()
            )
        except Exception:
            resolved_db = f"slug:{board}"
        if resolved_db in seen_db_paths:
            continue
        seen_db_paths.add(resolved_db)
        try:
            conn = kb.connect(board=board)
        except Exception:
            continue
        try:
            for sub in kb.list_notify_subs(conn):
                owner = str(sub.get("notifier_profile") or notifier_profile)
                if owner != notifier_profile:
                    continue
                task_id = str(sub.get("task_id") or "")
                platform = str(sub.get("platform") or "").lower()
                chat_id = str(sub.get("chat_id") or "")
                thread_id = str(sub.get("thread_id") or "")
                if not _TASK_RE.fullmatch(task_id) or not platform or not chat_id:
                    continue
                task = kb.get_task(conn, task_id)
                title = _safe_title(task.title if task else DEFAULT_HUMAN_PROGRESS_TITLE)
                relay_key = progress_relay_key(
                    board=board,
                    task_id=task_id,
                    platform=platform,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    notifier_profile=owner,
                )
                specs[relay_key] = {
                    "kind": _RELAY_MARKER,
                    "version": _RELAY_VERSION,
                    "relay_key": relay_key,
                    "board": board,
                    "task_id": task_id,
                    "platform": platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "user_id": str(sub.get("user_id") or ""),
                    "notifier_profile": owner,
                    "title": title,
                }
        finally:
            conn.close()
    return specs


def _origin_from_spec(spec: dict[str, Any]) -> dict[str, str]:
    origin = {
        "platform": str(spec["platform"]),
        "chat_id": str(spec["chat_id"]),
        "profile": str(spec["notifier_profile"]),
    }
    if spec.get("thread_id"):
        origin["thread_id"] = str(spec["thread_id"])
    if spec.get("user_id"):
        origin["user_id"] = str(spec["user_id"])
    return origin


def _existing_relay_jobs(notifier_profile: str) -> dict[str, list[dict[str, Any]]]:
    from cron.jobs import load_jobs

    by_key: dict[str, list[dict[str, Any]]] = {}
    for job in load_jobs():
        if not _looks_like_managed_relay(job):
            continue
        spec = _relay_spec_from_job(job)
        if spec is None or spec["notifier_profile"] != notifier_profile:
            by_key.setdefault(_INVALID_RELAY_BUCKET, []).append(job)
            continue
        by_key.setdefault(str(spec["relay_key"]), []).append(job)
    return by_key


def _pause_job_verified(job_id: str, *, reason: str) -> bool:
    from cron.jobs import pause_job

    paused = pause_job(job_id, reason=reason)
    return bool(
        paused
        and not paused.get("enabled")
        and paused.get("state") == "paused"
    )


def _relay_script_is_current() -> bool:
    path = get_hermes_home() / "scripts" / RELAY_SCRIPT_NAME
    try:
        return path.is_file() and path.read_text(encoding="utf-8") == _relay_script_source()
    except OSError:
        return False


def _job_matches_spec(
    job: dict[str, Any],
    spec: dict[str, Any],
    interval: int,
) -> bool:
    schedule = job.get("schedule") or {}
    try:
        schedule_matches = (
            schedule.get("kind") == "interval"
            and int(schedule.get("minutes") or 0) == interval
        )
    except (TypeError, ValueError):
        schedule_matches = False
    return bool(
        job.get("enabled")
        and job.get("state") != "paused"
        and job.get("no_agent") is True
        and job.get("script") == RELAY_SCRIPT_NAME
        and job.get("deliver") == "origin"
        and job.get("origin") == _origin_from_spec(spec)
        and job.get("prompt") == _relay_prompt(spec)
        and job.get("wrap_response") is False
        and schedule_matches
        and _relay_script_is_current()
    )


def _job_is_safe_to_emit(job: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Validate delivery-critical fields immediately before producing stdout."""
    return bool(
        job.get("enabled")
        and job.get("state") != "paused"
        and job.get("no_agent") is True
        and job.get("script") == RELAY_SCRIPT_NAME
        and job.get("deliver") == "origin"
        and job.get("origin") == _origin_from_spec(spec)
        and job.get("prompt") == _relay_prompt(spec)
        and job.get("wrap_response") is False
        and _relay_script_is_current()
    )


def reconcile_kanban_progress_crons(
    notifier_profile: str,
    *,
    interval_minutes: int = 5,
    enabled: bool = True,
    defer_direct_on_contention: bool = True,
) -> RelayReconcileResult:
    """Project owned Kanban subscriptions into idempotent no-agent cron jobs.

    Only fully validated jobs enter ``ready_keys``. During a concurrent
    reconciliation, current subscription keys enter ``deferred_keys`` so the
    notifier waits one tick instead of racing a first cron delivery. Errors
    pause all owned relays before direct delivery is allowed to resume.
    """
    profile = str(notifier_profile or "").strip()
    if not _PROFILE_RE.fullmatch(profile):
        return RelayReconcileResult()
    active_env_profile = str(os.environ.get("HERMES_PROFILE") or "").strip()
    if active_env_profile and active_env_profile != profile:
        # A multiplexing gateway must not write a secondary profile's cron store
        # through the active profile's HERMES_HOME.
        return RelayReconcileResult()
    interval = min(60, max(1, int(interval_minutes)))
    subscription_specs: dict[str, dict[str, Any]] = {}
    if enabled:
        try:
            subscription_specs = _owned_subscription_specs(profile)
        except Exception:
            subscription_specs = {}
    deferred = frozenset(subscription_specs)
    desired_specs = subscription_specs if enabled else {}
    lock_path = get_hermes_home() / "cron" / _RELAY_LOCK_NAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        if not enabled and defer_direct_on_contention:
            try:
                subscription_specs = _owned_subscription_specs(profile)
            except Exception:
                subscription_specs = {}
            deferred = frozenset(subscription_specs)
        return RelayReconcileResult(deferred_keys=deferred)
    try:
        from gateway.status import _release_file_lock, _try_acquire_file_lock

        if not _try_acquire_file_lock(handle):
            if not enabled and defer_direct_on_contention:
                try:
                    subscription_specs = _owned_subscription_specs(profile)
                except Exception:
                    subscription_specs = {}
                deferred = frozenset(subscription_specs)
            return RelayReconcileResult(deferred_keys=deferred)
        try:
            existing = _existing_relay_jobs(profile)
            from cron.jobs import create_job, resume_job, update_job

            malformed_jobs = existing.pop(_INVALID_RELAY_BUCKET, [])
            for malformed in malformed_jobs:
                if malformed.get("enabled") or malformed.get("state") != "paused":
                    if not _pause_job_verified(
                        malformed["id"], reason="Invalid managed Kanban progress relay"
                    ):
                        raise RuntimeError("could not quarantine invalid Kanban relay")

            if not enabled:
                for relay_key, jobs in existing.items():
                    for job in jobs:
                        if job.get("enabled") or job.get("state") != "paused":
                            if not _pause_job_verified(
                                job["id"], reason="Kanban cron delivery disabled"
                            ):
                                raise RuntimeError("could not disable Kanban relay")
                    _clear_relay_state(relay_key)
                return RelayReconcileResult()

            if desired_specs:
                _ensure_relay_script()

            ready: set[str] = set()
            for relay_key, spec in desired_specs.items():
                jobs = existing.get(relay_key, [])
                primary = jobs[0] if jobs else None
                for duplicate in jobs[1:]:
                    if duplicate.get("enabled") or duplicate.get("state") != "paused":
                        if not _pause_job_verified(
                            duplicate["id"], reason="Duplicate Kanban progress relay"
                        ):
                            raise RuntimeError("could not pause duplicate Kanban relay")
                prompt = _relay_prompt(spec)
                name_title = " ".join(str(spec["title"]).split())[:100]
                name = f"Progreso Kanban: {name_title}"
                origin = _origin_from_spec(spec)
                if primary is None:
                    primary = create_job(
                        prompt=prompt,
                        schedule=f"every {interval}m",
                        name=name,
                        deliver="origin",
                        origin=origin,
                        skills=[],
                        model=None,
                        provider=None,
                        script=RELAY_SCRIPT_NAME,
                        no_agent=True,
                        progress=False,
                    )
                    primary = update_job(
                        primary["id"],
                        {"wrap_response": False},
                    ) or primary
                else:
                    updates: dict[str, Any] = {}
                    if primary.get("prompt") != prompt:
                        updates["prompt"] = prompt
                    if primary.get("name") != name:
                        updates["name"] = name
                    if primary.get("origin") != origin:
                        updates["origin"] = origin
                    if primary.get("script") != RELAY_SCRIPT_NAME:
                        updates["script"] = RELAY_SCRIPT_NAME
                    if primary.get("deliver") != "origin":
                        updates["deliver"] = "origin"
                    if not primary.get("no_agent"):
                        updates["no_agent"] = True
                    if primary.get("wrap_response") is not False:
                        updates["wrap_response"] = False
                    schedule = primary.get("schedule") or {}
                    if schedule.get("kind") != "interval" or int(
                        schedule.get("minutes") or 0
                    ) != interval:
                        updates["schedule"] = f"every {interval}m"
                    if updates:
                        primary = update_job(primary["id"], updates) or primary
                    if not primary.get("enabled") or primary.get("state") == "paused":
                        _clear_relay_state(relay_key)
                        primary = resume_job(primary["id"]) or primary
                if _job_matches_spec(primary, spec, interval):
                    ready.add(relay_key)
                else:
                    if not _pause_job_verified(
                        primary["id"], reason="Invalid Kanban progress relay"
                    ):
                        raise RuntimeError("could not pause invalid Kanban relay")

            active_keys = set(desired_specs)
            for relay_key, jobs in existing.items():
                if relay_key in active_keys:
                    continue
                for job in jobs:
                    if job.get("enabled") or job.get("state") != "paused":
                        if not _pause_job_verified(
                            job["id"], reason="Kanban subscription ended"
                        ):
                            raise RuntimeError("could not stop orphan Kanban relay")
                    _clear_relay_state(relay_key)
            return RelayReconcileResult(ready_keys=frozenset(ready))
        except Exception:
            # Never let a partially-created or stale job suppress the direct
            # fallback. If cleanup itself cannot be verified, defer this tick
            # instead of risking duplicate or wrong-origin delivery.
            cleanup_ok = True
            try:
                for relay_key, jobs in _existing_relay_jobs(profile).items():
                    for job in jobs:
                        if job.get("enabled") or job.get("state") != "paused":
                            cleanup_ok = cleanup_ok and _pause_job_verified(
                                job["id"],
                                reason="Kanban relay reconciliation failed",
                            )
                    _clear_relay_state(relay_key)
            except Exception:
                cleanup_ok = False
            return (
                RelayReconcileResult()
                if cleanup_ok
                else RelayReconcileResult(deferred_keys=deferred)
            )
        finally:
            _release_file_lock(handle)
    finally:
        handle.close()


def _read_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        if len(raw) > 8192:
            return {}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _write_state(path: Path, value: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        mode=0o600,
    )


def _iso_timestamp(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _human_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return "menos de 1 min"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {remainder} min" if remainder else f"{hours} h"
    days, remainder_hours = divmod(hours, 24)
    return f"{days} d {remainder_hours} h" if remainder_hours else f"{days} d"


def _latest_progress(events: list[Any], current_run_id: Any) -> tuple[str, int]:
    candidates = [event for event in events if getattr(event, "kind", "") == "progress"]
    if current_run_id is not None:
        current = [
            event
            for event in candidates
            if getattr(event, "run_id", None) is not None
            and int(event.run_id) == int(current_run_id)
        ]
        if current:
            candidates = current
    for event in reversed(candidates):
        payload = getattr(event, "payload", None) or {}
        text = sanitize_human_progress_text(payload.get("text"), max_chars=600)
        if text:
            return text, int(getattr(event, "created_at", 0) or 0)
    return DEFAULT_HUMAN_PROGRESS_TEXT, 0


def _latest_terminal_detail(task: Any, events: list[Any], conn: Any) -> str:
    from hermes_cli import kanban_db as kb

    if task.status == "blocked":
        for event in reversed(events):
            if event.kind != "blocked":
                continue
            payload = event.payload or {}
            detail = sanitize_human_progress_text(payload.get("reason"), max_chars=600)
            if detail:
                return detail
    summary = kb.latest_summary(conn, task.id) or task.result
    return sanitize_human_progress_text(summary, max_chars=600)


def _subscription_exists(conn: Any, spec: dict[str, Any]) -> bool:
    from hermes_cli import kanban_db as kb

    for sub in kb.list_notify_subs(conn, str(spec["task_id"])):
        if (
            str(sub.get("platform") or "").lower() == str(spec["platform"]).lower()
            and str(sub.get("chat_id") or "") == str(spec["chat_id"])
            and str(sub.get("thread_id") or "") == str(spec.get("thread_id") or "")
            and str(sub.get("notifier_profile") or spec["notifier_profile"])
            == str(spec["notifier_profile"])
        ):
            return True
    return False


def _remove_subscription(conn: Any, spec: dict[str, Any]) -> None:
    from hermes_cli import kanban_db as kb

    kb.remove_notify_sub(
        conn,
        task_id=str(spec["task_id"]),
        platform=str(spec["platform"]),
        chat_id=str(spec["chat_id"]),
        thread_id=str(spec.get("thread_id") or ""),
    )


def _render_task_report(task: Any, events: list[Any], conn: Any) -> tuple[str, bool]:
    now = time.time()
    title = _safe_title(task.title)
    started_at = float(task.started_at or task.created_at or now)
    duration = _human_duration(now - started_at)
    status = str(task.status or "ready")
    if status in _TERMINAL_STATUSES:
        headings = {
            "done": "Kanban completado",
            "archived": "Kanban archivado",
            "blocked": "Kanban bloqueado",
        }
        labels = {
            "done": "Completado",
            "archived": "Archivado",
            "blocked": "Bloqueado",
        }
        lines = [headings[status], title, f"Estado: {labels[status]} · {duration}"]
        detail = _latest_terminal_detail(task, events, conn)
        if detail:
            lines.append(("Bloqueo: " if status == "blocked" else "Resultado: ") + detail)
        return "\n".join(lines), True

    labels = {
        "ready": "Pendiente",
        "running": "En curso",
        "waiting": "En espera",
    }
    progress, progress_at = _latest_progress(events, task.current_run_id)
    lines = [
        "Kanban en curso" if status == "running" else "Seguimiento Kanban",
        title,
        f"Estado: {labels.get(status, status.capitalize())} · {duration}",
        f"Último avance: {progress}",
    ]
    if progress_at:
        lines.append(f"Actualizado hace {_human_duration(now - progress_at)}")
    return "\n".join(lines), False


def _relay_script_run(job_id: str) -> str:
    from cron.jobs import get_job
    from hermes_cli import kanban_db as kb

    job = get_job(job_id)
    spec = _relay_spec_from_job(job)
    if job is None or spec is None:
        if job is not None and _looks_like_managed_relay(job):
            _pause_job_verified(job_id, reason="Invalid managed Kanban progress relay")
        return _SILENT_WAKE_GATE
    if not _job_is_safe_to_emit(job, spec):
        _pause_job_verified(job_id, reason="Unsafe Kanban relay delivery state")
        return _SILENT_WAKE_GATE
    state_path = _relay_state_path(str(spec["relay_key"]))
    state = _read_state(state_path)
    pending_at = float(state.get("terminal_pending_at") or 0)
    if pending_at:
        last_run_at = _iso_timestamp(job.get("last_run_at"))
        if (
            last_run_at >= pending_at
            and job.get("last_status") == "ok"
            and not job.get("last_delivery_error")
        ):
            if not _pause_job_verified(
                job_id, reason="Kanban final update delivered"
            ):
                return _SILENT_WAKE_GATE
            conn = kb.connect(board=str(spec["board"]))
            try:
                _remove_subscription(conn, spec)
            finally:
                conn.close()
            _clear_relay_state(str(spec["relay_key"]))
            return ""

    conn = kb.connect(board=str(spec["board"]))
    try:
        if not _subscription_exists(conn, spec):
            if not _pause_job_verified(job_id, reason="Kanban subscription ended"):
                return _SILENT_WAKE_GATE
            _clear_relay_state(str(spec["relay_key"]))
            return ""
        task = kb.get_task(conn, str(spec["task_id"]))
        if task is None:
            report = (
                "Seguimiento Kanban detenido\n"
                f"{_safe_title(spec.get('title'))}\n"
                "La tarjeta ya no está disponible."
            )
            terminal = True
        else:
            report, terminal = _render_task_report(task, kb.list_events(conn, task.id), conn)
    finally:
        conn.close()

    if terminal:
        _write_state(
            state_path,
            {
                "terminal_pending_at": time.time(),
                "terminal_fingerprint": hashlib.sha256(report.encode("utf-8")).hexdigest(),
            },
        )
    return report


def relay_script_main() -> int:
    """Entry point used by the profile-local no-agent cron script."""
    job_id = str(os.environ.get("HERMES_CRON_JOB_ID") or "").strip()
    if not _JOB_RE.fullmatch(job_id):
        print(_SILENT_WAKE_GATE)
        return 0
    try:
        output = _relay_script_run(job_id)
    except Exception:
        output = _SILENT_WAKE_GATE
    if output:
        print(output)
    return 0
