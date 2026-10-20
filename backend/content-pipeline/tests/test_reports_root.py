"""_REPORTS_ROOT must follow WORK_DIR (Docker-independent), not /shared.

Loads report_renderer.py directly by file path: importing through the
app.educational_intelligence package pulls the heavy agent chain
(qdrant_client, torch), which unit tests must not require.
"""
import importlib.util
import os
from pathlib import Path

_RENDERER_PATH = (
    Path(__file__).resolve().parent.parent
    / "app"
    / "educational_intelligence"
    / "reports"
    / "report_renderer.py"
)


def _load_renderer():
    for mod in ("report_renderer_under_test",):
        sys_modules_pop(mod)
    spec = importlib.util.spec_from_file_location(
        "report_renderer_under_test", _RENDERER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sys_modules_pop(name):
    import sys

    sys.modules.pop(name, None)


def test_reports_root_follows_work_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    renderer = _load_renderer()
    assert str(renderer._REPORTS_ROOT) == str(tmp_path / "work" / "reports")
    assert "/shared" not in str(renderer._REPORTS_ROOT)


def test_reports_root_legacy_fallback(monkeypatch):
    monkeypatch.delenv("WORK_DIR", raising=False)
    # os.environ may still carry WORK_DIR from the dev shell; clear it too.
    old = os.environ.pop("WORK_DIR", None)
    try:
        renderer = _load_renderer()
        assert str(renderer._REPORTS_ROOT) == "/shared/work/reports"
    finally:
        if old is not None:
            os.environ["WORK_DIR"] = old
