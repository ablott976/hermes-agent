"""Central creation and activation contract for cron jobs.

New jobs carry a versioned validation record. Persistent development jobs that
fail the contract are stored as paused drafts with a real ID, while pre-rollout
jobs remain logically exempt and are never rewritten merely because they were
read. Activation always rechecks post-rollout jobs so direct ``jobs.json``
drift cannot make an invalid draft tick.
"""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CONTRACT_VERSION = 1
INVALID_DRAFT_GRACE_SECONDS = 30 * 60

VALIDATION_VALID = "valid"
VALIDATION_VALID_DRAFT = "valid_draft"
VALIDATION_INVALID_DRAFT = "invalid_draft"
VALIDATION_LEGACY_EXEMPT = "legacy_exempt"
VALIDATION_DRIFT = "drift"

JOB_ID_PLACEHOLDER = "{{JOB_ID}}"
OWNER_PROFILE_PLACEHOLDER = "{{OWNER_PROFILE}}"
PAUSE_TECHNICAL_VALIDATION_TOKEN = "PAUSE_TECHNICAL_VALIDATION_V1"

REQUIRED_DEVELOPMENT_TOOLSETS = frozenset({"terminal", "file", "skills"})
FORBIDDEN_DEVELOPMENT_TOOLSETS = frozenset({"cronjob", "messaging", "clarify"})

_VALIDATION_CODE_ORDER = (
    "E_PLACEHOLDER_UNRESOLVED",
    "E_CREATION_REGISTRY_INCOMPLETE",
    "E_VALIDATION_RECORD_MISSING",
    "E_VALIDATION_RECORD_DRIFT",
    "E_VALIDATION_STATUS_DRIFT",
    "E_VALIDATION_KIND_DRIFT",
    "E_CONTRACT_KIND_DRIFT",
    "E_EXECUTABLE_CONTENT_MISSING",
    "E_SCRIPT_REQUIRED",
    "E_SCRIPT_NOT_READABLE",
    "E_NO_AGENT_PERSISTENT",
    "E_NO_AGENT_MODEL_CONFIG",
    "E_SCHEDULE_NOT_1M",
    "E_WORKDIR_NOT_ABSOLUTE",
    "E_WORKDIR_NOT_FOUND",
    "E_SKILLS_NOT_EMPTY",
    "E_TOOLSET_MISSING",
    "E_TOOLSET_FORBIDDEN",
    "E_PLAN_PATH_INVALID",
    "E_STATE_PATH_INVALID",
    "E_NO_RECURSION_CLAUSE_MISSING",
    "E_LOCAL_ARTIFACT_PROTECTION_MISSING",
    "E_SELF_PAUSE_MISSING",
    "E_PAUSE_TOKEN_MISSING",
    "E_DRAFT_NOT_ACTIVATED",
)
_CODE_RANK = {code: index for index, code in enumerate(_VALIDATION_CODE_ORDER)}

_UNRESOLVED_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(?:JOB_ID|OWNER_PROFILE)\s*\}\}",
    re.IGNORECASE,
)
_POSIX_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(/[^\r\n`\"']+?\.(?:md|json))(?=$|[\s`\"'\)\],;:])",
    re.IGNORECASE,
)
_WINDOWS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z]:[\\/][^\r\n`\"']+?\.(?:md|json))(?=$|[\s`\"'\)\],;:])",
    re.IGNORECASE,
)
_NO_RECURSION_RE = re.compile(
    r"(?:\bno\s+recursive\s+crons?\b|\bno\s+cron(?:s)?\s+recursiv[oa]s?\b|"
    r"\b(?:do\s+not|must\s+not|never|no)\b.{0,90}\b(?:create|schedule|program|crear|programar)\b"
    r".{0,90}\b(?:recursive\s+crons?|cron(?:s)?\s+recursiv[oa]s?)\b)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    @property
    def valid(self) -> bool:
        return not self.issues


class CronValidationBlocked(ValueError):
    """Raised when a cron job cannot be created or activated safely."""

    def __init__(self, issues: Iterable[ValidationIssue], *, prefix: str = "Cron validation failed"):
        ordered = _ordered_issues(issues)
        self.issues = ordered
        self.codes = tuple(issue.code for issue in ordered)
        detail = "; ".join(f"{issue.code}: {issue.message}" for issue in ordered)
        super().__init__(f"{prefix}: {detail}")


def owner_profile_for_home(owner_home: Path) -> str:
    """Return the owning profile name from a profile-scoped Hermes home."""
    home = Path(owner_home).expanduser().resolve()
    if home.parent.name == "profiles" and home.name:
        return home.name
    return "default"


def substitute_job_placeholders(value: Any, *, job_id: str, owner_profile: str) -> Any:
    """Substitute only the two explicit cron placeholders, recursively."""
    if isinstance(value, str):
        return value.replace(JOB_ID_PLACEHOLDER, job_id).replace(
            OWNER_PROFILE_PLACEHOLDER,
            owner_profile,
        )
    if isinstance(value, list):
        return [
            substitute_job_placeholders(item, job_id=job_id, owner_profile=owner_profile)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            substitute_job_placeholders(item, job_id=job_id, owner_profile=owner_profile)
            for item in value
        )
    if isinstance(value, dict):
        return {
            key: substitute_job_placeholders(item, job_id=job_id, owner_profile=owner_profile)
            for key, item in value.items()
        }
    return value


def registry_path(owner_home: Path) -> Path:
    return Path(owner_home) / "cron" / "creation_contract.json"


def registered_job_kinds(owner_home: Path) -> dict[str, str]:
    path = registry_path(owner_home)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        raise ValueError("E_CREATION_CONTRACT_REGISTRY_INVALID")
    if not isinstance(payload, Mapping) or payload.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("E_CREATION_CONTRACT_REGISTRY_INVALID")
    values = payload.get("job_kinds")
    if not isinstance(values, Mapping):
        raise ValueError("E_CREATION_CONTRACT_REGISTRY_INVALID")
    return {
        str(job_id): str(kind)
        for job_id, kind in values.items()
        if str(job_id) and str(kind)
    }


def registered_job_statuses(owner_home: Path) -> dict[str, str]:
    path = registry_path(owner_home)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        raise ValueError("E_CREATION_CONTRACT_REGISTRY_INVALID")
    if not isinstance(payload, Mapping) or payload.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("E_CREATION_CONTRACT_REGISTRY_INVALID")
    values = payload.get("job_statuses")
    if not isinstance(values, Mapping):
        raise ValueError("E_CREATION_CONTRACT_REGISTRY_INVALID")
    return {
        str(job_id): str(status)
        for job_id, status in values.items()
        if str(job_id) and str(status)
    }


def registered_job_ids(owner_home: Path) -> set[str]:
    return set(registered_job_kinds(owner_home)) | set(registered_job_statuses(owner_home))


def register_post_rollout_job(
    job_id: str,
    owner_home: Path,
    *,
    contract_kind: str,
    validation_status: str,
) -> None:
    value = str(job_id or "").strip()
    if not value:
        raise ValueError("cron creation contract registry requires a job ID")
    kind = str(contract_kind or "").strip()
    if kind not in {"fresh", "no_agent", "persistent_development"}:
        raise ValueError("cron creation contract registry requires a valid contract kind")
    status = str(validation_status or "").strip()
    if status not in {VALIDATION_VALID, VALIDATION_VALID_DRAFT, VALIDATION_INVALID_DRAFT}:
        raise ValueError("cron creation contract registry requires a valid validation status")
    path = registry_path(owner_home)
    jobs = registered_job_kinds(owner_home)
    statuses = registered_job_statuses(owner_home)
    if jobs.get(value) == kind and statuses.get(value) == status:
        return
    jobs[value] = kind
    statuses[value] = status
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(
            json.dumps(
                {
                    "contract_version": CONTRACT_VERSION,
                    "job_kinds": {
                        registered_id: jobs[registered_id]
                        for registered_id in sorted(jobs)
                    },
                    "job_statuses": {
                        registered_id: statuses[registered_id]
                        for registered_id in sorted(statuses)
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def is_post_rollout_job(
    job: Mapping[str, Any],
    *,
    owner_home: Path | None = None,
) -> bool:
    validation = job.get("validation")
    if (
        isinstance(validation, Mapping)
        and validation.get("contract_version") == CONTRACT_VERSION
        and validation.get("status") != VALIDATION_LEGACY_EXEMPT
    ):
        return True
    job_id = str(job.get("id") or "")
    return bool(owner_home is not None and job_id in registered_job_ids(owner_home))


def contract_kind(job: Mapping[str, Any]) -> str:
    if bool(job.get("no_agent")):
        return "no_agent"
    if str(job.get("session_mode") or "fresh").strip().lower() == "persistent":
        return "persistent_development"
    return "fresh"


def resolved_script_path(script: Any, owner_home: Path) -> Path | None:
    raw = str(script or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(owner_home) / "scripts" / path
    return path.resolve()


def validate_job(
    job: Mapping[str, Any],
    *,
    owner_home: Path,
    owner_profile: str | None = None,
    allow_contract_kind_change: bool = False,
) -> ValidationResult:
    """Purely validate one post-rollout job without mutating storage."""
    profile = owner_profile or owner_profile_for_home(owner_home)
    issues: list[ValidationIssue] = []

    def add(code: str, message: str) -> None:
        if not any(issue.code == code for issue in issues):
            issues.append(ValidationIssue(code, message))

    if _contains_unresolved_placeholder(job):
        add(
            "E_PLACEHOLDER_UNRESOLVED",
            "Stored cron fields still contain an unresolved job/profile placeholder.",
        )

    stored_validation = job.get("validation")
    job_id = str(job.get("id") or "")
    registered_kind = registered_job_kinds(owner_home).get(job_id, "")
    registered_status = registered_job_statuses(owner_home).get(job_id, "")
    if bool(registered_kind) != bool(registered_status):
        add(
            "E_CREATION_REGISTRY_INCOMPLETE",
            "The authoritative registry must contain matching kind and status entries.",
        )
    if registered_kind and not (
        isinstance(stored_validation, Mapping)
        and stored_validation.get("contract_version") == CONTRACT_VERSION
    ):
        add(
            "E_VALIDATION_RECORD_MISSING",
            "A post-rollout cron is missing its versioned validation record.",
        )
    stored_kind = (
        str(stored_validation.get("contract_kind") or "")
        if isinstance(stored_validation, Mapping)
        else ""
    )
    if registered_kind and stored_kind and stored_kind != registered_kind:
        add(
            "E_VALIDATION_KIND_DRIFT",
            "Stored validation kind differs from the authoritative registry.",
        )
    expected_kind = registered_kind or stored_kind
    current_kind = contract_kind(job)
    if expected_kind and expected_kind != current_kind and not allow_contract_kind_change:
        add(
            "E_CONTRACT_KIND_DRIFT",
            "Stored cron fields changed the validated execution kind outside the core update path.",
        )

    prompt = str(job.get("prompt") or "")
    skills = _normalized_string_list(job.get("skills"))
    if not skills and str(job.get("skill") or "").strip():
        skills = [str(job.get("skill")).strip()]
    script = str(job.get("script") or "").strip()
    no_agent = bool(job.get("no_agent"))
    session_mode = str(job.get("session_mode") or "fresh").strip().lower()

    script_path = resolved_script_path(script, owner_home) if script else None
    script_readable = bool(
        script_path
        and script_path.is_file()
        and _is_readable_file(script_path)
    )

    if no_agent:
        if not script:
            add("E_SCRIPT_REQUIRED", "A no-agent cron requires a script.")
        elif not script_readable:
            add("E_SCRIPT_NOT_READABLE", "The resolved cron script is missing or unreadable.")
        if session_mode == "persistent":
            add(
                "E_NO_AGENT_PERSISTENT",
                "session_mode='persistent' cannot be used with no_agent=True.",
            )
        if any(str(job.get(field) or "").strip() for field in ("model", "provider", "base_url")):
            add(
                "E_NO_AGENT_MODEL_CONFIG",
                "A no-agent cron cannot carry model, provider, or base URL execution settings.",
            )
    else:
        if session_mode == "persistent" and not prompt.strip() and not skills and not script:
            add(
                "E_EXECUTABLE_CONTENT_MISSING",
                "An agent cron requires a prompt, at least one skill, or a valid script.",
            )

    if session_mode == "persistent" and not no_agent:
        schedule = job.get("schedule")
        if not (
            isinstance(schedule, Mapping)
            and schedule.get("kind") == "interval"
            and _safe_int(schedule.get("minutes")) == 1
        ):
            add("E_SCHEDULE_NOT_1M", "Persistent development jobs must use exactly every 1m.")

        raw_workdir = str(job.get("workdir") or "").strip()
        workdir = Path(raw_workdir).expanduser() if raw_workdir else None
        resolved_workdir: Path | None = None
        if workdir is None or not workdir.is_absolute():
            add("E_WORKDIR_NOT_ABSOLUTE", "Persistent development workdir must be absolute.")
        else:
            resolved_workdir = workdir.resolve()
            if not resolved_workdir.is_dir():
                add("E_WORKDIR_NOT_FOUND", "Persistent development workdir must exist as a directory.")

        if skills:
            add("E_SKILLS_NOT_EMPTY", "Persistent development jobs must store skills=[].")

        toolsets = set(_normalized_string_list(job.get("enabled_toolsets")))
        missing = sorted(REQUIRED_DEVELOPMENT_TOOLSETS - toolsets)
        forbidden = sorted(FORBIDDEN_DEVELOPMENT_TOOLSETS & toolsets)
        if missing:
            add("E_TOOLSET_MISSING", f"Missing required toolsets: {', '.join(missing)}.")
        if forbidden:
            add("E_TOOLSET_FORBIDDEN", f"Forbidden toolsets present: {', '.join(forbidden)}.")

        if not _has_valid_plan_path(prompt, resolved_workdir):
            add(
                "E_PLAN_PATH_INVALID",
                "Prompt must name an existing absolute .md plan inside the workdir.",
            )
        if not _has_valid_state_path(prompt, resolved_workdir, Path(owner_home).resolve()):
            add(
                "E_STATE_PATH_INVALID",
                "Prompt must name an existing absolute .json state inside the workdir or owner profile home.",
            )

        if not _NO_RECURSION_RE.search(prompt):
            add(
                "E_NO_RECURSION_CLAUSE_MISSING",
                "Prompt must explicitly prohibit recursive cron creation.",
            )

        if not _has_local_artifact_protection(prompt):
            add(
                "E_LOCAL_ARTIFACT_PROTECTION_MISSING",
                "Prompt must keep plans, state, funciones.txt, and scratch/notes local-only and out of Git.",
            )

        expected_pause = f"hermes --profile {profile} cron pause {job.get('id', '')}"
        pause_index = prompt.find(expected_pause)
        if pause_index < 0:
            add(
                "E_SELF_PAUSE_MISSING",
                "Prompt must contain the exact self-pause command for the owner profile and real job ID.",
            )
        token_index = prompt.find(PAUSE_TECHNICAL_VALIDATION_TOKEN)
        if token_index < 0 or (pause_index >= 0 and token_index > pause_index):
            add(
                "E_PAUSE_TOKEN_MISSING",
                "PAUSE_TECHNICAL_VALIDATION_V1 must appear before the self-pause command.",
            )

    return ValidationResult(_ordered_issues(issues))


def validation_record(
    result: ValidationResult,
    *,
    status: str,
    now: datetime,
    contract_kind: str,
    grace_until: datetime | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "contract_kind": contract_kind,
        "status": status,
        "codes": list(result.codes),
        "details": [
            {"code": issue.code, "message": issue.message}
            for issue in result.issues
        ],
        "validated_at": now.isoformat(),
    }
    if grace_until is not None:
        record["grace_until"] = grace_until.isoformat()
    return record


def invalid_draft_record(
    result: ValidationResult,
    *,
    now: datetime,
    contract_kind: str,
) -> dict[str, Any]:
    return validation_record(
        result,
        status=VALIDATION_INVALID_DRAFT,
        now=now,
        contract_kind=contract_kind,
        grace_until=now + timedelta(seconds=INVALID_DRAFT_GRACE_SECONDS),
    )


def legacy_exempt_record() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "contract_kind": "legacy",
        "status": VALIDATION_LEGACY_EXEMPT,
        "codes": [],
        "details": [],
    }


def validation_readback(job: Mapping[str, Any], *, owner_home: Path) -> dict[str, Any]:
    """Return logical validation state without mutating the source record."""
    stored = job.get("validation")
    if not is_post_rollout_job(job, owner_home=owner_home):
        return legacy_exempt_record()

    result = validate_job(job, owner_home=owner_home)
    view = copy.deepcopy(dict(stored)) if isinstance(stored, Mapping) else {}
    stored_codes = tuple(str(code) for code in view.get("codes", []) if str(code))
    registered_status = registered_job_statuses(owner_home).get(str(job.get("id") or ""))
    status_drift = bool(registered_status and view.get("status") != registered_status)
    if stored_codes != result.codes or status_drift:
        readback_issues = list(result.issues)
        if stored_codes != result.codes:
            readback_issues.append(
                ValidationIssue(
                    "E_VALIDATION_RECORD_DRIFT",
                    "Stored validation codes do not match current authoritative validation.",
                )
            )
        if status_drift:
            readback_issues.append(
                ValidationIssue(
                    "E_VALIDATION_STATUS_DRIFT",
                    "Stored validation status differs from the authoritative registry.",
                )
            )
        ordered = _ordered_issues(readback_issues)
        view["stored_status"] = view.get("status")
        view["status"] = VALIDATION_DRIFT
        view["codes"] = [issue.code for issue in ordered]
        view["details"] = [
            {"code": issue.code, "message": issue.message}
            for issue in ordered
        ]
    return view


def ensure_job_activatable(job: Mapping[str, Any], *, owner_home: Path) -> None:
    """Fail closed for invalid post-rollout jobs without rewriting storage."""
    from cron.lifecycle_guard import check_gateway_lifecycle

    check_gateway_lifecycle(job.get("prompt"), job.get("script"))
    if not is_post_rollout_job(job, owner_home=owner_home):
        return

    result = validate_job(job, owner_home=owner_home)
    if not result.valid:
        raise CronValidationBlocked(result.issues, prefix="Cron activation blocked")

    validation = job.get("validation")
    status = validation.get("status") if isinstance(validation, Mapping) else None
    registered_status = registered_job_statuses(owner_home).get(str(job.get("id") or ""))
    if registered_status and status != registered_status:
        raise CronValidationBlocked(
            (
                ValidationIssue(
                    "E_VALIDATION_STATUS_DRIFT",
                    "Stored validation status differs from the authoritative registry.",
                ),
            ),
            prefix="Cron activation blocked",
        )
    stored_codes = (
        tuple(str(code) for code in validation.get("codes", []) if str(code))
        if isinstance(validation, Mapping)
        else ()
    )
    if stored_codes != result.codes:
        raise CronValidationBlocked(
            (
                ValidationIssue(
                    "E_VALIDATION_RECORD_DRIFT",
                    "Stored validation codes do not match current authoritative validation.",
                ),
            ),
            prefix="Cron activation blocked",
        )
    if status in {VALIDATION_INVALID_DRAFT, VALIDATION_VALID_DRAFT}:
        raise CronValidationBlocked(
            (
                ValidationIssue(
                    "E_DRAFT_NOT_ACTIVATED",
                    "A draft must be explicitly resumed after its contract is repaired.",
                ),
            ),
            prefix="Cron activation blocked",
        )
    if status != VALIDATION_VALID:
        raise CronValidationBlocked(
            (
                ValidationIssue(
                    "E_VALIDATION_STATUS_INVALID",
                    "A post-rollout cron must have validation status 'valid' before activation.",
                ),
            ),
            prefix="Cron activation blocked",
        )


def _ordered_issues(issues: Iterable[ValidationIssue]) -> tuple[ValidationIssue, ...]:
    unique: dict[str, ValidationIssue] = {}
    for issue in issues:
        unique.setdefault(issue.code, issue)
    return tuple(
        sorted(
            unique.values(),
            key=lambda issue: (_CODE_RANK.get(issue.code, len(_CODE_RANK)), issue.code),
        )
    )


def _contains_unresolved_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_UNRESOLVED_PLACEHOLDER_RE.search(value))
    if isinstance(value, Mapping):
        return any(_contains_unresolved_placeholder(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_unresolved_placeholder(item) for item in value)
    return False


def _normalized_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, Sequence):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _is_readable_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _absolute_paths(prompt: str, suffix: str) -> list[Path]:
    matches = [*(_POSIX_PATH_RE.findall(prompt)), *(_WINDOWS_PATH_RE.findall(prompt))]
    paths: list[Path] = []
    for raw in matches:
        if not raw.lower().endswith(suffix):
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            paths.append(candidate.resolve())
    return paths


def _is_within(path: Path, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_valid_plan_path(prompt: str, workdir: Path | None) -> bool:
    if workdir is None:
        return False
    return any(
        path.is_file() and _is_within(path, workdir)
        for path in _absolute_paths(prompt, ".md")
    )


def _has_valid_state_path(prompt: str, workdir: Path | None, owner_home: Path) -> bool:
    return any(
        path.is_file() and (_is_within(path, workdir) or _is_within(path, owner_home))
        for path in _absolute_paths(prompt, ".json")
    )


def _has_local_artifact_protection(prompt: str) -> bool:
    folded = re.sub(r"\s+", " ", prompt.casefold())
    names_present = (
        ".hermes/plans" in folded
        and "state" in folded
        and "funciones.txt" in folded
        and any(token in folded for token in ("scratch", "notes", "notas"))
    )
    git_protection = (
        "local-only" in folded
        or "local only" in folded
        or "solo local" in folded
    ) and any(token in folded for token in ("stage", "commit", "push", "git"))
    explicit_prohibition = bool(
        re.search(
            r"(?:must\s+not|do\s+not|never|no)\b.{0,100}\b(?:stage|commit|push)\b",
            folded,
        )
    )
    return names_present and (git_protection or explicit_prohibition)
