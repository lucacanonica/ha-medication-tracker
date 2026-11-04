"""Repository hygiene: shipped YAML parses, dashboards match the backend,
and nothing installation-specific leaks into the public files.

The privacy checks look for *patterns* (hex ids, non-generic entity names,
real dashboard paths), never for specific values.
"""
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "appdaemon" / "apps"))
import medication_tracker as mt  # noqa: E402

YAML_FILES = sorted(
    list((ROOT / "dashboard").glob("*.yaml"))
    + [ROOT / "homeassistant" / "scripts.yaml", ROOT / "appdaemon" / "apps" / "medication_tracker.yaml"]
    + list((ROOT / ".github" / "workflows").glob("*.yml"))
)
TEXT_FILES = [p for p in ROOT.rglob("*")
              if p.is_file() and p.suffix in (".py", ".yaml", ".yml", ".md")
              and ".git/" not in str(p) and "__pycache__" not in str(p)]

HANDLED_SOURCES = {"tap", "confirm", "form", "form_panel", "open_form",
                   "temp", "temp_panel", "temp_open_form", "temp_quick_form"}


def load(path):
    return yaml.safe_load(path.read_text())


def walk(node):
    """Yield every dict in a nested YAML structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


# --- 1. every shipped YAML parses -------------------------------------------------

@pytest.mark.parametrize("path", YAML_FILES, ids=lambda p: p.name)
def test_yaml_parses(path):
    assert load(path) is not None


# --- 2. dashboards match the backend ---------------------------------------------------

def dashboard_dicts():
    for path in (ROOT / "dashboard").glob("*.yaml"):
        for d in walk(load(path)):
            yield path.name, d


def test_dashboard_sources_are_handled_by_the_app():
    for name, d in dashboard_dicts():
        if "source" in d and "browser_id" in d:
            assert d["source"] in HANDLED_SOURCES, f"{name}: unknown source {d['source']}"


def test_dashboard_entities_exist_in_example_config():
    cfg = load(ROOT / "appdaemon" / "apps" / "medication_tracker.yaml")["medication_tracker"]
    expected = {f"sensor.medication_{m}" for m in cfg["medications"]}
    expected |= {mt.TEMP_ENTITY, mt.TIMELINE_ENTITY}
    for name, d in dashboard_dicts():
        ent = d.get("entity")
        if isinstance(ent, str) and ent.startswith("sensor.medication_"):
            assert ent in expected, f"{name}: {ent} has no medication in the example config"
        med = d.get("medication")
        if isinstance(med, str) and med:
            assert med in cfg["medications"], f"{name}: medication '{med}' not in example config"


def test_helpers_match_between_config_and_script():
    cfg = load(ROOT / "appdaemon" / "apps" / "medication_tracker.yaml")["medication_tracker"]
    temp = {**mt.TEMP_DEFAULTS, **(cfg.get("temperature") or {})}
    script = (ROOT / "homeassistant" / "scripts.yaml").read_text()
    for key in ("entry_helper", "dose_helper", "hours_ago_helper"):
        assert temp[key] in script, f"script does not read {temp[key]}"


def test_script_and_app_agree_on_event_name():
    script = load(ROOT / "homeassistant" / "scripts.yaml")["medication_record"]
    events = [s["event"] for s in script["sequence"] if "event" in s]
    assert events == [mt.EVENT]


# --- 3. nothing installation-specific in public files -----------------------------------

@pytest.mark.parametrize("path", TEXT_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_installation_specific_values(path):
    if path.resolve() == Path(__file__).resolve():
        return  # this file contains the patterns themselves
    text = path.read_text()
    rel = str(path.relative_to(ROOT))
    # HA user ids / long hex tokens (the example config shows a fake pattern, allowed)
    for m in re.finditer(r"\b[0-9a-f]{32}\b", text):
        assert m.group(0) in ("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210"), \
            f"{rel}: looks like a real id: {m.group(0)}"
    # weight helper must be the generic one
    for m in re.finditer(r"input_number\.\w*weight\w*", text):
        assert m.group(0) == "input_number.child_weight_lb", f"{rel}: {m.group(0)}"
    # dashboard navigation paths must be the placeholder
    for m in re.finditer(r"navigation_path:\s*(\S+)", text):
        assert m.group(1).startswith("/YOUR-DASHBOARD"), f"{rel}: real dashboard path {m.group(1)}"
    # no archive / test database paths
    assert "medication_db_archive" not in text and "_TEST_V" not in text, f"{rel}: test db path"
    # no e-mail addresses
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text.replace("noreply@anthropic.com", "")), \
        f"{rel}: e-mail address"
