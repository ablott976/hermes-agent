"""Focused lifecycle tests for opt-in persistent cron conversations."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from cron.jobs import (
    claim_persistent_rollover,
    claim_persistent_rollover_recovery,
    create_job,
    get_job,
    load_jobs,
    pause_job,
    resume_job,
    save_jobs,
    set_persistent_session_state,
    update_job,
)
from cron.scheduler import (
    _base_url_contract,
    _load_persistent_cron_history,
    _persistent_runtime_contract,
    _persistent_skill_contract,
    _persistent_tool_contract,
    _resolve_cron_max_iterations,
    _resolve_persistent_rollover_runs,
    run_job,
    run_one_job,
)
from hermes_cli.config import DEFAULT_CONFIG
from hermes_state import SessionDB as RealSessionDB


@dataclass
class FakeSessionStore:
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tips: dict[str, str] = field(default_factory=dict)
    reopened: list[str] = field(default_factory=list)
    ended: list[tuple[str, str]] = field(default_factory=list)

    def resolve_resume_session_id(self, root: str) -> str:
        return self.tips.get(root, root)

    def get_session(self, session_id: str):
        return self.sessions.get(session_id)

    def get_messages_as_conversation(self, session_id: str):
        return [dict(message) for message in self.messages.get(session_id, [])]

    def list_recent_user_messages(self, session_id: str, limit: int = 20):
        rows = self.messages.get(session_id, [])
        return [
            {"id": index + 1, "preview": str(message.get("content") or "")[:80]}
            for index, message in reversed(list(enumerate(rows)))
            if message.get("role") == "user"
        ][:limit]

    def rewind_to_message(self, session_id: str, target_message_id: int):
        rows = self.messages.get(session_id, [])
        target_index = target_message_id - 1
        if target_index < 0 or target_index >= len(rows):
            raise ValueError("message not found")
        if rows[target_index].get("role") != "user":
            raise ValueError("rewind target must be a user message")
        rewound_count = len(rows) - target_index
        del rows[target_index:]
        return {"rewound_count": rewound_count}

    def reopen_session(self, session_id: str):
        self.reopened.append(session_id)
        if session_id in self.sessions:
            self.sessions[session_id]["ended_at"] = None
            self.sessions[session_id]["end_reason"] = None

    def set_session_title(self, session_id: str, title: str):
        if session_id in self.sessions:
            self.sessions[session_id]["title"] = title

    def end_session(self, session_id: str, reason: str):
        self.ended.append((session_id, reason))
        if session_id in self.sessions:
            self.sessions[session_id]["end_reason"] = reason
            self.sessions[session_id]["ended_at"] = 1

    def close(self):
        return None


class FakeAgent:
    calls: list[dict[str, Any]] = []
    instances: list["FakeAgent"] = []
    fail_next = False
    system_contract = "stable-system-contract"
    tool_contract = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run a command.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        },
    ]

    def __init__(self, **kwargs):
        self.model = kwargs["model"]
        self.provider = kwargs["provider"]
        self.base_url = kwargs["base_url"]
        self.api_mode = kwargs["api_mode"]
        self.valid_tool_names = {"read_file", "terminal"}
        self.tools = json.loads(json.dumps(type(self).tool_contract))
        self.session_id = kwargs["session_id"]
        self._session_db = kwargs["session_db"]
        self.max_iterations = kwargs["max_iterations"]
        self._memory_nudge_interval = 10
        self._skill_nudge_interval = 10
        type(self).instances.append(self)

    def _build_system_prompt_parts(self, _system_message=None):
        return {
            "stable": type(self).system_contract,
            "context": "project-context",
            "volatile": "ignored-date",
        }

    @staticmethod
    def _format_turn_completion_explanation(_reason: str) -> str:
        return ""

    def run_conversation(self, prompt, system_message=None, conversation_history=None):
        history = [dict(message) for message in (conversation_history or [])]
        self.calls.append(
            {
                "session_id": self.session_id,
                "prompt": prompt,
                "history": history,
            }
        )
        user = {"role": "user", "content": prompt}
        if hasattr(self._session_db, "sessions"):
            self._session_db.sessions.setdefault(
                self.session_id,
                {
                    "id": self.session_id,
                    "system_prompt": "persisted-system-prompt",
                    "started_at": time.time(),
                },
            )
            self._session_db.messages.setdefault(self.session_id, []).append(user)
        else:
            if self._session_db.get_session(self.session_id) is None:
                self._session_db.create_session(
                    self.session_id,
                    source="cron",
                    model=self.model,
                    system_prompt="persisted-system-prompt",
                )
            self._session_db.append_message(
                self.session_id,
                "user",
                prompt,
            )
        if type(self).fail_next:
            type(self).fail_next = False
            raise RuntimeError("simulated crash")
        assistant = {"role": "assistant", "content": "milestone complete"}
        if hasattr(self._session_db, "messages"):
            self._session_db.messages[self.session_id].append(assistant)
        else:
            self._session_db.append_message(
                self.session_id,
                "assistant",
                assistant["content"],
            )
        return {
            "final_response": "milestone complete",
            "messages": history + [user, assistant],
            "api_calls": 1,
            "completed": True,
            "failed": False,
            "turn_exit_reason": "completed",
            "input_tokens": 100,
            "cache_read_tokens": 900,
            "output_tokens": 25,
            "session_id": self.session_id,
        }

    def close(self):
        return None


@pytest.fixture()
def persistent_env(tmp_path, monkeypatch):
    store = FakeSessionStore()
    FakeAgent.calls = []
    FakeAgent.instances = []
    FakeAgent.fail_next = False
    FakeAgent.system_contract = "stable-system-contract"
    FakeAgent.tool_contract = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "Run a command.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        },
    ]

    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr("cron.scheduler._hermes_home", tmp_path)
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("hermes_state.SessionDB", lambda: store)
    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr("hermes_cli.env_loader.load_hermes_dotenv", lambda **_kwargs: None)
    monkeypatch.setattr("hermes_cli.env_loader.reset_secret_source_cache", lambda: None)
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: [])
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    return store


def _create_persistent_job():
    return create_job(
        prompt="Implement the next verified milestone.",
        schedule="every 1m",
        model="test-model",
        provider="openrouter",
        session_mode="persistent",
    )


def test_base_url_contract_excludes_embedded_credentials_and_query():
    assert _base_url_contract(
        "HTTPS://user:password@example.com:8443/v1?api_key=secret#fragment"
    ) == "https://example.com:8443/v1"


def test_fresh_remains_default_and_persistent_rejects_script_only(persistent_env):
    fresh = create_job(prompt="monitor", schedule="every 1h")
    assert fresh["session_mode"] == "fresh"
    assert get_job(fresh["id"])["session_mode"] == "fresh"

    with pytest.raises(ValueError, match="cannot be used with no_agent"):
        create_job(
            prompt="",
            schedule="every 1h",
            script="watch.py",
            no_agent=True,
            session_mode="persistent",
        )


def test_persistent_tool_contract_ignores_descriptions_and_order_but_keeps_schema():
    original = [
        {
            "type": "function",
            "function": {
                "name": "demo",
                "description": "Original model guidance.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"},
                        "label": {"type": "string"},
                    },
                    "required": ["count"],
                },
            },
        },
        {"type": "function", "function": {"name": "other"}},
    ]
    metadata_only = [
        {"type": "function", "function": {"name": "other", "description": "New text"}},
        {
            "type": "function",
            "function": {
                "name": "demo",
                "description": "Reworded model guidance.",
                "parameters": {
                    "required": ["count"],
                    "properties": {
                        "label": {"type": "string"},
                        "count": {"type": "integer"},
                    },
                    "type": "object",
                },
            },
        },
    ]
    incompatible = json.loads(json.dumps(metadata_only))
    incompatible[1]["function"]["parameters"]["required"] = ["count", "label"]
    strict_change = json.loads(json.dumps(metadata_only))
    strict_change[1]["function"]["strict"] = True

    assert _persistent_tool_contract(original) == _persistent_tool_contract(metadata_only)
    assert _persistent_tool_contract(original) != _persistent_tool_contract(incompatible)
    assert _persistent_tool_contract(original) != _persistent_tool_contract(strict_change)


def test_runtime_contract_contains_only_digests(persistent_env):
    job = _create_persistent_job()
    agent = FakeAgent(
        model="secret-model-name",
        provider="secret-provider-name",
        base_url="https://user:password@example.invalid/v1",
        api_mode="chat_completions",
        max_iterations=12,
        session_id="probe",
        session_db=persistent_env,
    )

    contract = _persistent_runtime_contract(job, agent, "/private/customer/workdir")
    encoded = json.dumps(contract, sort_keys=True)

    assert contract["version"] == 4
    assert all(
        key == "version" or (isinstance(value, str) and len(value) == 64)
        for key, value in contract.items()
    )
    for raw in (
        job["prompt"],
        "secret-model-name",
        "secret-provider-name",
        "password",
        "/private/customer/workdir",
        "read_file",
        "path",
    ):
        assert raw not in encoded


@pytest.mark.parametrize(
    ("job", "cfg", "expected"),
    [
        ({"session_mode": "fresh"}, {"agent": {"max_turns": 300}}, 300),
        ({"session_mode": "persistent"}, {"agent": {"max_turns": 300}}, 12),
        ({"session_mode": "persistent"}, {"agent": {"max_turns": 8}}, 8),
        (
            {"session_mode": "persistent"},
            {"agent": {"max_turns": 300}, "cron": {"persistent_max_turns": 20}},
            20,
        ),
        (
            {"session_mode": "persistent"},
            {"agent": {"max_turns": 300}, "cron": {"persistent_max_turns": 0}},
            12,
        ),
        (
            {"session_mode": "persistent"},
            {"agent": {"max_turns": 300}, "cron": {"persistent_max_turns": "many"}},
            12,
        ),
        (
            {"session_mode": "persistent"},
            {"agent": {"max_turns": 300}, "cron": {"persistent_max_turns": True}},
            12,
        ),
    ],
)
def test_persistent_ticks_use_a_bounded_iteration_budget(job, cfg, expected):
    assert DEFAULT_CONFIG["cron"]["persistent_max_turns"] == 12
    assert _resolve_cron_max_iterations(job, cfg) == expected


def test_scheduler_applies_persistent_budget_and_disables_background_review(
    persistent_env,
):
    job = _create_persistent_job()

    assert run_job(job)[0] is True

    agent = FakeAgent.instances[-1]
    assert agent.max_iterations == 12
    assert agent._memory_nudge_interval == 0
    assert agent._skill_nudge_interval == 0


def test_scheduler_reads_persistent_budget_override_from_config(
    persistent_env,
    tmp_path,
):
    (tmp_path / "config.yaml").write_text(
        "agent:\n  max_turns: 30\ncron:\n  persistent_max_turns: 7\n",
        encoding="utf-8",
    )

    assert run_job(_create_persistent_job())[0] is True

    assert FakeAgent.instances[-1].max_iterations == 7


def test_switching_to_fresh_clears_scheduler_owned_continuation_state(persistent_env):
    job = _create_persistent_job()
    set_persistent_session_state(job["id"], "root", "fingerprint")

    update_job(job["id"], {"session_mode": "fresh"})
    stored = get_job(job["id"])

    assert stored["session_mode"] == "fresh"
    assert "session_root_id" not in stored
    assert "session_runtime_fingerprint" not in stored


def test_legacy_no_agent_persistent_record_never_opens_a_conversation(
    persistent_env,
    monkeypatch,
):
    monkeypatch.setattr("cron.scheduler._run_job_script", lambda _path: (True, "healthy"))
    legacy = {
        "id": "legacy-no-agent",
        "name": "legacy watchdog",
        "schedule_display": "every 1h",
        "script": "watchdog.py",
        "no_agent": True,
        "session_mode": "persistent",
        "session_root_id": "stale-root",
        "session_runtime_fingerprint": "stale-fingerprint",
    }

    success, _doc, response, error = run_job(legacy)

    assert success is True
    assert response == "healthy"
    assert error is None
    assert FakeAgent.calls == []
    assert persistent_env.sessions == {}


def test_pause_and_resume_preserve_persistent_session_pointer(persistent_env):
    job = _create_persistent_job()
    set_persistent_session_state(job["id"], "root", "fingerprint")

    assert pause_job(job["id"])["session_root_id"] == "root"
    assert resume_job(job["id"])["session_root_id"] == "root"
    assert get_job(job["id"])["session_runtime_fingerprint"] == "fingerprint"


def test_fresh_runs_remain_independent_and_rebootstrap_every_time(
    persistent_env,
    monkeypatch,
):
    sequence = iter(("fresh-1", "fresh-2"))
    monkeypatch.setattr("cron.scheduler._new_cron_session_id", lambda *_a, **_k: next(sequence))
    job = create_job(
        prompt="Generate the periodic report.",
        schedule="every 1h",
        model="test-model",
        provider="openrouter",
        session_mode="fresh",
    )

    assert run_job(job)[0] is True
    assert run_job(job)[0] is True

    calls = FakeAgent.calls[-2:]
    assert [call["session_id"] for call in calls] == ["fresh-1", "fresh-2"]
    assert all("Generate the periodic report." in call["prompt"] for call in calls)
    assert all(call["history"] == [] for call in calls)
    stored = get_job(job["id"])
    assert "session_root_id" not in stored
    assert "session_runtime_fingerprint" not in stored


def test_second_tick_resumes_same_conversation_with_compact_prompt(persistent_env):
    first_job = _create_persistent_job()

    first = run_job(first_job)
    stored = get_job(first_job["id"])
    second = run_job(stored)

    assert first[0] is True
    assert second[0] is True
    assert "## Tick usage" in second[1]
    assert len(FakeAgent.calls) == 2
    first_call, second_call = FakeAgent.calls
    assert first_call["session_id"] == second_call["session_id"]
    assert "Implement the next verified milestone." in first_call["prompt"]
    assert "exactly one bounded milestone" in first_call["prompt"]
    assert "changed, checks, next_action, and blockers" in first_call["prompt"]
    assert "CRON CONTINUATION" in second_call["prompt"]
    assert "Implement the next verified milestone." not in second_call["prompt"]
    assert "exactly one bounded milestone" in second_call["prompt"]
    assert "Do not begin another milestone in this tick" in second_call["prompt"]
    assert [message["role"] for message in second_call["history"]] == [
        "user",
        "assistant",
    ]
    assert stored["session_root_id"] == first_call["session_id"]
    assert stored["session_runtime_fingerprint"]
    assert persistent_env.reopened == [first_call["session_id"]]
    assert persistent_env.ended[-1][1] == "cron_waiting"


@pytest.mark.parametrize(
    ("raw", "expected"),
    ((0, 0), (-1, 0), (True, 0), (5.5, 0), ("invalid", 0), ("5", 5), (5, 5)),
)
def test_resolve_persistent_rollover_runs_is_opt_in_and_fail_closed(raw, expected):
    assert _resolve_persistent_rollover_runs(
        {"cron": {"persistent_rollover_runs": raw}}
    ) == expected


def test_persistent_rollover_is_disabled_by_default():
    assert DEFAULT_CONFIG["cron"]["persistent_rollover_runs"] == 0


def test_planned_rollover_bootstraps_once_then_resumes_new_root(
    persistent_env,
    monkeypatch,
):
    monkeypatch.setattr(
        "cron.scheduler.load_config",
        lambda: {"cron": {"persistent_rollover_runs": 5}},
    )
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    original = get_job(job["id"])
    assert original is not None
    original_root = original["session_root_id"]
    jobs = load_jobs()
    jobs[0]["repeat"]["completed"] = 5
    jobs[0]["persistent_successful_runs"] = 5
    jobs[0]["persistent_contract_forks"] = 1
    save_jobs(jobs)

    rollover_candidate = get_job(job["id"])
    assert rollover_candidate is not None
    assert run_job(rollover_candidate)[0] is True
    rolled = get_job(job["id"])
    assert rolled is not None
    rolled_root = rolled["session_root_id"]
    assert rolled_root != original_root
    assert rolled["persistent_rollover_checkpoint"] == 5
    assert rolled["persistent_planned_rollovers"] == 1
    assert rolled["persistent_contract_forks"] == 1
    assert "persistent_contract_update_pending" not in rolled
    bootstrap = FakeAgent.calls[-1]
    assert bootstrap["session_id"] == rolled_root
    assert bootstrap["history"] == []
    assert "CRON CONTINUATION" not in bootstrap["prompt"]

    resume_candidate = get_job(job["id"])
    assert resume_candidate is not None
    assert run_job(resume_candidate)[0] is True
    resumed = get_job(job["id"])
    assert resumed is not None
    assert resumed["session_root_id"] == rolled_root
    continuation = FakeAgent.calls[-1]
    assert continuation["session_id"] == rolled_root
    assert [message["role"] for message in continuation["history"]] == [
        "user",
        "assistant",
    ]
    assert "CRON CONTINUATION" in continuation["prompt"]


def test_planned_rollover_preserves_armed_fork_fuse(
    persistent_env,
    monkeypatch,
):
    monkeypatch.setattr(
        "cron.scheduler.load_config",
        lambda: {"cron": {"persistent_rollover_runs": 5}},
    )
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    jobs = load_jobs()
    jobs[0]["repeat"]["completed"] = 5
    jobs[0]["persistent_successful_runs"] = 5
    jobs[0]["persistent_contract_forks"] = 1
    save_jobs(jobs)
    candidate = get_job(job["id"])
    assert candidate is not None

    assert run_job(candidate)[0] is True

    rolled = get_job(job["id"])
    assert rolled is not None
    rolled_root = rolled["session_root_id"]
    assert rolled["persistent_contract_forks"] == 1
    paused = set_persistent_session_state(
        job["id"],
        "unexpected-root-after-rollover",
        "unexpected-fingerprint-after-rollover",
        runtime_contract=rolled["session_runtime_contract"],
        unexpected_fork=True,
    )
    assert paused is not None
    assert paused["enabled"] is False
    assert paused["persistent_contract_forks"] == 2
    assert paused["session_root_id"] == rolled_root


def test_crash_after_rollover_claim_recovers_with_one_bootstrap(
    persistent_env,
    monkeypatch,
):
    monkeypatch.setattr(
        "cron.scheduler.load_config",
        lambda: {"cron": {"persistent_rollover_runs": 5}},
    )
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    stored = get_job(job["id"])
    assert stored is not None
    jobs = load_jobs()
    jobs[0]["repeat"]["completed"] = 5
    jobs[0]["persistent_successful_runs"] = 5
    save_jobs(jobs)
    claimed = claim_persistent_rollover(
        job["id"],
        expected_root_id=stored["session_root_id"],
        expected_successful_runs=5,
        rollover_runs=5,
    )
    assert claimed is not None
    assert claimed["_persistent_rollover_claim"] == "applied"
    jobs = load_jobs()
    jobs[0]["persistent_rollover_lease"]["at"] = "2000-01-01T00:00:00+00:00"
    save_jobs(jobs)
    rootless = get_job(job["id"])
    assert rootless is not None
    assert "session_root_id" not in rootless

    calls_before = len(FakeAgent.calls)
    assert run_job(rootless)[0] is True
    recovered = get_job(job["id"])
    assert recovered is not None
    recovered_root = recovered["session_root_id"]
    assert len(FakeAgent.calls) == calls_before + 1
    assert "persistent_contract_update_pending" not in recovered
    assert "persistent_contract_forks" not in recovered

    resume_candidate = get_job(job["id"])
    assert resume_candidate is not None
    assert run_job(resume_candidate)[0] is True
    resumed = get_job(job["id"])
    assert resumed is not None
    assert resumed["session_root_id"] == recovered_root
    assert len(FakeAgent.calls) == calls_before + 2


def test_stale_rollover_runner_defers_without_model_or_repeat_increment(
    persistent_env,
    monkeypatch,
):
    monkeypatch.setattr(
        "cron.scheduler.load_config",
        lambda: {"cron": {"persistent_rollover_runs": 5}},
    )
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    jobs = load_jobs()
    jobs[0]["repeat"]["completed"] = 5
    jobs[0]["persistent_successful_runs"] = 5
    save_jobs(jobs)
    stale_snapshot = get_job(job["id"])
    assert stale_snapshot is not None
    claimed = claim_persistent_rollover(
        job["id"],
        expected_root_id=stale_snapshot["session_root_id"],
        expected_successful_runs=5,
        rollover_runs=5,
    )
    assert claimed is not None
    assert claimed["_persistent_rollover_claim"] == "applied"
    calls_before = len(FakeAgent.calls)

    assert run_one_job(stale_snapshot) is True

    current = get_job(job["id"])
    assert current is not None
    assert len(FakeAgent.calls) == calls_before
    assert current["repeat"]["completed"] == 5
    assert current["persistent_successful_runs"] == 5
    assert "session_root_id" not in current
    assert current["persistent_rollover_checkpoint"] == 5
    stale_write = set_persistent_session_state(
        job["id"],
        stale_snapshot["session_root_id"],
        stale_snapshot["session_runtime_fingerprint"],
        contract_update_token=str(
            stale_snapshot.get("persistent_contract_update_pending") or ""
        ),
    )
    assert stale_write is not None
    assert stale_write["_persistent_state_write_applied"] is False
    after_stale_write = get_job(job["id"])
    assert after_stale_write is not None
    assert "session_root_id" not in after_stale_write


def test_concurrent_rootless_recovery_snapshots_allow_one_model_bootstrap(
    persistent_env,
    monkeypatch,
):
    monkeypatch.setattr(
        "cron.scheduler.load_config",
        lambda: {"cron": {"persistent_rollover_runs": 5}},
    )
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    jobs = load_jobs()
    jobs[0]["repeat"]["completed"] = 5
    jobs[0]["persistent_successful_runs"] = 5
    save_jobs(jobs)
    stored = get_job(job["id"])
    assert stored is not None
    claimed = claim_persistent_rollover(
        job["id"],
        expected_root_id=stored["session_root_id"],
        expected_successful_runs=5,
        rollover_runs=5,
    )
    assert claimed is not None
    jobs = load_jobs()
    jobs[0]["persistent_rollover_lease"]["at"] = "2000-01-01T00:00:00+00:00"
    save_jobs(jobs)
    first_snapshot = get_job(job["id"])
    second_snapshot = get_job(job["id"])
    assert first_snapshot is not None
    assert second_snapshot is not None
    calls_before = len(FakeAgent.calls)

    assert run_job(first_snapshot)[0] is True
    second_metadata: dict[str, Any] = {}
    success, _doc, response, error = run_job(
        second_snapshot,
        run_metadata=second_metadata,
    )

    assert success is True
    assert response == "[SILENT]"
    assert error is None
    assert second_metadata["deferred"] is True
    assert len(FakeAgent.calls) == calls_before + 1
    final = get_job(job["id"])
    assert final is not None
    assert final["session_root_id"]
    assert "persistent_rollover_lease" not in final


def test_rollover_lease_is_revalidated_immediately_before_model(
    persistent_env,
    monkeypatch,
):
    monkeypatch.setattr(
        "cron.scheduler.load_config",
        lambda: {"cron": {"persistent_rollover_runs": 5}},
    )
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    jobs = load_jobs()
    jobs[0]["repeat"]["completed"] = 5
    jobs[0]["persistent_successful_runs"] = 5
    save_jobs(jobs)
    candidate = get_job(job["id"])
    assert candidate is not None
    calls_before = len(FakeAgent.calls)

    def _lose_lease(job_id, *, expected_owner):
        takeover = claim_persistent_rollover_recovery(
            job_id,
            expected_successful_runs=5,
            expected_owner=expected_owner,
            lease_ttl_seconds=0,
        )
        assert takeover is not None
        assert takeover["_persistent_rollover_recovery"] == "applied"
        return False

    monkeypatch.setattr(
        "cron.scheduler.heartbeat_persistent_rollover_lease",
        _lose_lease,
    )

    success, _doc, _response, error = run_job(candidate)

    assert success is False
    assert "lost before model execution" in str(error)
    assert len(FakeAgent.calls) == calls_before


def test_two_zero_tool_silences_autopause_through_shared_run_pipeline(
    persistent_env,
    monkeypatch,
    tmp_path,
):
    original_run_conversation = FakeAgent.run_conversation

    def _silent_run(self, *args, **kwargs):
        result = original_run_conversation(self, *args, **kwargs)
        result["final_response"] = "[SILENT]"
        result["turn_tool_calls"] = 0
        return result

    monkeypatch.setattr(FakeAgent, "run_conversation", _silent_run)
    created = _create_persistent_job()
    job = update_job(created["id"], {"workdir": str(tmp_path)})
    assert job is not None

    assert run_one_job(job) is True
    after_first = get_job(job["id"])
    assert after_first is not None
    assert after_first["enabled"] is True
    assert after_first["persistent_silent_ticks"] == 1

    assert run_one_job(after_first) is True
    after_second = get_job(job["id"])
    assert after_second is not None
    assert after_second["enabled"] is False
    assert after_second["state"] == "paused"
    assert after_second["persistent_silent_ticks"] == 2
    assert after_second["repeat"]["completed"] == 2


def test_persistent_session_survives_sqlite_close_and_reopen(
    persistent_env,
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("hermes_state.SessionDB", lambda: RealSessionDB(db_path))
    job = _create_persistent_job()

    assert run_job(job)[0] is True
    assert run_job(get_job(job["id"]))[0] is True

    stored = get_job(job["id"])
    db = RealSessionDB(db_path)
    try:
        tip = db.resolve_resume_session_id(stored["session_root_id"])
        session = db.get_session(tip)
        messages = db.get_messages_as_conversation(tip)
    finally:
        db.close()

    assert tip == stored["session_root_id"]
    assert session["system_prompt"] == "persisted-system-prompt"
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert len(FakeAgent.calls[-1]["history"]) == 2


def test_contract_drift_forks_and_bootstraps_new_root(persistent_env):
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    original = get_job(job["id"])
    original_root = original["session_root_id"]

    update_job(job["id"], {"prompt": "Use the revised acceptance criteria."})
    assert run_job(get_job(job["id"]))[0] is True
    updated = get_job(job["id"])

    assert updated["session_root_id"] != original_root
    second_call = FakeAgent.calls[-1]
    assert second_call["session_id"] == updated["session_root_id"]
    assert second_call["history"] == []
    assert "Use the revised acceptance criteria." in second_call["prompt"]
    assert "CRON CONTINUATION" not in second_call["prompt"]
    assert persistent_env.sessions[original_root]["title"] != persistent_env.sessions[
        updated["session_root_id"]
    ]["title"]
    assert "continuing since" in persistent_env.sessions[updated["session_root_id"]]["title"]


def test_tool_description_and_registry_order_drift_keeps_persistent_root(persistent_env):
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    original_root = get_job(job["id"])["session_root_id"]

    changed = json.loads(json.dumps(FakeAgent.tool_contract))
    changed.reverse()
    changed[0]["function"]["description"] = "Reworded guidance only."
    changed[1]["function"]["description"] = "Another description only."
    FakeAgent.tool_contract = changed

    assert run_job(get_job(job["id"]))[0] is True
    assert get_job(job["id"])["session_root_id"] == original_root
    assert "CRON CONTINUATION" in FakeAgent.calls[-1]["prompt"]


def test_two_consecutive_semantic_tool_drifts_pause_before_third_bootstrap(
    persistent_env,
):
    job = _create_persistent_job()
    assert run_job(job)[0] is True

    first_drift = json.loads(json.dumps(FakeAgent.tool_contract))
    first_drift[0]["function"]["parameters"]["required"] = []
    FakeAgent.tool_contract = first_drift
    assert run_job(get_job(job["id"]))[0] is True
    after_first = get_job(job["id"])
    assert after_first["enabled"] is True
    assert after_first["persistent_contract_forks"] == 1
    assert len(FakeAgent.calls) == 2

    second_drift = json.loads(json.dumps(FakeAgent.tool_contract))
    second_drift[0]["function"]["parameters"]["properties"]["path"]["type"] = "integer"
    FakeAgent.tool_contract = second_drift
    result = run_job(get_job(job["id"]))
    paused = get_job(job["id"])

    assert result[0] is True
    assert "pausado automáticamente" in result[2]
    assert paused["enabled"] is False
    assert paused["state"] == "paused"
    assert paused["persistent_contract_forks"] == 2
    assert len(FakeAgent.calls) == 2


def test_successful_semantic_resume_resets_contract_fork_counter(persistent_env):
    job = _create_persistent_job()
    assert run_job(job)[0] is True

    changed = json.loads(json.dumps(FakeAgent.tool_contract))
    changed[0]["function"]["parameters"]["required"] = []
    FakeAgent.tool_contract = changed
    assert run_job(get_job(job["id"]))[0] is True
    assert get_job(job["id"])["persistent_contract_forks"] == 1

    assert run_job(get_job(job["id"]))[0] is True
    assert "persistent_contract_forks" not in get_job(job["id"])


def test_compatible_explicit_update_is_consumed_before_future_drift_fuse(
    persistent_env,
):
    job = _create_persistent_job()
    assert run_job(job)[0] is True

    updated = update_job(job["id"], {"prompt": job["prompt"]})
    assert updated is not None
    assert updated["persistent_contract_update_pending"]
    assert run_job(updated)[0] is True
    compatible = get_job(job["id"])
    assert compatible is not None
    assert "persistent_contract_update_pending" not in compatible

    first_drift = json.loads(json.dumps(FakeAgent.tool_contract))
    first_drift[0]["function"]["parameters"]["required"] = []
    FakeAgent.tool_contract = first_drift
    assert run_job(compatible)[0] is True
    after_first = get_job(job["id"])
    assert after_first is not None
    assert after_first["persistent_contract_forks"] == 1

    second_drift = json.loads(json.dumps(FakeAgent.tool_contract))
    second_drift[0]["function"]["parameters"]["properties"]["path"]["type"] = "integer"
    FakeAgent.tool_contract = second_drift
    calls_before_pause = len(FakeAgent.calls)
    result = run_job(after_first)
    paused = get_job(job["id"])

    assert result[0] is True
    assert paused is not None
    assert paused["enabled"] is False
    assert len(FakeAgent.calls) == calls_before_pause


def test_contract_fuse_survives_run_pipeline_and_manual_resume_resets_it(
    persistent_env,
):
    job = _create_persistent_job()
    assert run_one_job(job) is True

    first_drift = json.loads(json.dumps(FakeAgent.tool_contract))
    first_drift[0]["function"]["parameters"]["required"] = []
    FakeAgent.tool_contract = first_drift
    first = get_job(job["id"])
    assert first is not None
    assert run_one_job(first) is True

    second_drift = json.loads(json.dumps(FakeAgent.tool_contract))
    second_drift[0]["function"]["parameters"]["properties"]["path"]["type"] = "integer"
    FakeAgent.tool_contract = second_drift
    before_pause = get_job(job["id"])
    assert before_pause is not None
    calls_before_pause = len(FakeAgent.calls)
    assert run_one_job(before_pause) is True
    paused = get_job(job["id"])

    assert paused is not None
    assert paused["enabled"] is False
    assert paused["state"] == "paused"
    assert paused["persistent_contract_forks"] == 2
    assert paused["repeat"]["completed"] == 3
    assert len(FakeAgent.calls) == calls_before_pause

    resumed = resume_job(job["id"])
    assert resumed is not None
    assert resumed["enabled"] is True
    assert "persistent_contract_forks" not in resumed


def test_system_contract_drift_forks_without_reinjecting_old_history(persistent_env):
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    original_root = get_job(job["id"])["session_root_id"]

    FakeAgent.system_contract = "changed-soul-or-project-context"
    assert run_job(get_job(job["id"]))[0] is True

    updated = get_job(job["id"])
    assert updated["session_root_id"] != original_root
    assert FakeAgent.calls[-1]["history"] == []
    assert "CRON CONTINUATION" not in FakeAgent.calls[-1]["prompt"]


def test_bundle_member_availability_is_part_of_persistent_skill_contract(monkeypatch):
    bundle_result = {
        "value": ("first loaded body", ["member-a"], ["member-b"]),
    }
    monkeypatch.setattr(
        "agent.skill_bundles.resolve_bundle_command_key",
        lambda _name: "/demo",
    )
    def _build_bundle(*_args, **kwargs):
        assert kwargs["return_member_ids"] is True
        return bundle_result["value"]

    monkeypatch.setattr(
        "agent.skill_bundles.build_bundle_invocation_message",
        _build_bundle,
    )

    partial = _persistent_skill_contract({"id": "job-1", "skills": ["/demo"]})
    bundle_result["value"] = (
        "different loaded body",
        ["member-a", "member-b"],
        [],
    )
    complete = _persistent_skill_contract({"id": "job-1", "skills": ["/demo"]})

    assert partial == [
        {
            "name": "bundle:/demo",
            "state": "bundle",
            "loaded": ["member-a"],
            "missing": ["member-b"],
        }
    ]
    assert complete == [
        {
            "name": "bundle:/demo",
            "state": "bundle",
            "loaded": ["member-a", "member-b"],
            "missing": [],
        }
    ]
    assert "loaded body" not in json.dumps(partial)
    assert partial != complete


def test_absolute_skill_body_change_keeps_persistent_root(
    persistent_env,
    monkeypatch,
    tmp_path,
):
    skills_dir = tmp_path / "skills"
    absolute_skill = skills_dir / "demo-skill"
    seen_names = []
    skill_body = {"content": "version one"}

    def _view_skill(name):
        seen_names.append(name)
        return json.dumps({"success": True, "content": skill_body["content"]})

    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("tools.skills_tool.skill_view", _view_skill)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda _name: None)
    job = create_job(
        prompt="Continue with the loaded procedure.",
        schedule="every 1m",
        model="test-model",
        provider="openrouter",
        skills=[str(absolute_skill)],
        session_mode="persistent",
    )

    assert run_job(job)[0] is True
    original_root = get_job(job["id"])["session_root_id"]
    assert "version one" in FakeAgent.calls[-1]["prompt"]
    assert seen_names
    assert set(seen_names) == {"demo-skill"}

    skill_body["content"] = "version two"
    seen_names.clear()
    assert run_job(get_job(job["id"]))[0] is True

    assert get_job(job["id"])["session_root_id"] == original_root
    resumed = FakeAgent.calls[-1]
    assert resumed["session_id"] == original_root
    assert "CRON CONTINUATION" in resumed["prompt"]
    assert "version two" not in resumed["prompt"]
    assert "version one" in resumed["history"][0]["content"]
    assert seen_names
    assert set(seen_names) == {"demo-skill"}


def test_configured_skill_identity_change_forks_persistent_root(
    persistent_env,
    monkeypatch,
):
    monkeypatch.setattr(
        "tools.skills_tool.skill_view",
        lambda name: json.dumps({"success": True, "content": f"body for {name}"}),
    )
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda _name: None)
    job = create_job(
        prompt="Continue with the loaded procedure.",
        schedule="every 1m",
        model="test-model",
        provider="openrouter",
        skills=["demo-skill"],
        session_mode="persistent",
    )

    assert run_job(job)[0] is True
    original_root = get_job(job["id"])["session_root_id"]

    update_job(job["id"], {"skills": ["replacement-skill"]})
    assert run_job(get_job(job["id"]))[0] is True

    assert get_job(job["id"])["session_root_id"] != original_root
    assert FakeAgent.calls[-1]["history"] == []
    assert "body for replacement-skill" in FakeAgent.calls[-1]["prompt"]


def test_configured_skill_availability_change_forks_persistent_root(
    persistent_env,
    monkeypatch,
):
    available = {"value": False}

    def _view_skill(_name):
        if available["value"]:
            return json.dumps({"success": True, "content": "now available"})
        return json.dumps({"success": False, "error": "missing"})

    monkeypatch.setattr("tools.skills_tool.skill_view", _view_skill)
    monkeypatch.setattr("tools.skill_usage.bump_use", lambda _name: None)
    job = create_job(
        prompt="Continue with the loaded procedure.",
        schedule="every 1m",
        model="test-model",
        provider="openrouter",
        skills=["demo-skill"],
        session_mode="persistent",
    )

    assert run_job(job)[0] is True
    original_root = get_job(job["id"])["session_root_id"]

    available["value"] = True
    assert run_job(get_job(job["id"]))[0] is True

    assert get_job(job["id"])["session_root_id"] != original_root
    assert "now available" in FakeAgent.calls[-1]["prompt"]


def test_orphaned_pointer_forks_before_bootstrap(persistent_env):
    job = _create_persistent_job()
    set_persistent_session_state(job["id"], "missing-root", "old-fingerprint")

    assert run_job(get_job(job["id"]))[0] is True

    stored = get_job(job["id"])
    assert stored["session_root_id"] != "missing-root"
    assert FakeAgent.calls[-1]["history"] == []
    assert "CRON CONTINUATION" not in FakeAgent.calls[-1]["prompt"]


def test_orphaned_pointer_with_contract_drift_logs_changed_components(
    persistent_env,
    caplog,
):
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    stored = get_job(job["id"])
    assert stored is not None
    root = stored["session_root_id"]
    persistent_env.sessions.pop(root)
    persistent_env.messages.pop(root)
    changed = json.loads(json.dumps(FakeAgent.tool_contract))
    changed[0]["function"]["parameters"]["required"] = []
    FakeAgent.tool_contract = changed

    with caplog.at_level("WARNING"):
        assert run_job(stored)[0] is True

    assert "changed_components=['tool_contract']" in caplog.text


def test_two_consecutive_orphaned_roots_pause_before_third_bootstrap(
    persistent_env,
):
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    first = get_job(job["id"])
    assert first is not None
    first_root = first["session_root_id"]
    persistent_env.sessions.pop(first_root)
    persistent_env.messages.pop(first_root)

    assert run_job(first)[0] is True
    replacement = get_job(job["id"])
    assert replacement is not None
    assert replacement["persistent_contract_forks"] == 1
    replacement_root = replacement["session_root_id"]
    persistent_env.sessions.pop(replacement_root)
    persistent_env.messages.pop(replacement_root)
    calls_before_pause = len(FakeAgent.calls)

    result = run_job(replacement)
    paused = get_job(job["id"])

    assert result[0] is True
    assert paused is not None
    assert paused["enabled"] is False
    assert paused["persistent_contract_forks"] == 2
    assert paused["session_root_id"] == replacement_root
    assert len(FakeAgent.calls) == calls_before_pause


def test_crashed_user_tail_is_not_replayed_on_retry(persistent_env):
    job = _create_persistent_job()
    FakeAgent.fail_next = True

    failed = run_job(job)
    failed_state = get_job(job["id"])
    failed_root = failed_state["session_root_id"]
    retried = run_job(failed_state)
    recovered = get_job(job["id"])

    assert failed[0] is False
    assert retried[0] is True
    assert recovered["session_root_id"] != failed_root
    retry_call = FakeAgent.calls[-1]
    assert retry_call["history"] == []
    assert "Implement the next verified milestone." in retry_call["prompt"]


def test_compression_tip_is_resolved_while_job_keeps_stable_root(persistent_env):
    job = _create_persistent_job()
    assert run_job(job)[0] is True
    stored = get_job(job["id"])
    root = stored["session_root_id"]
    child = f"{root}_compressed"
    persistent_env.sessions[root]["end_reason"] = "compression"
    persistent_env.sessions[child] = {
        "id": child,
        "parent_session_id": root,
        "system_prompt": "stable",
        "end_reason": "cron_waiting",
    }
    persistent_env.messages[child] = [
        {"role": "user", "content": "Compressed summary of prior work."},
        {"role": "assistant", "content": "Ready for the next milestone."},
    ]
    persistent_env.tips[root] = child

    assert run_job(get_job(job["id"]))[0] is True

    resumed = FakeAgent.calls[-1]
    assert resumed["session_id"] == child
    assert resumed["history"][0]["content"] == "Compressed summary of prior work."
    assert get_job(job["id"])["session_root_id"] == root
    assert persistent_env.reopened[-1] == child


def test_replay_cleanup_marks_interrupted_side_effect_unknown_and_preserves_context():
    store = FakeSessionStore()
    store.sessions["root"] = {"id": "root"}
    store.messages["root"] = [
        {"role": "user", "content": "bootstrap"},
        {"role": "assistant", "content": "prior milestone"},
        {"role": "user", "content": "continue"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal"}}],
        },
    ]

    tip, history = _load_persistent_cron_history(store, "root")

    assert tip == "root"
    assert history[:4] == [
        {"role": "user", "content": "bootstrap"},
        {"role": "assistant", "content": "prior milestone"},
        {"role": "user", "content": "continue"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal"}}],
        },
    ]
    recovered = history[-1]
    assert recovered["role"] == "tool"
    assert recovered["tool_call_id"] == "call-1"
    assert recovered["effect_disposition"] == "unknown"
    assert "effect is UNKNOWN" in recovered["content"]


def test_real_session_db_marks_interrupted_side_effect_unknown_after_reopen(tmp_path):
    db_path = tmp_path / "state.db"
    db = RealSessionDB(db_path)
    db.create_session("root", "cron", system_prompt="stable")
    db.append_message("root", "user", "bootstrap")
    db.append_message("root", "assistant", "prior milestone")
    db.append_message("root", "user", "continue")
    db.append_message(
        "root",
        "assistant",
        tool_calls=[{"id": "call-1", "function": {"name": "terminal"}}],
    )
    db.close()

    reopened = RealSessionDB(db_path)
    try:
        tip, history = _load_persistent_cron_history(reopened, "root")
    finally:
        reopened.close()

    assert tip == "root"
    assert [(item["role"], item["content"]) for item in history[:4]] == [
        ("user", "bootstrap"),
        ("assistant", "prior milestone"),
        ("user", "continue"),
        ("assistant", None),
    ]
    recovered = history[-1]
    assert recovered["role"] == "tool"
    assert recovered["tool_call_id"] == "call-1"
    assert recovered["effect_disposition"] == "unknown"
    assert "effect is UNKNOWN" in recovered["content"]


def test_real_session_db_soft_deletes_failed_user_tail_before_retry(tmp_path):
    db = RealSessionDB(tmp_path / "state.db")
    try:
        db.create_session("root", "cron", system_prompt="stable")
        db.append_message("root", "user", "bootstrap")
        db.append_message("root", "assistant", "prior milestone")
        failed_message_id = db.append_message("root", "user", "failed continuation")

        tip, history = _load_persistent_cron_history(db, "root")

        assert tip == "root"
        assert [(item["role"], item["content"]) for item in history] == [
            ("user", "bootstrap"),
            ("assistant", "prior milestone"),
        ]
        failed_row = next(
            row
            for row in db.get_messages("root", include_inactive=True)
            if row["id"] == failed_message_id
        )
        assert failed_row["active"] == 0

        db.append_message("root", "user", "replacement continuation")
        db.append_message("root", "assistant", "replacement complete")
        _, replay = _load_persistent_cron_history(db, "root")

        assert [(item["role"], item["content"]) for item in replay] == [
            ("user", "bootstrap"),
            ("assistant", "prior milestone"),
            ("user", "replacement continuation"),
            ("assistant", "replacement complete"),
        ]
    finally:
        db.close()


def test_real_session_db_resolves_compression_tip(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("root", "cron", system_prompt="stable")
        db.append_message("root", "user", "bootstrap")
        db.append_message("root", "assistant", "old context")
        db.end_session("root", "compression")
        db.create_session(
            "compressed",
            "cron",
            system_prompt="stable",
            parent_session_id="root",
        )
        db.append_message("compressed", "user", "compact summary")
        db.append_message("compressed", "assistant", "ready")
        db.end_session("compressed", "cron_waiting")

        tip, history = _load_persistent_cron_history(db, "root")

        assert tip == "compressed"
        assert [message["content"] for message in history] == [
            "compact summary",
            "ready",
        ]
    finally:
        db.close()
