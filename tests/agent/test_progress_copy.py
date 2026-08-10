import pytest

from agent.progress_copy import (
    DEFAULT_HUMAN_PROGRESS_TEXT,
    sanitize_human_progress_text,
)


def test_human_progress_copy_keeps_operational_language():
    text = "Terminado: el análisis. Ahora: valido el flujo. Siguiente: entrega."
    assert sanitize_human_progress_text(text) == text


@pytest.mark.parametrize(
    "unsafe",
    [
        "Ahora: reviso /private/repo/gateway/run.py.",
        "Ahora: ejecuto `pytest tests/gateway -q`.",
        "Ahora: uso Claude Fable mediante el provider OAuth.",
        "Ahora: leo el traceback y los logs de stderr.",
        "Ahora: valido task_id t_1234abcd y run_id 42.",
        "Ahora: consulto https://internal.example.test/debug.",
    ],
)
def test_human_progress_copy_rejects_technical_internals(unsafe):
    assert sanitize_human_progress_text(unsafe) == ""


def test_default_human_progress_copy_is_safe():
    assert sanitize_human_progress_text(DEFAULT_HUMAN_PROGRESS_TEXT)
