"""Regression tests for hermetic cron storage during pytest collection."""

import json
import os
from pathlib import Path

# Deliberately import before the autouse fixture runs.  This reproduces the
# collection-order bug where cron.jobs cached a developer's live profile paths.
import cron.jobs as cron_jobs


def test_preimported_cron_store_is_reanchored_before_job_creation():
    expected_home = Path(os.environ["HERMES_HOME"]).resolve()
    expected_cron = expected_home / "cron"

    assert cron_jobs.HERMES_DIR == expected_home
    assert cron_jobs.CRON_DIR == expected_cron
    assert cron_jobs.JOBS_FILE == expected_cron / "jobs.json"
    assert cron_jobs.OUTPUT_DIR == expected_cron / "output"
    assert cron_jobs.TICKER_HEARTBEAT_FILE == expected_cron / "ticker_heartbeat"
    assert cron_jobs.TICKER_SUCCESS_FILE == expected_cron / "ticker_last_success"
    assert cron_jobs._current_cron_store().jobs_file == expected_cron / "jobs.json"

    job = cron_jobs.create_job(
        prompt="hermetic regression",
        schedule="every 5m",
        name="hermetic-cron-store",
    )

    stored = json.loads((expected_cron / "jobs.json").read_text(encoding="utf-8"))
    assert [item["id"] for item in stored["jobs"]] == [job["id"]]
