"""Medication + temperature tracker for Home Assistant (AppDaemon app).

Flow:  dashboard tap  ->  script.medication_record  ->  HA event "medication_record"
       ->  this app validates + writes SQLite  ->  this app answers in the
       originating browser (Browser Mod toast / popup).

The process that writes the row is the one that reports the result. There is
no HA-side waiting, no result sensor, no clock comparison.

Sensors published (only when something changed):
  sensor.medication_<med>              state: eligible | approaching | too_soon | unknown
                                       attrs: summary, default_dose_ml, doses_24h, doses (7 d), ...
  sensor.medication_temperature             state: last reading in °F
                                       attrs: summary, level, history (7 d), timeline (48 h markdown)
  sensor.medication_tracker_heartbeat  state: ISO time, refreshed every 60 s

Event "medication_record" data:
  source:   tap | confirm | form | form_panel | open_form | temp | temp_panel | temp_open_form | temp_quick_form
  medication, browser_id, user_id
  volume_ml, administered_at   (confirm / form)
  temp_f, administered_at      (temp)
"""

import math
import sqlite3
from datetime import datetime, timedelta, timezone

try:  # absent in local unit tests
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:  # pragma: no cover
    hass = None

EVENT = "medication_record"
HEARTBEAT = "sensor.medication_tracker_heartbeat"
TEMP_ENTITY = "sensor.medication_temperature"
TIMELINE_ENTITY = "sensor.medication_timeline"
SCRIPT = "script.medication_record"
WINDOW = timedelta(hours=24)
APPROACHING = timedelta(hours=1)
MAX_BACKDATE = timedelta(days=7)

# Rejections a caregiver may override by confirming ("Log anyway") or by
# submitting the explicit form. Anything else is never recorded.
OVERRIDABLE = ("interval", "limit")

TEMP_DEFAULTS = {
    "enabled": True,
    "fever_f": 100.4,
    "high_fever_f": 102.2,
    "stale_hours": 12,
    "min_f": 93.0,
    "max_f": 108.0,
    "history_days": 4,
    "timeline_hours": 48,
    # helpers behind the -/+ button popups (create as Number helpers in HA)
    "entry_helper": "input_number.medication_temp_entry",
    "dose_helper": "input_number.medication_dose_entry",
    "hours_ago_helper": "input_number.medication_hours_ago",
}


class Rejected(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- helpers --


def parse_ts(value):
    """ISO string -> aware UTC datetime. Raises ValueError on naive input."""
    dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("naive timestamp")
    return dt.astimezone(timezone.utc)


def parse_stored(value):
    """Rows written by older versions might be naive; treat those as UTC."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_delta(td):
    minutes = max(0, math.ceil(td.total_seconds() / 60))
    h, m = divmod(minutes, 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


def nb(text):
    """Replace spaces with non-breaking spaces (keeps a phrase on one line)."""
    return text.replace(" ", "\u00a0")


def fmt_ago(td):
    minutes = max(0, int(td.total_seconds() // 60))
    if minutes < 1:
        return "just now"
    h, m = divmod(minutes, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d {h}h ago"
    return f"{h}h {m:02d}m ago" if h else f"{m} min ago"


# ------------------------------------------------------------------- core --


class Core:
    """All logic, no AppDaemon. `config` is the app's `args` dict."""

    def __init__(self, db_path, config, tz, log):
        self.db_path = db_path
        self.config = config
        self.tz = tz
        self.log = log
        self.temp_cfg = {**TEMP_DEFAULTS, **(config.get("temperature") or {})}
        self._setup_db()

    # --- database ---------------------------------------------------------

    def _connect(self):
        # isolation_level=None -> we control BEGIN/COMMIT ourselves.
        return sqlite3.connect(self.db_path, timeout=10, isolation_level=None)

    def _setup_db(self):
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS doses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    medication TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    volume_ml REAL NOT NULL,
                    concentration_mg_ml REAL NOT NULL,
                    amount_mg REAL NOT NULL,
                    caregiver TEXT NOT NULL
                )
                """
            )
            cols = {r[1] for r in db.execute("PRAGMA table_info(doses)")}
            if "created_at" not in cols:
                db.execute("ALTER TABLE doses ADD COLUMN created_at TEXT")
            if "source" not in cols:
                db.execute("ALTER TABLE doses ADD COLUMN source TEXT")
            try:
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_doses_med_ts "
                    "ON doses (medication, timestamp)"
                )
            except sqlite3.IntegrityError:
                # Duplicates left by the old bug. History is never deleted
                # silently; the transaction check still blocks new duplicates.
                self.log(
                    "doses table has duplicate (medication, timestamp) rows - "
                    "unique index NOT created. Inspect with: SELECT medication, "
                    "timestamp, COUNT(*) FROM doses GROUP BY 1,2 HAVING COUNT(*)>1",
                    level="ERROR",
                )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS temperatures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL UNIQUE,
                    temp_f REAL NOT NULL,
                    caregiver TEXT NOT NULL,
                    created_at TEXT,
                    source TEXT
                )
                """
            )

    def _times(self, db, medication):
        rows = db.execute(
            "SELECT timestamp FROM doses WHERE medication = ?", (medication,)
        ).fetchall()
        return sorted(parse_stored(r[0]) for r in rows)

    def _doses_since(self, db, medication, since):
        rows = db.execute(
            "SELECT timestamp, volume_ml FROM doses WHERE medication = ? "
            "AND timestamp >= ? ORDER BY timestamp",
            (medication, since.isoformat()),
        ).fetchall()
        return [(parse_stored(r[0]), r[1]) for r in rows]

    def _temps_since(self, db, since):
        rows = db.execute(
            "SELECT timestamp, temp_f FROM temperatures WHERE timestamp >= ? "
            "ORDER BY timestamp",
            (since.isoformat(),),
        ).fetchall()
        return [(parse_stored(r[0]), r[1]) for r in rows]

    # --- configuration ----------------------------------------------------

    def med_config(self, medication):
        meds = self.config.get("medications", {})
        if medication not in meds:
            raise Rejected("config", f"Unknown medication '{medication}'")
        cfg = meds[medication]
        if not self.config.get("enabled", False) or not cfg.get("enabled", False):
            raise Rejected("config", f"{medication.capitalize()} recording is disabled")
        return cfg

    def default_dose(self, medication, weight_state, product=None):
        cfg = self.med_config(medication)
        product = product or cfg["default_product"]
        if product not in cfg["products"]:
            raise Rejected("config", f"Unknown {medication} product '{product}'")
        conc = float(cfg["products"][product]["concentration_mg_ml"])
        if not math.isfinite(conc) or conc <= 0:
            raise Rejected("config", f"Invalid concentration for {product}")

        if "fixed_dose_ml" in cfg:  # no weight chart: one configured dose
            ml = float(cfg["fixed_dose_ml"])
            if not math.isfinite(ml) or ml <= 0:
                raise Rejected("config", f"Invalid fixed_dose_ml for {medication}")
            return {"weight_lb": None, "product": product, "concentration_mg_ml": conc,
                    "dose_ml": round(ml, 3), "dose_mg": round(ml * conc, 3)}

        if weight_state in (None, "", "unknown", "unavailable"):
            raise Rejected("weight", "Weight is not set - fill in the weight helper first")
        try:
            weight = float(weight_state)
        except (TypeError, ValueError):
            raise Rejected("weight", f"Weight '{weight_state}' is not a number")
        if not math.isfinite(weight) or weight <= 0:
            raise Rejected("weight", f"Weight {weight:g} lb is invalid")

        for row in cfg.get("dose_chart", []):
            lo, hi = float(row["min_lb"]), float(row["max_lb_exclusive"])
            if lo <= weight < hi:
                if "dose_mg" in row:
                    mg = float(row["dose_mg"])
                    ml = mg / conc
                else:
                    ml = float(row["dose_ml"])
                    mg = ml * conc
                if not all(math.isfinite(v) and v > 0 for v in (ml, mg)):
                    raise Rejected("config", "Chart row produces an invalid dose")
                return {
                    "weight_lb": weight,
                    "product": product,
                    "concentration_mg_ml": conc,
                    "dose_ml": round(ml, 3),
                    "dose_mg": round(mg, 3),
                }
        raise Rejected(
            "weight", f"No {medication} chart row for {weight:g} lb - ask the pediatrician"
        )

    # --- rules ------------------------------------------------------------

    def check(self, times, when, cfg):
        """(code, message) of the first rule `when` breaks, or None."""
        interval = timedelta(hours=float(cfg["min_interval_hours"]))
        limit = int(cfg["max_doses_24h"])

        for t in times:
            gap = abs(when - t)
            if gap < interval:
                return "interval", (
                    f"only {fmt_delta(gap)} since the dose at "
                    f"{t.astimezone(self.tz):%H:%M} (interval {cfg['min_interval_hours']:g} h)"
                )

        proposed = sorted(times + [when])
        # Only 24 h windows that contain the new dose. Windows holding only
        # old (possibly overridden) doses must not block future recordings.
        for start in proposed:
            if not (start <= when < start + WINDOW):
                continue
            n = sum(1 for t in proposed if start <= t < start + WINDOW)
            if n > limit:
                return "limit", f"this would be dose {n} of max {limit} in 24 h"
        return None

    def next_allowed(self, times, now, cfg):
        """Earliest time at which both rules pass, or None if already fine."""
        if not times:
            return None
        interval = timedelta(hours=float(cfg["min_interval_hours"]))
        limit = int(cfg["max_doses_24h"])
        candidates = [max(times) + interval]
        recent = sorted((t for t in times if now - WINDOW < t <= now), reverse=True)
        if len(recent) >= limit:
            # Rolling window: the limit-th most recent dose has to fall out.
            candidates.append(recent[limit - 1] + WINDOW)
        when = max(candidates)
        return when if when > now else None

    # --- status (for the tiles) --------------------------------------------

    def status(self, medication, weight_state, now):
        """-> (state, attributes) for sensor.medication_<medication>."""
        cfg = self.config.get("medications", {}).get(medication, {})
        name = medication.capitalize()
        try:
            dose = self.default_dose(medication, weight_state)
        except Rejected as err:
            return "unknown", {"friendly_name": name, "summary": f"Check: {err.message}",
                               "error": err.message}

        history_days = int(self.temp_cfg["history_days"])
        with self._connect() as db:
            times = self._times(db, medication)
            last_row = db.execute(
                "SELECT timestamp, volume_ml, amount_mg FROM doses "
                "WHERE medication = ? ORDER BY timestamp DESC LIMIT 1",
                (medication,),
            ).fetchone()
            week = self._doses_since(db, medication, now - timedelta(days=history_days))

        recent = [t for t in times if now - WINDOW < t <= now]
        limit = int(cfg["max_doses_24h"])
        nxt = self.next_allowed(times, now, cfg)
        counter = f"{len(recent)}/{limit} in 24 h"

        ml = f"{dose['dose_ml']:g} mL"
        if times and max(times) > now + timedelta(minutes=5):
            state, status_text = "unknown", "Check: a recorded dose is in the future"
        elif nxt is None:
            state, status_text = "eligible", "Ready now"
        else:
            state = "approaching" if nxt - now <= APPROACHING else "too_soon"
            status_text = f"{fmt_delta(nxt - now)} left"

        attrs = {
            "friendly_name": f"{name} · {ml}",   # panels override with name: <short>
            "summary": f"{ml}\n{status_text}\n{counter}",   # 3 lines (wall panel)
            # one-line tiles: state_content [dose_text, status_text, count_text];
            # non-breaking spaces so a wrap only happens between the pieces
            "dose_text": nb(ml),
            "status_text": nb(status_text),
            "count_text": nb(counter),
            "weight_lb": dose["weight_lb"],
            "product": dose["product"],
            "concentration_mg_ml": dose["concentration_mg_ml"],
            "default_dose_ml": dose["dose_ml"],
            "default_dose_mg": dose["dose_mg"],
            "doses_24h": len(recent),
            "max_doses_24h": limit,
            "min_interval_hours": float(cfg["min_interval_hours"]),
            "doses": [[t.isoformat(), ml] for t, ml in week],
        }
        if last_row:
            attrs["last_dose"] = parse_stored(last_row[0]).isoformat()
            attrs["last_volume_ml"] = last_row[1]
            attrs["last_amount_mg"] = last_row[2]
        if nxt:
            attrs["next_allowed"] = nxt.isoformat()
        return state, attrs

    def temp_level(self, temp, age):
        """normal | fever | high | stale (reading too old to colour)."""
        c = self.temp_cfg
        if age > timedelta(hours=float(c["stale_hours"])):
            return "stale"
        if temp >= float(c["high_fever_f"]):
            return "high"
        if temp >= float(c["fever_f"]):
            return "fever"
        return "normal"

    def temperature_status(self, now):
        """-> (state, attributes) for sensor.medication_temperature."""
        c = self.temp_cfg
        attrs = {"friendly_name": "Temperature", "unit_of_measurement": "°F",
                 "fever_f": float(c["fever_f"]), "high_fever_f": float(c["high_fever_f"])}
        with self._connect() as db:
            history = self._temps_since(db, now - timedelta(days=int(c["history_days"])))
            last = db.execute(
                "SELECT timestamp, temp_f FROM temperatures ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        attrs["history"] = [[t.isoformat(), v] for t, v in history]

        if not last:
            attrs.update({"summary": "No reading yet", "reading": nb("No reading yet"),
                          "level": "none"})
            return "unknown", attrs
        last_at, temp = parse_stored(last[0]), float(last[1])
        level = self.temp_level(temp, now - last_at)
        attrs.update({
            "summary": f"{temp:g} °F\n{fmt_ago(now - last_at)}",
            "reading": nb(f"{temp:g} °F"),      # one-line tiles: [reading, age]
            "age": nb(fmt_ago(now - last_at)),
            "level": level,
            "last_at": last_at.isoformat(),
        })
        return f"{temp:g}", attrs

    def timeline_status(self, now):
        """-> (state, attributes) for sensor.medication_timeline."""
        count, md = self.timeline(now)
        return str(count), {"friendly_name": "Medication timeline", "timeline": md,
                            "window_hours": int(self.temp_cfg["timeline_hours"])}

    def timeline(self, now):
        """(count, markdown list of readings and doses in the last N hours, by day)."""
        since = now - timedelta(hours=int(self.temp_cfg["timeline_hours"]))
        events = []
        with self._connect() as db:
            for t, v in self._temps_since(db, since):
                events.append((t, f"🌡 {v:g} °F"))
            for med in self.config.get("medications", {}):
                for t, ml in self._doses_since(db, med, since):
                    events.append((t, f"💊 {med.capitalize()} {ml:g} mL"))
        if not events:
            return 0, f"_No entries in the last {self.temp_cfg['timeline_hours']} h._"
        events.sort(key=lambda e: e[0])
        lines, day = [], None
        for t, text in events:
            local = t.astimezone(self.tz)
            if local.date() != day:
                day = local.date()
                lines.append(f"**{local:%a %b} {local.day}**")
            lines.append(f"- {local:%H:%M} {text}")
        return len(events), "\n".join(lines)

    # --- recording --------------------------------------------------------

    def record(self, medication, when, volume_ml, caregiver, source, weight_state,
               override, now):
        """Validate and insert one dose. Returns a result dict or raises Rejected."""
        cfg = self.med_config(medication)
        dose = self.default_dose(medication, weight_state)

        if when > now + timedelta(minutes=2):
            raise Rejected("time", "That time is in the future")
        if when < now - MAX_BACKDATE:
            raise Rejected("time", "More than 7 days ago - not accepted")

        try:
            volume = float(volume_ml)
        except (TypeError, ValueError):
            raise Rejected("volume", f"Amount '{volume_ml}' is not a number")
        if not math.isfinite(volume) or volume <= 0:
            raise Rejected("volume", "Amount must be greater than 0 mL")
        hard_max = float(cfg.get("max_volume_ml", 2 * dose["dose_ml"]))
        if volume > hard_max + 1e-9:
            raise Rejected(
                "volume",
                f"{volume:g} mL is above the safety cap of {hard_max:g} mL - not "
                "recorded. If this amount really was given, raise max_volume_ml "
                "in medication_tracker.yaml.",
            )

        conc = dose["concentration_mg_ml"]
        ts = when.isoformat()  # normalized UTC; backs the unique index
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                times = self._times(db, medication)
                if when in times:
                    raise Rejected("duplicate", "Already recorded (same time)")
                broken = self.check(times, when, cfg)
                if broken and not (override and broken[0] in OVERRIDABLE):
                    raise Rejected(*broken)
                db.execute(
                    "INSERT INTO doses (medication, timestamp, volume_ml, "
                    "concentration_mg_ml, amount_mg, caregiver, created_at, source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (medication, ts, volume, conc, round(volume * conc, 3),
                     caregiver, now.isoformat(), source),
                )
                db.execute("COMMIT")
            except sqlite3.IntegrityError:
                db.execute("ROLLBACK")
                raise Rejected("duplicate", "Already recorded (same time)")
            except Exception:
                db.execute("ROLLBACK")
                raise
            count = len([t for t in self._times(db, medication)
                         if now - WINDOW < t <= now])

        notes = []
        if broken:
            notes.append("overrode: " + broken[1])
        if volume > dose["dose_ml"] + 1e-9:
            notes.append(f"above chart dose of {dose['dose_ml']:g} mL")
        return {
            "medication": medication,
            "volume_ml": volume,
            "amount_mg": round(volume * conc, 1),
            "when": when,
            "count_24h": count,
            "limit": int(cfg["max_doses_24h"]),
            "notes": notes,
        }

    def record_temperature(self, when, value, caregiver, source, now):
        c = self.temp_cfg
        if not c.get("enabled", True):
            raise Rejected("config", "Temperature recording is disabled")
        if when > now + timedelta(minutes=2):
            raise Rejected("time", "That time is in the future")
        if when < now - MAX_BACKDATE:
            raise Rejected("time", "More than 7 days ago - not accepted")
        try:
            temp = float(value)
        except (TypeError, ValueError):
            raise Rejected("value", f"Temperature '{value}' is not a number")
        lo, hi = float(c["min_f"]), float(c["max_f"])
        if not math.isfinite(temp) or not (lo <= temp <= hi):
            raise Rejected("value", f"{value} °F is outside {lo:g}-{hi:g} °F - looks like a "
                                    "typo, not recorded")
        temp = round(temp, 1)
        with self._connect() as db:
            try:
                db.execute(
                    "INSERT INTO temperatures (timestamp, temp_f, caregiver, created_at, "
                    "source) VALUES (?, ?, ?, ?, ?)",
                    (when.isoformat(), temp, caregiver, now.isoformat(), source),
                )
            except sqlite3.IntegrityError:
                raise Rejected("duplicate", "Already recorded (same time)")
        return {"temp_f": temp, "when": when, "level": self.temp_level(temp, now - when)}

    # --- request handling ------------------------------------------------

    def parse_when(self, raw):
        if raw in (None, "", "now"):
            return None
        try:
            return parse_ts(raw)
        except ValueError:
            pass
        try:  # naive string: interpret in local time
            return datetime.fromisoformat(str(raw).strip()).replace(
                tzinfo=self.tz).astimezone(timezone.utc)
        except ValueError:
            raise Rejected("time", f"Can't read the time '{raw}'")

    def handle(self, data, weight_state, now):
        """Process one `medication_record` event.

        -> ("recorded", result) | ("confirm", info) | ("form", info)
         | ("temp_form", info) | ("recorded_temp", result) | ("rejected", msg)
        """
        medication = str(data.get("medication", "")).strip().lower()
        source = str(data.get("source", "tap")).strip().lower()
        caregiver = str(data.get("caregiver") or "dashboard").strip()

        try:
            if source in ("temp_open_form", "temp_quick_form"):
                with self._connect() as db:
                    last = db.execute(
                        "SELECT temp_f FROM temperatures ORDER BY timestamp DESC LIMIT 1"
                    ).fetchone()
                kind = "temp_quick" if source == "temp_quick_form" else "temp_form"
                return kind, {
                    "temp_f": float(last[0]) if last else 98.6,
                    "local_now": now.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:00"),
                }

            if source in ("temp", "temp_panel"):
                when = self.parse_when(data.get("administered_at")) or now
                return "recorded_temp", self.record_temperature(
                    when, data.get("temp_f"), caregiver, source, now)

            cfg = self.med_config(medication)
            dose = self.default_dose(medication, weight_state)

            if source == "open_form":
                return "form", {
                    "medication": medication,
                    "volume_ml": dose["dose_ml"],
                    "local_now": now.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:00"),
                }

            if source == "tap":
                with self._connect() as db:
                    times = self._times(db, medication)
                broken = self.check(times, now, cfg)
                if broken:
                    return "confirm", {
                        "medication": medication,
                        "reason": broken[1],
                        "administered_at": now.isoformat(),
                        "volume_ml": dose["dose_ml"],
                    }
                return "recorded", self.record(
                    medication, now, dose["dose_ml"], caregiver, source,
                    weight_state, False, now)

            if source in ("confirm", "form", "form_panel"):
                when = self.parse_when(data.get("administered_at")) or now
                volume = data.get("volume_ml")
                if volume in (None, ""):
                    volume = dose["dose_ml"]
                return "recorded", self.record(
                    medication, when, volume, caregiver, source,
                    weight_state, True, now)

            raise Rejected("request", f"Unknown request source '{source}'")

        except Rejected as err:
            return "rejected", err.message


# ------------------------------------------------------------ AppDaemon ---


if hass is not None:

    class MedicationTracker(hass.Hass):

        def initialize(self):
            try:
                from zoneinfo import ZoneInfo
                self.tz = ZoneInfo(self.get_timezone())
            except Exception:
                self.tz = datetime.now().astimezone().tzinfo

            db_path = self.args.get("db_path", "/config/medication_tracker.db")
            self.core = Core(db_path, self.args, self.tz, self.log)
            self.log(f"Medication tracker using database {db_path}")

            self.listen_event(self.on_record, EVENT)
            self.listen_state(self.on_weight, self.args["weight_entity"])
            self.run_every(self.tick, "now", 60)

        # --- callbacks ---

        def on_weight(self, *args, **kwargs):
            self.publish_status()

        def tick(self, *args, **kwargs):
            self.publish_status()
            self.set_state(HEARTBEAT, state=self.now().isoformat(),
                           attributes={"friendly_name": "Medication tracker heartbeat"})

        def on_record(self, event, data, kwargs):
            browser = str(data.get("browser_id") or "").strip()
            if browser in ("", "THIS"):
                browser = None
            data = dict(data)
            data["caregiver"] = self.caregiver_name(data.get("user_id"))
            self.log(f"{EVENT}: {data}")

            try:
                kind, payload = self.core.handle(data, self.weight(), self.now())
            except Exception as err:  # a bug must never fail silently
                self.error(f"{EVENT} crashed: {err!r}")
                kind, payload = "rejected", f"Tracker error, nothing recorded: {err}"
            self.publish_status()

            if kind == "recorded":
                self.log(f"Recorded {payload}")
                self.toast(browser, self.recorded_text(payload))
            elif kind == "recorded_temp":
                self.log(f"Recorded temperature {payload}")
                self.toast(browser, self.temp_text(payload))
            elif kind == "confirm":
                self.confirm_popup(browser, payload)
            elif kind == "form":
                self.form_popup(browser, payload)
            elif kind == "temp_form":
                self.temp_form_popup(browser, payload)
            elif kind == "temp_quick":
                self.temp_quick_popup(browser, payload)
            else:
                self.log(f"NOT recorded: {payload}", level="WARNING")
                self.popup(browser, "Not recorded", payload)

        # --- small helpers ---

        def now(self):
            return datetime.now(timezone.utc)

        def weight(self):
            return self.get_state(self.args["weight_entity"])

        def caregiver_name(self, user_id):
            user_id = str(user_id or "").strip()
            names = self.args.get("caregivers") or {}
            if user_id and user_id != "THIS" and user_id in names:
                return str(names[user_id])
            return "dashboard"

        def when_text(self, when):
            if self.now() - when < timedelta(minutes=2):
                return "now"
            return when.astimezone(self.tz).strftime("%a %H:%M")

        def recorded_text(self, r):
            text = (f"{r['medication'].capitalize()} {r['volume_ml']:g} mL "
                    f"({r['amount_mg']:g} mg) recorded {self.when_text(r['when'])} · "
                    f"{r['count_24h']}/{r['limit']} in 24 h")
            if r["notes"]:
                text += " · " + "; ".join(r["notes"])
            return text

        def temp_text(self, r):
            label = {"normal": "no fever", "fever": "fever", "high": "high fever",
                     "stale": "older reading"}[r["level"]]
            return f"{r['temp_f']:g} °F recorded {self.when_text(r['when'])} · {label}"

        def publish_status(self):
            now = self.now()
            weight = self.weight()
            for med in self.args.get("medications", {}):
                entity = f"sensor.medication_{med}"
                try:
                    state, attrs = self.core.status(med, weight, now)
                except Exception as err:  # never leave a tile stale on a bug
                    self.error(f"{med} status failed: {err}")
                    state, attrs = "unknown", {"friendly_name": med.capitalize(),
                                               "summary": f"Check: {err}",
                                               "error": str(err)}
                self.publish(entity, state, attrs)
            try:
                state, attrs = self.core.temperature_status(now)
            except Exception as err:
                self.error(f"temperature status failed: {err}")
                state, attrs = "unknown", {"friendly_name": "Temperature",
                                           "summary": f"Check: {err}", "level": "none"}
            self.publish(TEMP_ENTITY, state, attrs)
            try:
                state, attrs = self.core.timeline_status(now)
            except Exception as err:
                self.error(f"timeline status failed: {err}")
                state, attrs = "0", {"friendly_name": "Medication timeline",
                                     "timeline": f"Check: {err}"}
            self.publish(TIMELINE_ENTITY, state, attrs)

        def publish(self, entity, state, attrs):
            prev = self.get_state(entity, attribute="all") or {}
            if prev.get("state") != state or prev.get("attributes") != attrs:
                self.set_state(entity, state=state, attributes=attrs, replace=True)

        # --- Browser Mod ---

        def toast(self, browser, message):
            if not browser:
                return
            self.call_service("browser_mod/notification", browser_id=browser,
                              message=message, duration=6000)

        def popup(self, browser, title, content):
            if not browser:
                return
            self.call_service("browser_mod/popup", browser_id=browser, title=title,
                              content=content, adaptive=True, right_button="OK")

        def _action(self, source, med, browser, extra):
            return {"service": SCRIPT,
                    "data": {"source": source, "medication": med,
                             "browser_id": browser, "user_id": "THIS", **extra}}

        def confirm_popup(self, browser, info):
            if not browser:
                self.log("confirmation needed but no browser_id - nothing shown",
                         level="WARNING")
                return
            med = info["medication"]
            self.call_service(
                "browser_mod/popup",
                browser_id=browser,
                title=med.capitalize(),
                adaptive=True,
                content=(f"Too early: {info['reason']}. "
                         f"Log {med.capitalize()} {info['volume_ml']:g} mL anyway?"),
                left_button="Cancel",
                right_button="Log anyway",
                right_button_variant="warning",
                right_button_appearance="filled",
                right_button_action=self._action("confirm", med, browser, {
                    "administered_at": info["administered_at"],
                    "volume_ml": info["volume_ml"],
                }),
            )

        def _tile(self, entity, name, icon):
            return {
                "type": "tile", "entity": entity, "name": name, "icon": icon,
                "state_content": "state", "features_position": "bottom",
                "features": [{"type": "numeric-input", "style": "buttons"}],
                "tap_action": {"action": "none"}, "icon_tap_action": {"action": "none"},
                "card_mod": {"style": "ha-card { zoom: 1.3; }"},
            }

        def _set_helper(self, entity, value):
            self.call_service("input_number/set_value", entity_id=entity, value=value)

        def form_popup(self, browser, info):
            """-/+ buttons for the amount (preset to chart dose) and hours ago."""
            if not browser:
                return
            c = self.core.temp_cfg
            med = info["medication"]
            self._set_helper(c["dose_helper"], min(15, max(0.25, round(info["volume_ml"], 2))))
            self._set_helper(c["hours_ago_helper"], 0)
            self.call_service(
                "browser_mod/popup", browser_id=browser,
                title=f"Record {med.capitalize()}", adaptive=True,
                content={"type": "vertical-stack", "cards": [
                    self._tile(c["dose_helper"], "Amount given", "mdi:pill"),
                    self._tile(c["hours_ago_helper"], "Hours ago (0 = now)", "mdi:clock-outline"),
                ]},
                left_button="Cancel", right_button="Record",
                right_button_variant="success", right_button_appearance="filled",
                right_button_action=self._action("form_panel", med, browser, {}),
            )

        def _temp_cards(self, with_hours_ago):
            c = self.core.temp_cfg
            cards = [self._tile(c["entry_helper"], "Temperature", "mdi:thermometer")]
            if with_hours_ago:
                cards.append(self._tile(c["hours_ago_helper"], "Hours ago (0 = now)",
                                        "mdi:clock-outline"))
            return {"type": "vertical-stack", "cards": cards}

        def _preset_helpers(self, info):
            c = self.core.temp_cfg
            self._set_helper(c["entry_helper"], min(105, max(97, round(info["temp_f"], 1))))
            self._set_helper(c["hours_ago_helper"], 0)

        def temp_quick_popup(self, browser, info):
            """-/+ buttons preloaded with the last reading, recorded as now."""
            if not browser:
                return
            self._preset_helpers(info)
            self.call_service(
                "browser_mod/popup", browser_id=browser, title="Temperature now",
                adaptive=True, content=self._temp_cards(False),
                left_button="Cancel", right_button="Record",
                right_button_variant="success", right_button_appearance="filled",
                right_button_action=self._action("temp_panel", "", browser, {}),
            )

        def temp_form_popup(self, browser, info):
            """Same buttons plus 'hours ago' for backdating."""
            if not browser:
                return
            self._preset_helpers(info)
            self.call_service(
                "browser_mod/popup", browser_id=browser, title="Earlier temperature",
                adaptive=True, content=self._temp_cards(True),
                left_button="Cancel", right_button="Record",
                right_button_variant="success", right_button_appearance="filled",
                right_button_action=self._action("temp_panel", "", browser, {}),
            )
