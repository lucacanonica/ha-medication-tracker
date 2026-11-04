import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appdaemon" / "apps"))
import medication_tracker as mt  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
CONFIG = {
    "enabled": True,
    "weight_entity": "input_number.child_weight_lb",
    "medications": {
        "tylenol": {
            "enabled": True, "default_product": "childrens_liquid",
            "products": {"childrens_liquid": {"concentration_mg_ml": 32}},
            "min_interval_hours": 4, "max_doses_24h": 5,
            "dose_chart": [
                {"min_lb": 12, "max_lb_exclusive": 18, "dose_ml": 2.5},
                {"min_lb": 18, "max_lb_exclusive": 24, "dose_ml": 3.75},
                {"min_lb": 24, "max_lb_exclusive": 36, "dose_ml": 5.0},
            ],
        },
        "ibuprofen": {
            "enabled": True, "default_product": "childrens_liquid",
            "products": {"infant_drops": {"concentration_mg_ml": 40},
                         "childrens_liquid": {"concentration_mg_ml": 20}},
            "min_interval_hours": 6, "max_doses_24h": 4,
            "dose_chart": [
                {"min_lb": 12, "max_lb_exclusive": 18, "dose_mg": 50},
                {"min_lb": 18, "max_lb_exclusive": 24, "dose_mg": 75},
                {"min_lb": 24, "max_lb_exclusive": 36, "dose_mg": 100},
            ],
        },
    },
}
CONFIG["medications"]["zyrtec"] = {
    "enabled": True, "default_product": "childrens_syrup",
    "products": {"childrens_syrup": {"concentration_mg_ml": 1}},
    "min_interval_hours": 24, "max_doses_24h": 1, "max_volume_ml": 5,
    "fixed_dose_ml": 2.5,
}
NOW = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)  # 15:00 PST
H = timedelta(hours=1)


def noop_log(*a, **k):
    pass


@pytest.fixture
def core(tmp_path):
    return mt.Core(str(tmp_path / "t.db"), CONFIG, TZ, noop_log)


def tap(core, med="tylenol", now=NOW, weight="21"):
    return core.handle({"source": "tap", "medication": med}, weight, now)


def confirm(core, when, med="tylenol", now=NOW, volume=None):
    data = {"source": "confirm", "medication": med,
            "administered_at": when.isoformat()}
    if volume is not None:
        data["volume_ml"] = volume
    return core.handle(data, "21", now)


# --- dose chart --------------------------------------------------------------

def test_default_doses_at_21_lb(core):
    assert core.default_dose("tylenol", "21")["dose_ml"] == 3.75
    assert core.default_dose("tylenol", "21")["dose_mg"] == 120
    ibu = core.default_dose("ibuprofen", "21")
    assert ibu["dose_ml"] == 3.75 and ibu["dose_mg"] == 75


def test_weight_problems_are_rejected(core):
    for w in (None, "unknown", "unavailable", "abc", "0", "100"):
        with pytest.raises(mt.Rejected):
            core.default_dose("tylenol", w)
    assert core.status("tylenol", "unknown", NOW)[0] == "unknown"


def test_fixed_dose_medication_ignores_weight(core):
    d = core.default_dose("zyrtec", "unknown")
    assert d["dose_ml"] == 2.5 and d["dose_mg"] == 2.5 and d["weight_lb"] is None
    state, a = core.status("zyrtec", "unknown", NOW)
    assert state == "eligible" and a["friendly_name"] == "Zyrtec · 2.5 mL"
    assert tap(core, "zyrtec", weight="unknown")[0] == "recorded"
    kind, info = tap(core, "zyrtec", now=NOW + 23 * H)
    assert kind == "confirm"
    assert core.status("zyrtec", "21", NOW + 24 * H + timedelta(seconds=1))[0] == "eligible"


# --- tap flow ------------------------------------------------------------------

def test_tap_records_default_dose_when_eligible(core):
    kind, r = tap(core)
    assert kind == "recorded"
    assert r["volume_ml"] == 3.75 and r["amount_mg"] == 120
    assert r["count_24h"] == 1 and r["notes"] == []
    assert core.status("tylenol", "21", NOW)[0] == "too_soon"


def test_tap_too_early_asks_for_confirmation_and_writes_nothing(core):
    tap(core)
    kind, info = tap(core, now=NOW + 2 * H)
    assert kind == "confirm"
    assert "interval" in info["reason"] and info["volume_ml"] == 3.75
    assert core.status("tylenol", "21", NOW + 2 * H)[1]["doses_24h"] == 1


def test_confirm_records_early_dose_with_note(core):
    tap(core)
    _, info = tap(core, now=NOW + 2 * H)
    kind, r = confirm(core, mt.parse_ts(info["administered_at"]), now=NOW + 2 * H)
    assert kind == "recorded"
    assert r["count_24h"] == 2 and r["notes"][0].startswith("overrode")


def test_double_log_from_two_windows_writes_once(core):
    tap(core)
    _, info = tap(core, now=NOW + 2 * H)
    when = mt.parse_ts(info["administered_at"])
    assert confirm(core, when, now=NOW + 2 * H)[0] == "recorded"
    kind, msg = confirm(core, when, now=NOW + 2 * H + timedelta(seconds=3))
    assert kind == "rejected" and "Already recorded" in msg
    assert core.status("tylenol", "21", NOW + 3 * H)[1]["doses_24h"] == 2


def test_unique_index_exists(core):
    with sqlite3.connect(core.db_path) as db:
        names = {r[1] for r in db.execute("PRAGMA index_list(doses)")}
    assert "ux_doses_med_ts" in names


def test_medications_have_separate_histories(core):
    tap(core, "tylenol")
    kind, _ = tap(core, "ibuprofen", now=NOW + timedelta(minutes=1))
    assert kind == "recorded"


# --- rolling 24 h limit, incl. after overrides -----------------------------------

def test_limit_blocks_sixth_dose_and_clears_when_fifth_oldest_expires(core):
    t = NOW
    for i in range(5):  # 5 doses, 4 h apart -> hits max_doses_24h=5
        assert tap(core, now=t + i * 4 * H)[0] == "recorded"
    now = t + 20 * H + timedelta(minutes=1)
    # interval fine (4 h since last) but 5/5 in window -> limit
    kind, info = tap(core, now=now)
    assert kind == "confirm" and "dose 6 of max 5" in info["reason"]
    state, attrs = core.status("tylenol", "21", now)
    assert state == "too_soon"
    assert attrs["next_allowed"] == (t + 24 * H).isoformat()
    # first dose expires at t+24h -> eligible again
    assert core.status("tylenol", "21", t + 24 * H + timedelta(seconds=1))[0] == "eligible"


def test_limit_after_override_waits_for_enough_doses_to_expire(core):
    t = NOW
    for i in range(5):
        tap(core, now=t + i * 4 * H)
    now = t + 20 * H + timedelta(minutes=1)
    _, info = tap(core, now=now)
    assert confirm(core, mt.parse_ts(info["administered_at"]), now=now)[0] == "recorded"
    state, attrs = core.status("tylenol", "21", now + timedelta(minutes=1))
    assert attrs["doses_24h"] == 6 and state == "too_soon"
    # Need count < 5: the 5th most recent (= dose #2 at t+4h) must expire,
    # and interval from the override dose (now+4h) must pass -> max of both.
    expected = max(t + 4 * H + 24 * H, now + 4 * H)
    assert attrs["next_allowed"] == expected.isoformat()
    assert core.status("tylenol", "21", expected + timedelta(seconds=1))[0] == "eligible"


def test_old_overridden_window_does_not_block_later_doses(core):
    """A crowded 24 h window in the past must not reject a dose outside it."""
    t = NOW - 3 * timedelta(days=1)
    for i in range(6):
        confirm(core, t + i * 4 * H, now=t + i * 4 * H)
    assert tap(core, now=NOW)[0] == "recorded"


# --- form / backdating -------------------------------------------------------------

def test_form_backdates_in_local_naive_time(core):
    kind, r = core.handle({"source": "form", "medication": "tylenol",
                           "volume_ml": 2.5, "administered_at": "2026-11-02 13:30:00"},
                          "21", NOW)
    assert kind == "recorded"
    assert r["when"] == datetime(2026, 11, 2, 21, 30, tzinfo=timezone.utc)
    assert r["volume_ml"] == 2.5 and r["amount_mg"] == 80


def test_form_accepts_aware_iso_and_blank_volume(core):
    kind, r = core.handle({"source": "form", "medication": "ibuprofen",
                           "administered_at": "2026-11-02T14:00:00-08:00"}, "21", NOW)
    assert kind == "recorded" and r["volume_ml"] == 3.75


def test_form_early_dose_is_recorded_not_blocked(core):
    tap(core)
    kind, r = core.handle({"source": "form", "medication": "tylenol",
                           "administered_at": "now"}, "21", NOW + H)
    assert kind == "recorded" and r["notes"]


def test_future_and_too_old_are_rejected(core):
    assert confirm(core, NOW + 10 * H)[0] == "rejected"
    assert confirm(core, NOW - 8 * timedelta(days=1))[0] == "rejected"


def test_volume_guardrails(core):
    assert confirm(core, NOW, volume=0)[0] == "rejected"
    assert confirm(core, NOW, volume="x")[0] == "rejected"
    kind, msg = confirm(core, NOW, volume=20)
    assert kind == "rejected" and "safety cap" in msg
    kind, r = confirm(core, NOW, volume=5)  # above chart, under 2x cap
    assert kind == "recorded" and "above chart dose" in r["notes"][0]


def test_open_form_prefills_dose_and_local_time(core):
    kind, info = core.handle({"source": "open_form", "medication": "tylenol"}, "21", NOW)
    assert kind == "form"
    assert info["volume_ml"] == 3.75 and info["local_now"] == "2026-11-02 15:00:00"


# --- status text ------------------------------------------------------------------

def test_status_summary_and_approaching(core):
    assert core.status("tylenol", "21", NOW)[1]["summary"] == "3.75 mL\nReady now\n0/5 in 24 h"
    tap(core)
    s, a = core.status("tylenol", "21", NOW + 3 * H + timedelta(minutes=10))
    assert s == "approaching" and a["summary"] == "3.75 mL\n50 min left\n1/5 in 24 h"
    s, a = core.status("tylenol", "21", NOW + timedelta(minutes=30))
    assert s == "too_soon" and a["summary"] == "3.75 mL\n3h 30m left\n1/5 in 24 h"
    assert a["friendly_name"] == "Tylenol · 3.75 mL"


def test_status_is_stable_between_minutes_except_summary(core):
    tap(core)
    _, a1 = core.status("tylenol", "21", NOW + timedelta(seconds=10))
    _, a2 = core.status("tylenol", "21", NOW + timedelta(seconds=40))
    assert a1 == a2  # same minute -> identical -> no republish


def test_disabled_config(tmp_path):
    cfg = {**CONFIG, "enabled": False}
    c = mt.Core(str(tmp_path / "d.db"), cfg, TZ, noop_log)
    assert tap(c)[0] == "rejected"
    assert c.status("tylenol", "21", NOW)[0] == "unknown"


def test_legacy_naive_rows_and_duplicates_survive_startup(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE doses (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "medication TEXT NOT NULL, timestamp TEXT NOT NULL, volume_ml REAL "
                   "NOT NULL, concentration_mg_ml REAL NOT NULL, amount_mg REAL NOT "
                   "NULL, caregiver TEXT NOT NULL)")
        for _ in range(2):  # duplicate rows from the old bug
            db.execute("INSERT INTO doses VALUES (NULL,'tylenol',?,3.75,32,120,'x')",
                       ((NOW - 2 * H).replace(tzinfo=None).isoformat(),))
    logs = []
    c = mt.Core(str(path), CONFIG, TZ, lambda m, **k: logs.append(m))
    assert any("duplicate" in m for m in logs)
    state, attrs = c.status("tylenol", "21", NOW)
    assert state == "too_soon" and attrs["doses_24h"] == 2
    # transactional check still blocks a new duplicate
    assert confirm(c, NOW - 2 * H)[0] == "rejected"


def test_concurrent_confirms_write_once(core):
    when = NOW
    results = []

    def go():
        results.append(confirm(core, when)[0])

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("recorded") == 1
    assert results.count("rejected") == 7


# --- temperature ---------------------------------------------------------------

def temp(core, value, when=None, now=NOW):
    data = {"source": "temp", "temp_f": value}
    if when is not None:
        data["administered_at"] = when if isinstance(when, str) else when.isoformat()
    return core.handle(data, "21", now)


def test_temperature_record_and_status(core):
    assert core.temperature_status(NOW)[0] == "unknown"
    kind, r = temp(core, 101.8)
    assert kind == "recorded_temp" and r["temp_f"] == 101.8 and r["level"] == "fever"
    state, a = core.temperature_status(NOW + timedelta(minutes=130))
    assert state == "101.8" and a["level"] == "fever"
    assert a["summary"] == "101.8 °F\n2h 10m ago"
    assert a["history"] == [[NOW.isoformat(), 101.8]]
    assert a["unit_of_measurement"] == "°F"


def test_temperature_levels_and_stale(core):
    assert temp(core, 99.2)[1]["level"] == "normal"
    assert temp(core, 102.2, now=NOW + H)[1]["level"] == "high"
    state, a = core.temperature_status(NOW + H + timedelta(hours=13))
    assert state == "102.2" and a["level"] == "stale"


def test_temperature_rejections(core):
    assert temp(core, 110)[0] == "rejected"
    assert temp(core, 90)[0] == "rejected"
    assert temp(core, "abc")[0] == "rejected"
    assert temp(core, 100, when=NOW + 5 * H)[0] == "rejected"
    assert temp(core, 100)[0] == "recorded_temp"
    kind, msg = temp(core, 100)  # same timestamp
    assert kind == "rejected" and "Already" in msg


def test_temp_panel_source_records(core):
    kind, r = core.handle({"source": "temp_panel", "temp_f": "100.4",
                           "administered_at": "now"}, "21", NOW)
    assert kind == "recorded_temp" and r["temp_f"] == 100.4 and r["when"] == NOW


def test_form_panel_source_records(core):
    kind, r = core.handle({"source": "form_panel", "medication": "tylenol",
                           "volume_ml": "2.5", "administered_at": "now"}, "21", NOW)
    assert kind == "recorded" and r["volume_ml"] == 2.5


def test_temperature_backdate_naive_local(core):
    kind, r = temp(core, 100.9, when="2026-11-02 07:10:00")
    assert kind == "recorded_temp"
    assert r["when"] == datetime(2026, 11, 2, 15, 10, tzinfo=timezone.utc)


def test_temp_open_form_prefills_last_reading(core):
    assert core.handle({"source": "temp_open_form"}, "21", NOW)[1]["temp_f"] == 98.6
    temp(core, 100.4)
    kind, info = core.handle({"source": "temp_open_form"}, "21", NOW)
    assert kind == "temp_form" and info["temp_f"] == 100.4
    assert info["local_now"] == "2026-11-02 15:00:00"


def test_timeline_and_doses_attribute(core):
    temp(core, 101.8, when="2026-11-02 07:10:00")
    tap(core, now=datetime(2026, 11, 2, 15, 15, tzinfo=timezone.utc))  # 07:15 local
    temp(core, 99.9, when="2026-11-02 10:40:00")
    temp(core, 100.8, when="2026-11-01 22:00:00")  # previous day
    n, md = core.timeline(NOW)
    assert n == 4
    assert md.split("\n") == [
        "**Sun Nov 1**",
        "- 22:00 🌡 100.8 °F",
        "**Mon Nov 2**",
        "- 07:10 🌡 101.8 °F",
        "- 07:15 💊 Tylenol 3.75 mL",
        "- 10:40 🌡 99.9 °F",
    ]
    _, a = core.status("tylenol", "21", NOW)
    assert a["doses"] == [[datetime(2026, 11, 2, 15, 15, tzinfo=timezone.utc).isoformat(), 3.75]]
    state, a = core.timeline_status(NOW)
    assert state == "4" and a["timeline"] == md
    assert "timeline" not in core.temperature_status(NOW)[1]


def test_timeline_empty(core):
    n, md = core.timeline(NOW)
    assert n == 0 and md.startswith("_No entries")
    assert core.timeline_status(NOW)[0] == "0"
