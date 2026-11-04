"""Safety invariants for the published repo.

These are not about arithmetic. They make CI go red if anyone commits a real
dose value into the example config, lets an unedited config record anything,
or loosens the hard caps.
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "appdaemon" / "apps"))
import medication_tracker as mt  # noqa: E402

EXAMPLE_YAML = ROOT / "appdaemon" / "apps" / "medication_tracker.yaml"
NOW = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("America/Los_Angeles")


def example_config():
    return yaml.safe_load(EXAMPLE_YAML.read_text())["medication_tracker"]


@pytest.fixture
def shipped(tmp_path):
    """The app running on the example config exactly as shipped."""
    return mt.Core(str(tmp_path / "s.db"), example_config(), TZ, lambda *a, **k: None)


# 1. The repo ships no dosing numbers -----------------------------------------

def test_example_config_contains_no_dose_values():
    cfg = example_config()
    for med, m in cfg["medications"].items():
        for row in m.get("dose_chart", []):
            for key in ("dose_ml", "dose_mg"):
                if key in row:
                    assert float(row[key]) == 0, f"{med}: {row} ships a real dose"
        if "fixed_dose_ml" in m:
            assert float(m["fixed_dose_ml"]) == 0, f"{med}: fixed_dose_ml ships a real dose"


def test_example_config_has_disclaimer_and_placeholder_markers():
    text = EXAMPLE_YAML.read_text()
    assert "DISCLAIMER" in text and "not medical advice" in text.lower()
    assert "PLACEHOLDER" in text


def test_readme_has_disclaimer_near_the_top():
    readme = (ROOT / "README.md").read_text()
    assert "Not medical advice" in readme[:600]
    assert re.search(r"^## Disclaimer", readme, re.M)


# 2. An unedited config can't record anything --------------------------------------

@pytest.mark.parametrize("weight", ["21", "40", "unknown"])
def test_shipped_config_refuses_every_tap(shipped, weight):
    for med in example_config()["medications"]:
        kind, _ = shipped.handle({"source": "tap", "medication": med}, weight, NOW)
        assert kind == "rejected", f"{med} recorded a dose from the shipped config"
        state, attrs = shipped.status(med, weight, NOW)
        assert state == "unknown" and attrs["summary"].startswith("Check:")


def test_shipped_config_refuses_form_and_confirm_too(shipped):
    for med in example_config()["medications"]:
        for source in ("form", "confirm", "form_panel"):
            kind, _ = shipped.handle({"source": source, "medication": med,
                                      "volume_ml": 2.5, "administered_at": "now"}, "21", NOW)
            assert kind == "rejected", f"{med}/{source} recorded from the shipped config"


# 3. The app never invents a dose ----------------------------------------------------

def configured(tmp_path):
    cfg = example_config()
    t = cfg["medications"]["tylenol"]
    t["dose_chart"] = [{"min_lb": 18, "max_lb_exclusive": 24, "dose_ml": 3.75}]
    t["max_volume_ml"] = 10
    return mt.Core(str(tmp_path / "c.db"), cfg, TZ, lambda *a, **k: None)


def test_recorded_amount_is_exactly_config_or_user_input(tmp_path):
    core = configured(tmp_path)
    kind, r = core.handle({"source": "tap", "medication": "tylenol"}, "21", NOW)
    assert kind == "recorded" and r["volume_ml"] == 3.75          # exactly the chart value
    kind, r = core.handle({"source": "form", "medication": "tylenol", "volume_ml": 2.5,
                           "administered_at": "now"}, "21", NOW + timedelta(hours=5))
    assert kind == "recorded" and r["volume_ml"] == 2.5           # exactly what was typed
    # mg is only the label conversion, never a different quantity
    assert r["amount_mg"] == 2.5 * 32


def test_no_dose_outside_the_configured_weight_bracket(tmp_path):
    core = configured(tmp_path)
    for weight in ("17.9", "24", "60"):
        kind, msg = core.handle({"source": "tap", "medication": "tylenol"}, weight, NOW)
        assert kind == "rejected" and "No tylenol chart row" in msg


# 4. Caps are hard ----------------------------------------------------------------------

@pytest.mark.parametrize("source", ["form", "confirm", "form_panel"])
def test_cap_cannot_be_exceeded_by_any_path(tmp_path, source):
    core = configured(tmp_path)
    kind, msg = core.handle({"source": source, "medication": "tylenol", "volume_ml": 10.25,
                             "administered_at": "now"}, "21", NOW)
    assert kind == "rejected" and "safety cap" in msg


def test_temperature_bounds_are_hard(tmp_path):
    core = configured(tmp_path)
    for value in (92.9, 108.1, 120):
        kind, _ = core.handle({"source": "temp", "temp_f": value}, "21", NOW)
        assert kind == "rejected"


def test_override_never_bypasses_non_overridable_rejections(tmp_path):
    core = configured(tmp_path)
    bad = [
        {"volume_ml": 0},                                   # zero amount
        {"volume_ml": 3.75, "administered_at": (NOW + timedelta(hours=1)).isoformat()},  # future
        {"volume_ml": 3.75, "administered_at": (NOW - timedelta(days=8)).isoformat()},   # too old
    ]
    for extra in bad:
        kind, _ = core.handle({"source": "form", "medication": "tylenol",
                               "administered_at": "now", **extra}, "21", NOW)
        assert kind == "rejected", extra
