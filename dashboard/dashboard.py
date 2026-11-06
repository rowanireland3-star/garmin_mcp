#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["garminconnect==0.3.2"]
# ///
"""Running / training dashboard built from Garmin Connect data.

Reuses the OAuth tokens cached by `garmin-mcp-auth` (default ~/.garminconnect,
override with GARMINTOKENS). It never asks for a password. Every run pulls fresh
data and rewrites a single self-contained index.html next to this script.

    python dashboard.py            # fetch data, rebuild index.html
    python dashboard.py --probe    # list which metrics your account returns
    python dashboard.py --demo     # build the page from made-up data (no login)
    python dashboard.py --open     # rebuild, then open it in your browser

Completed past days are cached in .cache/ so a refresh only fetches what's new.
Delete that folder to force a full re-download.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

# ---------------------------------------------------------------------------
# Your settings. Edit these.
# ---------------------------------------------------------------------------
RACES = [
    {"name": "Exeter Half Marathon", "date": "2027-02-06"},
    {"name": "Cardiff Half Marathon", "date": "2027-10-03"},
]
ATHLETE = {"height": "6'3\"", "weight_kg": 80}
PBS = {  # seconds
    "5k": 22 * 60 + 10,      # before the ankle injury
    "10k": 52 * 60,          # Cardiff half this year
    "half": 1 * 3600 + 57 * 60,
}
WEEKS = 12              # training load + weekly mileage window
RECOVERY_DAYS = 28      # HRV / resting HR / sleep window
RECENT_RUN_DAYS = 28    # recent runs table + pace panel window
VO2_WEEKS = 26          # VO2 max trend window
LONG_RUN_KM = 14        # an easy-intent run this long counts as a long run
# Easy-run heart-rate ceiling (bpm). None = work it out from your lactate
# threshold HR in Garmin (89% of LTHR), or 80% of your highest recorded HR.
EASY_HR_CEILING: int | None = None
# Force a run's category if the auto-classifier gets it wrong:
# {activity_id: "easy" | "long" | "workout" | "race"}
OVERRIDES: dict[int, str] = {}

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE / ".cache"
OUT_FILE = HERE / "index.html"

RUN_TYPES = {
    "running", "treadmill_running", "trail_running", "track_running",
    "indoor_running", "street_running", "virtual_run", "ultra_run",
    "obstacle_run",
}
HARD_LABELS = {"THRESHOLD", "VO2MAX", "ANAEROBIC_CAPACITY", "SPRINT"}
WORKOUT_WORDS = re.compile(
    r"interval|tempo|threshold|\breps?\b|fartlek|hill|progression|workout|"
    r"session|track|speed|\d+\s*[x×]\s*\d+|cruise|ladder|pyramid|yasso|\bLT\b",
    re.I,
)
RACE_WORDS = re.compile(r"\brace\b|parkrun|half marathon|\b10k race\b", re.I)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Garmin access
# ---------------------------------------------------------------------------
def connect():
    try:
        from garminconnect import Garmin
    except ImportError:
        sys.exit(
            "garminconnect isn't installed. Either run this script with uv\n"
            "  uv run dashboard.py\n"
            "or install it first:\n"
            "  pip install garminconnect==0.3.2"
        )
    tokenstore = os.path.expanduser(os.getenv("GARMINTOKENS", "~/.garminconnect"))
    if not os.path.isdir(tokenstore):
        sys.exit(
            f"No Garmin tokens found at {tokenstore}.\n"
            "Run `garmin-mcp-auth` once to log in and save them, then re-run this."
        )
    is_cn = os.getenv("GARMIN_IS_CN", "false").lower() in ("true", "1", "yes")
    # No email/password on purpose: login only uses the saved tokens.
    api = Garmin(is_cn=is_cn)
    try:
        api.login(tokenstore)
    except Exception as e:  # noqa: BLE001
        sys.exit(
            f"Couldn't log in with the tokens in {tokenstore}: {e}\n"
            "They've probably expired. Run `garmin-mcp-auth` to refresh them."
        )
    return api


class Fetcher:
    """Calls the API, caches finished days on disk, and records what worked."""

    def __init__(self, api, today: date, use_cache: bool = True):
        self.api = api
        self.today = today
        self.use_cache = use_cache
        self.status: dict[str, dict] = {}
        if use_cache:
            CACHE_DIR.mkdir(exist_ok=True)

    def _note(self, key: str, outcome: str, err: str | None = None) -> None:
        s = self.status.setdefault(key, {"ok": 0, "empty": 0, "error": 0, "last_error": None})
        s[outcome] += 1
        if err:
            s["last_error"] = err[:200]

    def call(self, key: str, fn, *args, day: date | None = None, **kwargs):
        cache_file = None
        # Only cache days that are safely in the past (watch has synced).
        if self.use_cache and day is not None and day < self.today - timedelta(days=2):
            cache_file = CACHE_DIR / f"{key}_{day.isoformat()}.json"
            if cache_file.exists():
                try:
                    data = json.loads(cache_file.read_text())
                    self._note(key, "ok" if data else "empty")
                    return data
                except ValueError:
                    pass
        try:
            data = fn(*args, **kwargs)
            time.sleep(0.1)
        except Exception as e:  # noqa: BLE001
            self._note(key, "error", f"{type(e).__name__}: {e}")
            return None
        self._note(key, "ok" if data else "empty")
        if cache_file is not None:
            cache_file.write_text(json.dumps(data))
        return data


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def dig(obj, *path, default=None):
    for p in path:
        if isinstance(obj, dict):
            obj = obj.get(p)
        elif isinstance(obj, list) and isinstance(p, int) and -len(obj) <= p < len(obj):
            obj = obj[p]
        else:
            return default
        if obj is None:
            return default
    return obj


def first_device_value(d):
    """Garmin keys some training-status blocks by device id; take the primary."""
    if not isinstance(d, dict) or not d:
        return None
    vals = [v for v in d.values() if isinstance(v, dict)]
    prim = [v for v in vals if v.get("primaryTrainingDevice")]
    return (prim or vals or [None])[0]


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def riegel(t: float, d1: float, d2: float) -> float:
    return t * (d2 / d1) ** 1.06


def fmt_hms(s: float | None) -> str | None:
    if s is None:
        return None
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_all(f: Fetcher) -> dict:
    api, today = f.api, f.today
    raw: dict = {}

    log("• profile")
    raw["profile"] = f.call("user_profile", api.get_user_profile)

    # Activities: the load model needs a warm-up period before the window.
    hist_start = today - timedelta(weeks=WEEKS) - timedelta(days=120)
    log(f"• activities since {hist_start}")
    raw["activities"] = f.call(
        "activities", api.get_activities_by_date, hist_start.isoformat(), today.isoformat()
    ) or []

    log(f"• HRV / resting HR / sleep, last {RECOVERY_DAYS} days")
    raw["daily"] = {}
    for i in range(RECOVERY_DAYS):
        d = today - timedelta(days=i)
        ds = d.isoformat()
        raw["daily"][ds] = {
            "hrv": f.call("hrv", api.get_hrv_data, ds, day=d),
            "rhr": f.call("rhr", api.get_rhr_day, ds, day=d),
            "sleep": f.call("sleep", api.get_sleep_data, ds, day=d),
        }

    log(f"• VO2 max / training status, weekly for {VO2_WEEKS} weeks")
    raw["status"] = {}
    for i in range(VO2_WEEKS):
        d = today - timedelta(weeks=i)
        raw["status"][d.isoformat()] = f.call("training_status", api.get_training_status, d.isoformat(), day=d)

    log("• race predictions")
    raw["race_predictions"] = f.call("race_predictions", api.get_race_predictions)
    return raw


def probe(api, today: date) -> None:
    """Print what each endpoint returns for your account, so you can see
    exactly which fields exist before trusting the dashboard."""
    y = (today - timedelta(days=1)).isoformat()
    t = today.isoformat()
    checks = [
        ("get_user_profile()", lambda: api.get_user_profile()),
        ("get_activities(0, 5)", lambda: api.get_activities(0, 5)),
        (f"get_hrv_data({y})", lambda: api.get_hrv_data(y)),
        (f"get_rhr_day({y})", lambda: api.get_rhr_day(y)),
        (f"get_sleep_data({y})", lambda: api.get_sleep_data(y)),
        (f"get_training_status({t})", lambda: api.get_training_status(t)),
        (f"get_max_metrics({y})", lambda: api.get_max_metrics(y)),
        ("get_race_predictions()", lambda: api.get_race_predictions()),
    ]

    def summarize(v, depth=0, maxdepth=2):
        pad = "    " * (depth + 1)
        if isinstance(v, list):
            print(f"{pad}list[{len(v)}]")
            if v and depth < maxdepth:
                summarize(v[0], depth + 1, maxdepth)
        elif isinstance(v, dict):
            for k, val in list(v.items())[:40]:
                if isinstance(val, (dict, list)) and depth < maxdepth:
                    print(f"{pad}{k}:")
                    summarize(val, depth + 1, maxdepth)
                else:
                    shown = repr(val)
                    print(f"{pad}{k} = {shown[:60]}")

    used = {
        "get_activities": ["activityId", "activityName", "startTimeLocal", "activityType",
                           "distance", "duration", "averageSpeed", "averageHR", "maxHR",
                           "activityTrainingLoad", "trainingEffectLabel",
                           "aerobicTrainingEffect", "anaerobicTrainingEffect",
                           "eventType", "vO2MaxValue"],
    }
    for name, fn in checks:
        print(f"\n=== {name}")
        try:
            v = fn()
        except Exception as e:  # noqa: BLE001
            print(f"    ERROR {type(e).__name__}: {e}")
            continue
        if not v:
            print("    (empty)")
            continue
        summarize(v)
        if name.startswith("get_activities") and isinstance(v, list) and v:
            missing = [k for k in used["get_activities"] if k not in v[0]]
            print(f"    -> fields the dashboard uses but missing here: {missing or 'none'}")


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------
def parse_runs_and_load(acts: list, today: date, rhr_guess: float | None, profile):
    max_hr_seen = max((num(a.get("maxHR")) or 0 for a in acts), default=0) or None
    lthr = num(dig(profile, "userData", "lactateThresholdHeartRate"))
    if EASY_HR_CEILING:
        ceiling, ceiling_src = EASY_HR_CEILING, "set in dashboard.py"
    elif lthr:
        ceiling, ceiling_src = round(0.89 * lthr), f"89% of your Garmin lactate threshold HR ({int(lthr)} bpm)"
    elif max_hr_seen:
        ceiling, ceiling_src = round(0.80 * max_hr_seen), f"80% of your highest recorded HR ({int(max_hr_seen)} bpm)"
    else:
        ceiling, ceiling_src = None, "no heart-rate data"

    daily_load: dict[str, float] = {}
    load_src = {"garmin": 0, "estimated": 0, "none": 0}
    runs = []
    for a in acts:
        start = a.get("startTimeLocal") or a.get("startTimeGMT") or ""
        ds = start[:10]
        if len(ds) != 10:
            continue
        dur = num(a.get("duration")) or 0
        avg_hr = num(a.get("averageHR"))
        load = num(a.get("activityTrainingLoad"))
        if load is not None:
            load_src["garmin"] += 1
        elif avg_hr and max_hr_seen and dur:
            # Banister TRIMP fallback when Garmin gives no training load.
            rest = rhr_guess or 55
            hrr = max(0.0, min(1.0, (avg_hr - rest) / max(1, max_hr_seen - rest)))
            load = dur / 60 * hrr * 0.64 * math.exp(1.92 * hrr)
            load_src["estimated"] += 1
        else:
            load = 0
            load_src["none"] += 1
        daily_load[ds] = daily_load.get(ds, 0) + load

        type_key = dig(a, "activityType", "typeKey", default="")
        if type_key not in RUN_TYPES and "run" not in type_key:
            continue
        dist = num(a.get("distance")) or 0
        if dist < 500:
            continue
        speed = num(a.get("averageSpeed")) or (dist / dur if dur else None)
        pace = 1000 / speed if speed else None
        name = a.get("activityName") or ""
        label = (a.get("trainingEffectLabel") or "").upper()
        an_te = num(a.get("anaerobicTrainingEffect")) or 0
        is_race = dig(a, "eventType", "typeKey") == "race" or bool(RACE_WORDS.search(name))
        aid = a.get("activityId")
        if aid in OVERRIDES:
            cat = OVERRIDES[aid]
        elif is_race:
            cat = "race"
        elif (WORKOUT_WORDS.search(name) or type_key == "track_running"
              or label in HARD_LABELS or an_te >= 2.0):
            cat = "workout"
        elif dist >= LONG_RUN_KM * 1000:
            cat = "long"
        else:
            cat = "easy"
        runs.append({
            "id": aid,
            "date": ds,
            "time": start[11:16],
            "name": name,
            "type": type_key,
            "cat": cat,
            "dist": round(dist, 1),
            "dur": round(dur),
            "pace": round(pace, 1) if pace else None,
            "hr": round(avg_hr) if avg_hr else None,
            "maxhr": num(a.get("maxHR")),
            "load": round(load) if load else None,
            "label": label.replace("_", " ").title() if label else None,
            "aer": num(a.get("aerobicTrainingEffect")),
            "ana": an_te or None,
            "elev": round(num(a.get("elevationGain")) or 0),
            "vo2": num(a.get("vO2MaxValue")),
            "hard": bool(cat in ("easy", "long") and ((ceiling and avg_hr and avg_hr > ceiling)
                                                      or label == "TEMPO")),
        })
    runs.sort(key=lambda r: (r["date"], r["time"]))

    # CTL (42-day) / ATL (7-day) exponentially weighted load; TSB = yesterday's CTL - ATL.
    start = min((date.fromisoformat(d) for d in daily_load), default=today)
    ctl = atl = 0.0
    load_series = []
    d = start
    while d <= today:
        L = daily_load.get(d.isoformat(), 0)
        tsb = ctl - atl
        ctl += (L - ctl) / 42
        atl += (L - atl) / 7
        load_series.append({"date": d.isoformat(), "ctl": round(ctl, 1), "atl": round(atl, 1),
                            "tsb": round(tsb, 1), "load": round(L)})
        d += timedelta(days=1)
    window_start = (today - timedelta(weeks=WEEKS)).isoformat()
    load_series = [p for p in load_series if p["date"] > window_start]
    history_days = (today - start).days
    return runs, load_series, load_src, {"ceiling": ceiling, "source": ceiling_src,
                                         "lthr": lthr, "max_hr": max_hr_seen}, history_days


def weekly_mileage(runs: list, today: date):
    first = monday(today) - timedelta(weeks=WEEKS - 1)
    weeks = []
    for i in range(WEEKS):
        ws = first + timedelta(weeks=i)
        we = ws + timedelta(days=6)
        rs = [r for r in runs if ws.isoformat() <= r["date"] <= we.isoformat()]
        weeks.append({
            "start": ws.isoformat(),
            "dist": round(sum(r["dist"] for r in rs)),
            "runs": len(rs),
            "long": round(max((r["dist"] for r in rs), default=0)),
            "time": sum(r["dur"] for r in rs),
            "partial": we >= today,
        })
    # Least-squares trend over completed weeks only.
    pts = [(i, w["dist"]) for i, w in enumerate(weeks) if not w["partial"]]
    trend = None
    if len(pts) >= 3:
        n = len(pts)
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        den = sum((p[0] - mx) ** 2 for p in pts) or 1
        slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / den
        trend = {"slope": slope, "values": [round(my + slope * (i - mx)) for i in range(WEEKS)]}
    return weeks, trend


def recovery(daily: dict):
    out = []
    for ds in sorted(daily):
        d = daily[ds]
        hs = dig(d, "hrv", "hrvSummary") or {}
        sl = dig(d, "sleep", "dailySleepDTO") or {}
        rhr = None
        metrics = dig(d, "rhr", "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE")
        if isinstance(metrics, list) and metrics:
            rhr = num(metrics[0].get("value"))
        if rhr is None:
            rhr = num(dig(d, "sleep", "restingHeartRate"))
        secs = num(sl.get("sleepTimeSeconds"))
        out.append({
            "date": ds,
            "hrv": num(hs.get("lastNightAvg")),
            "hrv_week": num(hs.get("weeklyAvg")),
            "hrv_low": num(dig(hs, "baseline", "balancedLow")),
            "hrv_high": num(dig(hs, "baseline", "balancedUpper")),
            "hrv_status": hs.get("status"),
            "rhr": rhr,
            "sleep_h": round(secs / 3600, 2) if secs else None,
            "deep_h": round((num(sl.get("deepSleepSeconds")) or 0) / 3600, 2) if secs else None,
            "rem_h": round((num(sl.get("remSleepSeconds")) or 0) / 3600, 2) if secs else None,
            "sleep_score": num(dig(sl, "sleepScores", "overall", "value")),
        })
    return out


def vo2_and_status(status: dict, runs: list):
    pts = {}
    for ds, st in status.items():
        g = dig(st, "mostRecentVO2Max", "generic") or {}
        v = num(g.get("vo2MaxPreciseValue")) or num(g.get("vo2MaxValue"))
        if v:
            pts[g.get("calendarDate") or ds] = v
    src = "Garmin training status"
    if not pts:  # fall back to the value stamped on each run
        src = "per-run values"
        for r in runs:
            if r["vo2"]:
                pts[r["date"]] = r["vo2"]
    vo2 = [{"date": d, "v": round(v, 1)} for d, v in sorted(pts.items())]

    latest = status.get(max(status)) if status else None
    ts = first_device_value(dig(latest, "mostRecentTrainingStatus", "latestTrainingStatusData"))
    acute = dig(ts, "acuteTrainingLoadDTO") or {}
    phrase = (ts or {}).get("trainingStatusFeedbackPhrase")
    cur = {
        "status": re.sub(r"_\d+$", "", phrase).replace("_", " ").title() if phrase else None,
        "acute": num(acute.get("dailyTrainingLoadAcute")),
        "chronic": num(acute.get("dailyTrainingLoadChronic")),
        "acwr": num(acute.get("dailyAcuteChronicWorkloadRatio")),
        "acwr_status": (acute.get("acwrStatus") or "").replace("_", " ").title() or None,
    }
    return vo2, src, cur


def race_predictions(rp):
    if isinstance(rp, list):
        rp = rp[-1] if rp else None
    if not isinstance(rp, dict):
        return None
    out = {k: num(rp.get(f)) for k, f in
           (("5k", "time5K"), ("10k", "time10K"), ("half", "timeHalfMarathon"), ("marathon", "timeMarathon"))}
    return out if any(out.values()) else None


def build_data(raw: dict, today: date, status: dict, demo: bool) -> dict:
    rec = recovery(raw["daily"])
    rhrs = [r["rhr"] for r in rec if r["rhr"]]
    runs, load, load_src, hr_info, history_days = parse_runs_and_load(
        raw["activities"], today, median(rhrs) if rhrs else None, raw.get("profile"))
    weeks, trend = weekly_mileage(runs, today)
    vo2, vo2_src, cur = vo2_and_status(raw["status"], runs)
    cutoff = (today - timedelta(days=RECENT_RUN_DAYS - 1)).isoformat()
    recent = [r for r in runs if r["date"] >= cutoff]

    def pace_stats(rs):
        p = [r["pace"] for r in rs if r["pace"]]
        h = [r["hr"] for r in rs if r["hr"]]
        return {"n": len(rs), "pace": median(p) if p else None, "hr": round(median(h)) if h else None,
                "km": round(sum(r["dist"] for r in rs) / 1000, 1)}

    easy = [r for r in recent if r["cat"] in ("easy", "long")]
    hard = [r for r in recent if r["cat"] in ("workout", "race")]

    hm = 21097.5
    predictions = {
        "from_5k": riegel(PBS["5k"], 5000, hm),
        "from_10k": riegel(PBS["10k"], 10000, hm),
    }
    hm_pace = PBS["half"] / 21.0975
    paces = {  # sec/km guide paces anchored on your current half PB
        "easy": [hm_pace + 60, hm_pace + 100],
        "half_pb": hm_pace,
        "sub_155": (3600 + 55 * 60) / 21.0975,
    }

    data_status = {
        "Activities": status.get("activities"),
        "HRV": status.get("hrv"),
        "Resting HR": status.get("rhr"),
        "Sleep": status.get("sleep"),
        "Training status / VO2 max": status.get("training_status"),
        "Race predictions": status.get("race_predictions"),
        "Profile (LTHR)": status.get("user_profile"),
    }
    return {
        "generated": datetime.now().strftime("%a %d %b %Y, %H:%M"),
        "today": today.isoformat(),
        "demo": demo,
        "races": RACES,
        "athlete": ATHLETE,
        "pbs": {k: fmt_hms(v) for k, v in PBS.items()},
        "predictions": {k: fmt_hms(v) for k, v in predictions.items()},
        "garmin_predictions": {k: fmt_hms(v) for k, v in (race_predictions(raw.get("race_predictions")) or {}).items()} or None,
        "paces": paces,
        "load": load,
        "load_src": load_src,
        "load_history_days": history_days,
        "weeks": weeks,
        "trend": trend,
        "recovery": rec,
        "vo2": vo2,
        "vo2_src": vo2_src,
        "current": cur,
        "hr": hr_info,
        "runs": list(reversed(recent)),
        "pace_split": {"easy": pace_stats(easy), "hard": pace_stats(hard),
                       "too_hard": sum(1 for r in easy if r["hard"])},
        "status": data_status,
        "config": {"weeks": WEEKS, "recovery_days": RECOVERY_DAYS,
                   "recent_days": RECENT_RUN_DAYS, "long_km": LONG_RUN_KM},
    }


# ---------------------------------------------------------------------------
# Demo data: fake API responses in Garmin's shapes, run through the same code
# ---------------------------------------------------------------------------
class DemoGarmin:
    def __init__(self, today: date):
        self.today = today
        rnd = random.Random(7)
        self.acts = []
        d = today - timedelta(weeks=WEEKS) - timedelta(days=120)
        aid = 17_000_000_000
        while d <= today:
            wk = (d - (today - timedelta(weeks=30))).days / 7
            dow = d.weekday()
            plan = {1: "workout", 3: "easy", 5: "easy", 6: "long"}.get(dow)
            if plan and rnd.random() > 0.08:
                aid += 1
                base = 7 + max(0, wk) * 0.18
                if plan == "long":
                    dist = min(19, 11 + max(0, wk) * 0.3) * 1000
                    pace = rnd.uniform(375, 405)
                    hr = rnd.uniform(142, 152)
                    name, label = "Long run", "BASE"
                elif plan == "workout":
                    dist = base * 1000
                    pace = rnd.uniform(315, 335)
                    hr = rnd.uniform(160, 168)
                    name = rnd.choice(["6x800m intervals", "Tempo 3x10min", "Threshold session", "Exeter Running"])
                    label = rnd.choice(["THRESHOLD", "VO2MAX", "TEMPO"])
                else:
                    dist = rnd.uniform(6, 9) * 1000
                    pace = rnd.uniform(375, 420) if rnd.random() > 0.25 else rnd.uniform(345, 360)
                    hr = 141 + (pace < 365) * 15 + rnd.uniform(-4, 4)
                    name, label = "Exeter Running", "TEMPO" if pace < 365 else rnd.choice(["BASE", "RECOVERY"])
                dur = dist / 1000 * pace
                self.acts.append({
                    "activityId": aid, "activityName": name,
                    "startTimeLocal": f"{d.isoformat()} 07:{rnd.randint(0, 59):02d}:00",
                    "activityType": {"typeKey": "running"}, "eventType": {"typeKey": "uncategorized"},
                    "distance": dist, "duration": dur, "averageSpeed": 1000 / pace,
                    "averageHR": hr, "maxHR": hr + rnd.uniform(8, 18),
                    "activityTrainingLoad": dur / 60 * (hr - 100) / 30,
                    "trainingEffectLabel": label, "aerobicTrainingEffect": 3.1,
                    "anaerobicTrainingEffect": 2.3 if label == "VO2MAX" else 0.4,
                    "elevationGain": rnd.uniform(20, 140), "vO2MaxValue": 45,
                })
            if dow == 2 and rnd.random() > 0.4:
                aid += 1
                self.acts.append({"activityId": aid, "activityName": "Cycling",
                                  "startTimeLocal": f"{d.isoformat()} 18:00:00",
                                  "activityType": {"typeKey": "cycling"}, "distance": 20000,
                                  "duration": 3000, "averageHR": 130, "activityTrainingLoad": 45})
            d += timedelta(days=1)
        self.rnd = rnd

    def get_user_profile(self):
        return {"userData": {"lactateThresholdHeartRate": 168, "vo2MaxRunning": 46}}

    def get_activities_by_date(self, start, end):
        return [a for a in self.acts if start <= a["startTimeLocal"][:10] <= end][::-1]

    def get_hrv_data(self, ds):
        i = (self.today - date.fromisoformat(ds)).days
        return {"hrvSummary": {"calendarDate": ds, "weeklyAvg": 52 + i % 3,
                               "lastNightAvg": round(52 + 7 * math.sin(i / 3) + self.rnd.uniform(-4, 4)),
                               "status": "BALANCED",
                               "baseline": {"balancedLow": 46, "balancedUpper": 60}}}

    def get_rhr_day(self, ds):
        return {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [
            {"value": round(49 + self.rnd.uniform(-2, 3)), "calendarDate": ds}]}}}

    def get_sleep_data(self, ds):
        s = self.rnd.uniform(6.0, 8.4) * 3600
        return {"dailySleepDTO": {"sleepTimeSeconds": s, "deepSleepSeconds": s * .18,
                                  "remSleepSeconds": s * .22,
                                  "sleepScores": {"overall": {"value": round(55 + (s / 3600 - 6) * 15)}}}}

    def get_training_status(self, ds):
        i = (self.today - date.fromisoformat(ds)).days / 7
        return {"mostRecentVO2Max": {"generic": {"calendarDate": ds, "vo2MaxPreciseValue": round(47.5 - i * 0.09, 1)}},
                "mostRecentTrainingStatus": {"latestTrainingStatusData": {"1": {
                    "primaryTrainingDevice": True, "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                    "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 410, "dailyTrainingLoadChronic": 360,
                                             "dailyAcuteChronicWorkloadRatio": 1.1, "acwrStatus": "OPTIMAL"}}}}}

    def get_race_predictions(self):
        return {"time5K": 1335, "time10K": 2850, "timeHalfMarathon": 6420, "timeMarathon": 13700}


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def render(data: dict) -> str:
    tpl = TEMPLATE
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", blob)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Training Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --border: rgba(11,11,11,0.10);
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --band: rgba(42,120,214,0.10); --neutral: #e1e0d9;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s7: #6250d6;
  --pos: #2a78d6; --neg: #e34948;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b; --good-text: #006300;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --border: rgba(255,255,255,0.10);
    --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --band: rgba(57,135,229,0.16); --neutral: #383835;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s7: #9085e9;
    --pos: #3987e5; --neg: #e66767; --good-text: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --border: rgba(255,255,255,0.10);
  --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --band: rgba(57,135,229,0.16); --neutral: #383835;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s7: #9085e9;
  --pos: #3987e5; --neg: #e66767; --good-text: #0ca30c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 24px 16px 48px; }
header { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; justify-content: space-between; margin-bottom: 20px; }
h1 { font-size: 24px; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 16px; margin: 0 0 2px; }
.sub { color: var(--ink-2); font-size: 13px; }
.muted { color: var(--muted); }
.controls { display: flex; gap: 8px; align-items: center; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--surface); }
.seg button { border: 0; background: none; color: var(--ink-2); font: inherit; font-size: 13px; padding: 6px 12px; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--page); }
.banner { background: var(--surface); border: 1px dashed var(--serious); border-radius: 10px; padding: 10px 14px; margin-bottom: 16px; font-size: 14px; }
.grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; min-width: 0; }
.span-12 { grid-column: span 12; } .span-8 { grid-column: span 8; } .span-7 { grid-column: span 7; }
.span-6 { grid-column: span 6; } .span-5 { grid-column: span 5; } .span-4 { grid-column: span 4; }
@media (max-width: 900px) { .span-8, .span-7, .span-6, .span-5, .span-4 { grid-column: span 12; } }
.race { display: flex; flex-direction: column; gap: 4px; }
.race .big { font-size: 40px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.1; }
.race .big small { font-size: 16px; font-weight: 500; color: var(--ink-2); }
.bar { height: 6px; background: var(--neutral); border-radius: 3px; overflow: hidden; margin-top: 8px; }
.bar i { display: block; height: 100%; background: var(--s1); border-radius: 3px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; }
.tile .label { font-size: 13px; color: var(--ink-2); }
.tile .value { font-size: 28px; font-weight: 650; letter-spacing: -0.01em; }
.tile .note { font-size: 12px; color: var(--muted); }
.chart { position: relative; height: 260px; margin-top: 10px; }
.chart.short { height: 130px; } .chart.mid { height: 190px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 13px; color: var(--ink-2); margin-top: 8px; }
.legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 2px; background: var(--c); margin-right: 6px; vertical-align: -1px; }
.legend span.line::before { height: 2px; vertical-align: 3px; width: 14px; }
.legend span.dash::before { height: 0; border-top: 2px dashed var(--c); background: none; vertical-align: 3px; width: 14px; }
.legend span.tri::before { width: 0; height: 0; background: none; border-left: 6px solid transparent; border-right: 6px solid transparent; border-bottom: 10px solid var(--c); border-radius: 0; }
.rec-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
@media (max-width: 900px) { .rec-grid { grid-template-columns: 1fr; } }
.rec-grid h3 { font-size: 14px; margin: 0; }
.rec-grid .now { font-size: 22px; font-weight: 650; }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 10px; }
.split > div { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
.split .v { font-size: 22px; font-weight: 650; }
.status-pill { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); }
.status-pill::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--c); }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; font-weight: 600; color: var(--ink-2); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; padding: 8px 10px; border-bottom: 1px solid var(--axis); white-space: nowrap; }
td { padding: 8px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.name { white-space: normal; min-width: 140px; }
.table-wrap { overflow-x: auto; margin-top: 10px; }
.tag { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 999px; background: color-mix(in srgb, var(--c) 16%, transparent); color: var(--ink); border: 1px solid color-mix(in srgb, var(--c) 45%, transparent); }
.warn { color: var(--ink); font-size: 12px; }
.warn::before { content: "▲ "; color: var(--serious); }
.empty { color: var(--muted); font-size: 14px; padding: 40px 0; text-align: center; }
ul.plain { margin: 6px 0 0; padding-left: 18px; color: var(--ink-2); font-size: 13px; }
footer { margin-top: 24px; color: var(--muted); font-size: 12px; }
footer table { font-size: 12px; max-width: 640px; }
footer td, footer th { padding: 4px 8px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Training dashboard</h1>
      <div class="sub" id="generated"></div>
    </div>
    <div class="controls">
      <div class="seg" role="group" aria-label="Units">
        <button data-unit="km" aria-pressed="true">km</button>
        <button data-unit="mi" aria-pressed="false">miles</button>
      </div>
    </div>
  </header>
  <div id="banner"></div>

  <div class="grid">
    <div id="races" style="display:contents"></div>

    <section class="card span-12">
      <div class="tiles" id="tiles"></div>
    </section>

    <section class="card span-12">
      <h2>Training load</h2>
      <div class="sub" id="load-sub"></div>
      <div class="chart"><canvas id="c-load"></canvas></div>
      <div class="legend"><span class="line" style="--c:var(--s1)">Fitness (CTL, 42-day)</span><span class="line" style="--c:var(--s2)">Fatigue (ATL, 7-day)</span></div>
      <div class="chart short"><canvas id="c-tsb"></canvas></div>
      <div class="legend"><span style="--c:var(--pos)">Form (TSB) positive: fresh</span><span style="--c:var(--neg)">Form negative: carrying fatigue</span></div>
    </section>

    <section class="card span-7">
      <h2>Weekly distance</h2>
      <div class="sub" id="weeks-sub"></div>
      <div class="chart"><canvas id="c-weeks"></canvas></div>
      <div class="legend"><span style="--c:var(--s1)">Distance</span><span class="dash" style="--c:var(--ink-2)">Trend (completed weeks)</span></div>
    </section>

    <section class="card span-5">
      <h2>VO2 max</h2>
      <div class="sub" id="vo2-sub"></div>
      <div class="chart"><canvas id="c-vo2"></canvas></div>
    </section>

    <section class="card span-12">
      <h2>Recovery</h2>
      <div class="sub" id="rec-sub"></div>
      <div class="rec-grid" style="margin-top:12px">
        <div><h3>HRV (overnight avg)</h3><div class="now" id="hrv-now"></div><div class="chart mid"><canvas id="c-hrv"></canvas></div>
          <div class="legend"><span class="line" style="--c:var(--s1)">Last night</span><span class="dash" style="--c:var(--ink-2)">7-day avg</span><span style="--c:var(--band)">Balanced range</span></div></div>
        <div><h3>Resting heart rate</h3><div class="now" id="rhr-now"></div><div class="chart mid"><canvas id="c-rhr"></canvas></div></div>
        <div><h3>Sleep</h3><div class="now" id="sleep-now"></div><div class="chart mid"><canvas id="c-sleep"></canvas></div></div>
      </div>
    </section>

    <section class="card span-12">
      <h2>Pace check: are easy days easy?</h2>
      <div class="sub" id="pace-sub"></div>
      <div class="split" id="pace-split"></div>
      <div class="grid" style="margin-top:4px">
        <div class="span-7">
          <div class="chart"><canvas id="c-pace"></canvas></div>
          <div class="legend"><span style="--c:var(--s1)">Easy / long</span><span class="tri" style="--c:var(--serious)">Easy, but HR or effect too high</span><span style="--c:var(--s2)">Workout</span><span style="--c:var(--s3)">Race</span><span style="--c:var(--band)">Easy pace guide</span></div>
        </div>
        <div class="span-5">
          <div class="chart"><canvas id="c-easyhr"></canvas></div>
          <div class="legend"><span style="--c:var(--s1)">Avg HR, easy runs</span><span class="dash" style="--c:var(--serious)">Easy ceiling</span></div>
        </div>
      </div>
    </section>

    <section class="card span-12">
      <h2>Recent runs</h2>
      <div class="sub" id="runs-sub"></div>
      <div class="table-wrap"><table id="runs"></table></div>
    </section>

    <section class="card span-12">
      <h2>Benchmarks</h2>
      <div class="sub">Your PBs, what they predict for a half (Riegel formula), and guide paces.</div>
      <div class="table-wrap"><table id="bench"></table></div>
    </section>
  </div>

  <footer>
    <div id="howto"></div>
    <div class="table-wrap"><table id="avail"></table></div>
  </footer>
</div>

<script>
const D = /*__DATA__*/null;
let unit = "km";
try { unit = localStorage.getItem("unit") || "km"; } catch (e) {}
const charts = [];
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const MI = 1609.344;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const dist = m => unit === "km" ? m / 1000 : m / MI;
const fmtDist = m => m == null ? "–" : dist(m).toFixed(dist(m) >= 100 ? 0 : 1);
const paceUnit = () => unit === "km" ? "/km" : "/mi";
const paceVal = spk => spk == null ? null : (unit === "km" ? spk : spk * MI / 1000);
function fmtPace(spk) { if (spk == null) return "–"; let s = Math.round(paceVal(spk)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); }
function fmtDur(s) { if (!s) return "–"; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return h ? `${h}:${String(m).padStart(2,"0")}:${String(x).padStart(2,"0")}` : `${m}:${String(x).padStart(2,"0")}`; }
const dt = s => new Date(s + "T12:00:00");
const shortDate = s => dt(s).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
const dayDate = s => dt(s).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
const avg = a => { a = a.filter(v => v != null); return a.length ? a.reduce((x, y) => x + y, 0) / a.length : null; };
const last = (a, k) => { for (let i = a.length - 1; i >= 0; i--) if (a[i][k] != null) return a[i]; return null; };
const CAT = { easy: ["Easy", "--s1"], long: ["Long", "--s7"], workout: ["Workout", "--s2"], race: ["Race", "--s3"] };

function baseOpts(extra = {}) {
  const grid = css("--grid"), muted = css("--muted"), ink = css("--ink");
  return Object.assign({
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false },
      tooltip: { backgroundColor: css("--surface"), titleColor: ink, bodyColor: css("--ink-2"), borderColor: css("--axis"), borderWidth: 1, padding: 10, boxPadding: 4, usePointStyle: true } },
    scales: {
      x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: muted, maxRotation: 0, autoSkipPadding: 14, font: { size: 11 } } },
      y: { grid: { color: grid }, border: { display: false }, ticks: { color: muted, font: { size: 11 } } } },
  }, extra);
}
function mk(id, cfg) { const c = new Chart($(id), cfg); charts.push(c); return c; }
function empty(id, msg) { const cv = $(id); cv.style.display = "none"; cv.insertAdjacentHTML("afterend", `<div class="empty">${esc(msg)}</div>`); }

function races() {
  const today = dt(D.today);
  $("races").innerHTML = D.races.map((r, i) => {
    const days = Math.round((dt(r.date) - today) / 864e5);
    const w = Math.floor(days / 7), d = days % 7;
    const pct = Math.max(0, Math.min(100, 100 - days / (i === 0 ? 182 : 365) * 100));
    const when = dt(r.date).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    const body = days < 0 ? `<div class="big">Done</div>` :
      `<div class="big">${w}<small> weeks</small> ${d}<small> days</small></div><div class="sub">${days} days to go</div>`;
    return `<section class="card span-6 race"><div class="sub">${esc(when)}</div><h2>${esc(r.name)}</h2>${body}
      <div class="bar" title="Share of the build elapsed"><i style="width:${pct}%"></i></div></section>`;
  }).join("");
}

function formLabel(t) {
  if (t == null) return ["–", "--muted"];
  if (t > 5) return ["Fresh", "--good"];
  if (t >= -10) return ["Neutral", "--s1"];
  if (t >= -30) return ["Productive fatigue", "--warning"];
  return ["High fatigue", "--critical"];
}
function tiles() {
  const L = D.load.length ? D.load[D.load.length - 1] : null;
  const L7 = D.load.length > 7 ? D.load[D.load.length - 8] : null;
  const delta = (a, b) => a != null && b != null ? (a - b >= 0 ? "+" : "") + (a - b).toFixed(1) + " vs last week" : "";
  const v = last(D.vo2, "v");
  const f = formLabel(L && L.tsb);
  const c = D.current || {};
  const gp = D.garmin_predictions;
  const T = [
    ["Fitness (CTL)", L ? L.ctl.toFixed(0) : "–", L7 ? delta(L.ctl, L7.ctl) : ""],
    ["Fatigue (ATL)", L ? L.atl.toFixed(0) : "–", L7 ? delta(L.atl, L7.atl) : ""],
    ["Form (TSB)", L ? (L.tsb > 0 ? "+" : "") + L.tsb.toFixed(0) : "–", `<span class="status-pill" style="--c:var(${f[1]})">${f[0]}</span>`],
    ["Garmin status", c.status || "–", c.acwr != null ? `Load ratio ${c.acwr.toFixed(1)}${c.acwr_status ? " · " + esc(c.acwr_status) : ""}` : ""],
    ["VO2 max", v ? v.v.toFixed(1) : "–", v ? "as of " + shortDate(v.date) : ""],
    ["Garmin half prediction", gp && gp.half ? gp.half : "–", gp && gp["10k"] ? "10k " + gp["10k"] + " · 5k " + gp["5k"] : "PB " + D.pbs.half],
  ];
  $("tiles").innerHTML = T.map(t => `<div class="tile"><div class="label">${t[0]}</div><div class="value">${esc(t[1])}</div><div class="note">${t[2]}</div></div>`).join("");
}

function loadChart() {
  const s = D.load_src;
  const src = s.garmin && !s.estimated ? "Garmin's per-activity training load" :
    s.estimated ? `Garmin training load (${s.estimated} activities estimated from heart rate)` : "no load data";
  $("load-sub").textContent = `Last ${D.config.weeks} weeks, all sports. Built from ${src}. Form = yesterday's fitness minus fatigue.` +
    (D.load_history_days < 90 ? ` Only ${D.load_history_days} days of history, so fitness is still warming up.` : "");
  if (!D.load.length) { empty("c-load", "No activities found."); empty("c-tsb", ""); return; }
  const labels = D.load.map(p => shortDate(p.date));
  mk("c-load", { type: "line", data: { labels, datasets: [
      { label: "Fitness (CTL)", data: D.load.map(p => p.ctl), borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .3 },
      { label: "Fatigue (ATL)", data: D.load.map(p => p.atl), borderColor: css("--s2"), backgroundColor: css("--s2"), borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .3 },
    ] },
    options: baseOpts({ plugins: { ...baseOpts().plugins, tooltip: { ...baseOpts().plugins.tooltip, callbacks: {
      title: i => dayDate(D.load[i[0].dataIndex].date),
      label: c => ` ${c.dataset.label}: ${c.parsed.y.toFixed(0)}`,
      afterBody: i => { const p = D.load[i[0].dataIndex]; return [`Form (TSB): ${p.tsb.toFixed(0)}`, `Load that day: ${p.load}`]; } } } } }) });
  const pos = css("--pos"), neg = css("--neg");
  mk("c-tsb", { type: "bar", data: { labels, datasets: [{ label: "Form (TSB)", data: D.load.map(p => p.tsb),
      backgroundColor: D.load.map(p => p.tsb >= 0 ? pos : neg), borderRadius: 2, barPercentage: 1, categoryPercentage: .8 }] },
    options: baseOpts({ plugins: { ...baseOpts().plugins, tooltip: { ...baseOpts().plugins.tooltip, callbacks: {
      title: i => dayDate(D.load[i[0].dataIndex].date), label: c => ` Form: ${c.parsed.y.toFixed(0)} · ${formLabel(c.parsed.y)[0]}` } } } }) });
}

function weeksChart() {
  const W = D.weeks, u = unit === "km" ? "km" : "mi";
  const done = W.filter(w => !w.partial);
  const last4 = done.slice(-4);
  const a4 = avg(last4.map(w => w.dist));
  const slope = D.trend ? dist(D.trend.slope) : null;
  $("weeks-sub").textContent = `Last ${W.length} weeks (Mon–Sun). Last 4 complete weeks average ${a4 != null ? dist(a4).toFixed(1) : "–"} ${u}` +
    (slope != null ? `; trend ${slope >= 0 ? "+" : ""}${slope.toFixed(1)} ${u}/week.` : ".");
  const s1 = css("--s1");
  mk("c-weeks", { type: "bar", data: { labels: W.map(w => shortDate(w.start)), datasets: [
      { type: "bar", label: "Distance", data: W.map(w => +dist(w.dist).toFixed(1)), backgroundColor: W.map(w => w.partial ? s1 + "66" : s1), borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", categoryPercentage: .75 },
      ...(D.trend ? [{ type: "line", label: "Trend", data: D.trend.values.map(v => +dist(v).toFixed(1)), borderColor: css("--ink-2"), borderDash: [5, 4], borderWidth: 2, pointRadius: 0, pointHoverRadius: 0 }] : []),
    ] },
    options: baseOpts({ plugins: { ...baseOpts().plugins, tooltip: { ...baseOpts().plugins.tooltip, filter: c => c.datasetIndex === 0, callbacks: {
      title: i => "Week of " + dayDate(W[i[0].dataIndex].start) + (W[i[0].dataIndex].partial ? " (in progress)" : ""),
      label: c => ` ${c.parsed.y.toFixed(1)} ${u}`,
      afterBody: i => { const w = W[i[0].dataIndex]; return [`${w.runs} runs · ${fmtDur(w.time)}`, `Longest: ${fmtDist(w.long)} ${u}`]; } } } },
      scales: { ...baseOpts().scales, y: { ...baseOpts().scales.y, beginAtZero: true, title: { display: true, text: u, color: css("--muted") } } } }) });
}

function vo2Chart() {
  const V = D.vo2;
  if (!V.length) { $("vo2-sub").textContent = ""; empty("c-vo2", "Your account returned no VO2 max values."); return; }
  const first = V[0], lastv = V[V.length - 1];
  const ch = lastv.v - first.v;
  $("vo2-sub").textContent = `Running VO2 max since ${shortDate(first.date)}: ${ch >= 0 ? "+" : ""}${ch.toFixed(1)} (from ${D.vo2_src}).`;
  const vals = V.map(p => p.v);
  mk("c-vo2", { type: "line", data: { labels: V.map(p => shortDate(p.date)), datasets: [
      { label: "VO2 max", data: vals, borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 3, pointHoverRadius: 5, stepped: false, tension: .2 }] },
    options: baseOpts({ plugins: { ...baseOpts().plugins, tooltip: { ...baseOpts().plugins.tooltip, callbacks: { title: i => dayDate(V[i[0].dataIndex].date), label: c => ` VO2 max ${c.parsed.y.toFixed(1)}` } } },
      scales: { ...baseOpts().scales, y: { ...baseOpts().scales.y, suggestedMin: Math.floor(Math.min(...vals) - 1), suggestedMax: Math.ceil(Math.max(...vals) + 1) } } }) });
}

function recovery() {
  const R = D.recovery;
  const labels = R.map(r => shortDate(r.date));
  const tt = (fmt) => ({ ...baseOpts().plugins.tooltip, callbacks: { title: i => dayDate(R[i[0].dataIndex].date), ...fmt } });
  $("rec-sub").textContent = `Last ${R.length} nights. HRV and sleep are the night before each date.`;
  // HRV
  const h = last(R, "hrv");
  if (!R.some(r => r.hrv != null)) { empty("c-hrv", "No HRV data (needs a watch that records overnight HRV)."); $("hrv-now").textContent = "–"; }
  else {
    const status = h.hrv_status ? h.hrv_status.charAt(0) + h.hrv_status.slice(1).toLowerCase() : "";
    $("hrv-now").innerHTML = `${h.hrv} ms <span class="sub">${esc(status)}${h.hrv_week ? " · 7-day " + h.hrv_week : ""}</span>`;
    const band = css("--band");
    mk("c-hrv", { type: "line", data: { labels, datasets: [
        { label: "Balanced high", data: R.map(r => r.hrv_high), borderWidth: 0, pointRadius: 0, fill: "+1", backgroundColor: band },
        { label: "Balanced low", data: R.map(r => r.hrv_low), borderWidth: 0, pointRadius: 0 },
        { label: "Last night", data: R.map(r => r.hrv), borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 2, pointHoverRadius: 4, spanGaps: true, tension: .3 },
        { label: "7-day avg", data: R.map(r => r.hrv_week), borderColor: css("--ink-2"), borderDash: [5, 4], borderWidth: 2, pointRadius: 0, spanGaps: true },
      ] }, options: baseOpts({ plugins: { ...baseOpts().plugins, tooltip: tt({ label: c => c.parsed.y == null ? null : ` ${c.dataset.label}: ${c.parsed.y} ms` }) } }) });
  }
  // RHR
  const r = last(R, "rhr");
  if (!r) { empty("c-rhr", "No resting HR data."); $("rhr-now").textContent = "–"; }
  else {
    const a = avg(R.map(x => x.rhr));
    $("rhr-now").innerHTML = `${r.rhr} bpm <span class="sub">${R.length}-day avg ${a.toFixed(0)}</span>`;
    mk("c-rhr", { type: "line", data: { labels, datasets: [{ label: "Resting HR", data: R.map(x => x.rhr), borderColor: css("--s1"), backgroundColor: css("--s1"), borderWidth: 2, pointRadius: 2, pointHoverRadius: 4, spanGaps: true, tension: .3 }] },
      options: baseOpts({ plugins: { ...baseOpts().plugins, tooltip: tt({ label: c => ` ${c.parsed.y} bpm` }) } }) });
  }
  // Sleep
  const s = last(R, "sleep_h");
  if (!s) { empty("c-sleep", "No sleep data."); $("sleep-now").textContent = "–"; }
  else {
    const a = avg(R.slice(-7).map(x => x.sleep_h));
    const hm = x => Math.floor(x) + "h " + String(Math.round(x % 1 * 60)).padStart(2, "0") + "m";
    $("sleep-now").innerHTML = `${hm(s.sleep_h)} <span class="sub">${s.sleep_score ? "score " + s.sleep_score + " · " : ""}7-day avg ${hm(a)}</span>`;
    mk("c-sleep", { type: "bar", data: { labels, datasets: [{ label: "Sleep", data: R.map(x => x.sleep_h), backgroundColor: css("--s1"), borderRadius: { topLeft: 3, topRight: 3 }, borderSkipped: "bottom", categoryPercentage: .8 }] },
      options: baseOpts({ plugins: { ...baseOpts().plugins, tooltip: tt({ label: c => " " + hm(c.parsed.y),
        afterBody: i => { const x = R[i[0].dataIndex]; return [x.sleep_score ? `Score ${x.sleep_score}` : "", x.deep_h != null ? `Deep ${hm(x.deep_h)} · REM ${hm(x.rem_h)}` : ""].filter(Boolean); } }) } ,
        scales: { ...baseOpts().scales, y: { ...baseOpts().scales.y, beginAtZero: true, suggestedMax: 9, ticks: { ...baseOpts().scales.y.ticks, callback: v => v + "h" } } } }) });
  }
}

function pacePanel() {
  const runs = [...D.runs].reverse();
  const P = D.pace_split, hr = D.hr;
  $("pace-sub").innerHTML = `Last ${D.config.recent_days} days. Easy ceiling ${hr.ceiling ? hr.ceiling + " bpm" : "unknown"} (${esc(hr.source)}). ` +
    `Easy guide pace ${fmtPace(D.paces.easy[0])}–${fmtPace(D.paces.easy[1])} ${paceUnit()} (half PB pace plus 60–100 s/km).`;
  const box = (t, p, extra) => `<div><div class="sub">${t}</div><div class="v">${fmtPace(p.pace)} <span class="sub">${paceUnit()}</span></div>
    <div class="sub">${p.n} runs · ${fmtDist(p.km * 1000)} ${unit === "km" ? "km" : "mi"}${p.hr ? " · median HR " + p.hr : ""}</div>${extra || ""}</div>`;
  const th = P.too_hard;
  $("pace-split").innerHTML = box("Easy and long runs, median pace", P.easy,
      P.easy.n ? (th ? `<div class="warn">${th} of ${P.easy.n} ran above the easy ceiling or logged a tempo-level effect</div>` : `<div class="sub" style="color:var(--good-text)">✓ All stayed easy</div>`) : "") +
    box("Workouts and races, median pace", P.hard);
  if (!runs.length) { empty("c-pace", "No runs in this window."); empty("c-easyhr", ""); return; }

  const t0 = dt(runs[0].date).getTime();
  const xOf = r => (dt(r.date) - t0) / 864e5;
  const pt = r => ({ x: xOf(r), y: paceVal(r.pace), r });
  const sets = [
    ["Easy / long", r => (r.cat === "easy" || r.cat === "long") && !r.hard, "--s1", "circle"],
    ["Easy, too hard", r => (r.cat === "easy" || r.cat === "long") && r.hard, "--serious", "triangle"],
    ["Workout", r => r.cat === "workout", "--s2", "circle"],
    ["Race", r => r.cat === "race", "--s3", "rectRounded"],
  ];
  const span = Math.max(1, xOf(runs[runs.length - 1]));
  const lo = paceVal(D.paces.easy[0]), hi = paceVal(D.paces.easy[1]);
  const surface = css("--surface");
  mk("c-pace", { type: "scatter", data: { datasets: [
      { label: "guide-hi", data: [{ x: -1, y: hi }, { x: span + 1, y: hi }], showLine: true, borderWidth: 0, pointRadius: 0, fill: "+1", backgroundColor: css("--band") },
      { label: "guide-lo", data: [{ x: -1, y: lo }, { x: span + 1, y: lo }], showLine: true, borderWidth: 0, pointRadius: 0 },
      ...sets.map(([label, f, c, ps]) => ({ label, data: runs.filter(r => r.pace && f(r)).map(pt), backgroundColor: css(c), borderColor: surface, borderWidth: 2, pointStyle: ps, pointRadius: 6, pointHoverRadius: 8 })) ] },
    options: baseOpts({ interaction: { mode: "nearest", intersect: true },
      plugins: { ...baseOpts().plugins, tooltip: { ...baseOpts().plugins.tooltip, filter: c => !!c.raw.r, callbacks: {
        title: i => { const r = i[0].raw.r; return dayDate(r.date) + " · " + r.name; },
        label: c => { const r = c.raw.r; return ` ${fmtDist(r.dist)} ${unit === "km" ? "km" : "mi"} @ ${fmtPace(r.pace)} ${paceUnit()}${r.hr ? " · HR " + r.hr : ""}`; },
        afterLabel: c => { const r = c.raw.r; return r.label ? ` Garmin effect: ${r.label}` : ""; } } } },
      scales: { x: { ...baseOpts().scales.x, type: "linear", min: -0.5, max: span + 0.5, ticks: { ...baseOpts().scales.x.ticks, stepSize: 7, callback: v => shortDate(new Date(t0 + v * 864e5).toISOString().slice(0, 10)) } },
        y: { ...baseOpts().scales.y, reverse: true, title: { display: true, text: "pace " + paceUnit() + " (faster ↑)", color: css("--muted") }, ticks: { ...baseOpts().scales.y.ticks, callback: v => { v = Math.round(v); return Math.floor(v / 60) + ":" + String(v % 60).padStart(2, "0"); } } } } }) });

  const easy = runs.filter(r => (r.cat === "easy" || r.cat === "long") && r.hr);
  if (!easy.length) { empty("c-easyhr", "No easy runs with heart rate."); return; }
  const s1 = css("--s1"), ser = css("--serious");
  mk("c-easyhr", { type: "bar", data: { labels: easy.map(r => shortDate(r.date)), datasets: [
      { type: "bar", label: "Avg HR", data: easy.map(r => r.hr), backgroundColor: easy.map(r => r.hard ? ser : s1), borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom", categoryPercentage: .75 },
      ...(hr.ceiling ? [{ type: "line", label: "Easy ceiling", data: easy.map(() => hr.ceiling), borderColor: ser, borderDash: [5, 4], borderWidth: 2, pointRadius: 0, pointHoverRadius: 0 }] : []) ] },
    options: baseOpts({ plugins: { ...baseOpts().plugins, tooltip: { ...baseOpts().plugins.tooltip, filter: c => c.datasetIndex === 0, callbacks: {
        title: i => { const r = easy[i[0].dataIndex]; return dayDate(r.date) + " · " + r.name; },
        label: c => { const r = easy[c.dataIndex]; return ` HR ${r.hr} · ${fmtPace(r.pace)} ${paceUnit()}${r.hard ? " · over easy" : ""}`; } } } },
      scales: { ...baseOpts().scales, y: { ...baseOpts().scales.y, suggestedMin: Math.min(...easy.map(r => r.hr)) - 10, title: { display: true, text: "bpm", color: css("--muted") } } } }) });
}

function runsTable() {
  const u = unit === "km" ? "km" : "mi";
  $("runs-sub").textContent = `${D.runs.length} runs in the last ${D.config.recent_days} days. Type is auto-detected from the name, Garmin's training effect, and distance (long ≥ ${D.config.long_km} km); fix any in OVERRIDES in dashboard.py.`;
  if (!D.runs.length) { $("runs").innerHTML = `<tr><td class="empty">No runs found.</td></tr>`; return; }
  $("runs").innerHTML = `<thead><tr><th>Date</th><th>Run</th><th class="num">Distance (${u})</th><th class="num">Time</th><th class="num">Pace (${paceUnit()})</th><th class="num">Avg HR</th><th>Type</th><th>Garmin effect</th><th class="num">Load</th></tr></thead><tbody>` +
    D.runs.map(r => { const c = CAT[r.cat] || ["?", "--muted"];
      return `<tr><td>${dayDate(r.date)}</td><td class="name">${esc(r.name)}</td><td class="num">${fmtDist(r.dist)}</td><td class="num">${fmtDur(r.dur)}</td><td class="num">${fmtPace(r.pace)}</td><td class="num">${r.hr ?? "–"}</td>
        <td><span class="tag" style="--c:var(${c[1]})">${c[0]}</span>${r.hard ? ` <span class="warn">too hard</span>` : ""}</td><td>${esc(r.label || "–")}</td><td class="num">${r.load ?? "–"}</td></tr>`; }).join("") + "</tbody>";
}

function bench() {
  const g = D.garmin_predictions || {};
  const hmKm = 21.0975;
  const pace = t => { if (!t) return "–"; const p = t.split(":").map(Number); const s = p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1]; return fmtPace(s / hmKm); };
  const rows = [
    ["Half PB", D.pbs.half, pace(D.pbs.half), "Your current best"],
    ["Half predicted from 10k PB (52:00)", D.predictions.from_10k, pace(D.predictions.from_10k), "Where your current 10k says you are"],
    ["Half predicted from 5k PB (22:10)", D.predictions.from_5k, pace(D.predictions.from_5k), "Pre-injury fitness. A target once you're back to it"],
    ["Garmin's half prediction", g.half || "–", pace(g.half), "From your watch"],
    ["Sub-1:55 half", "1:55:00", fmtPace(D.paces.sub_155), "Next milestone"],
  ];
  $("bench").innerHTML = `<thead><tr><th>Benchmark</th><th class="num">Half time</th><th class="num">Pace (${paceUnit()})</th><th>Note</th></tr></thead><tbody>` +
    rows.map(r => `<tr><td>${r[0]}</td><td class="num">${r[1]}</td><td class="num">${r[2]}</td><td class="name muted">${r[3]}</td></tr>`).join("") + "</tbody>";
}

function footer() {
  $("howto").innerHTML = `Refresh: run <code>python dashboard.py</code> (or <code>uv run dashboard.py</code>) and reload this page. Athlete: ${esc(D.athlete.height)}, ${D.athlete.weight_kg} kg. Load numbers are in Garmin training-load units, not TrainingPeaks TSS.`;
  const rows = Object.entries(D.status).map(([k, s]) => s ? `<tr><td>${k}</td><td class="num">${s.ok}</td><td class="num">${s.empty}</td><td class="num">${s.error}</td><td class="name">${esc(s.last_error || "")}</td></tr>` : `<tr><td>${k}</td><td colspan="4">not requested</td></tr>`);
  $("avail").innerHTML = `<thead><tr><th>What your account returned</th><th class="num">With data</th><th class="num">Empty</th><th class="num">Errors</th><th>Last error</th></tr></thead><tbody>${rows.join("")}</tbody>`;
}

function render() {
  charts.splice(0).forEach(c => c.destroy());
  document.querySelectorAll(".chart .empty").forEach(e => e.remove());
  document.querySelectorAll(".chart canvas").forEach(c => c.style.display = "");
  document.querySelectorAll(".seg button").forEach(b => b.setAttribute("aria-pressed", b.dataset.unit === unit));
  $("generated").textContent = "Updated " + D.generated;
  $("banner").innerHTML = D.demo ? `<div class="banner"><strong>Demo data.</strong> This page was built with <code>--demo</code>. Run <code>python dashboard.py</code> to use your Garmin data.</div>` : "";
  races(); tiles(); loadChart(); weeksChart(); vo2Chart(); recovery(); pacePanel(); runsTable(); bench(); footer();
}
document.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => {
  unit = b.dataset.unit; try { localStorage.setItem("unit", unit); } catch (e) {} render();
}));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
if (typeof Chart === "undefined") {
  document.querySelector(".wrap").insertAdjacentHTML("afterbegin", `<div class="banner">Charts need an internet connection to load Chart.js from cdnjs. The numbers and tables below still work.</div>`);
  window.Chart = function () { return { destroy() {} }; };
}
render();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a Garmin training dashboard (index.html).")
    ap.add_argument("--probe", action="store_true", help="print which metrics your account returns, then exit")
    ap.add_argument("--demo", action="store_true", help="build from made-up data, no Garmin login")
    ap.add_argument("--no-cache", action="store_true", help="re-download everything")
    ap.add_argument("--open", action="store_true", help="open the page in your browser when done")
    ap.add_argument("--out", type=Path, default=OUT_FILE, help=f"output file (default {OUT_FILE})")
    args = ap.parse_args()

    today = date.today()
    if args.demo:
        api, use_cache = DemoGarmin(today), False
    else:
        log("Logging in with saved Garmin tokens…")
        api, use_cache = connect(), not args.no_cache
    if args.probe:
        probe(api, today)
        return

    f = Fetcher(api, today, use_cache=use_cache)
    raw = fetch_all(f)
    data = build_data(raw, today, f.status, args.demo)
    args.out.write_text(render(data), encoding="utf-8")

    log("\nData returned by your account:")
    for k, s in data["status"].items():
        if not s:
            log(f"  {k:28} not requested")
            continue
        line = f"  {k:28} {s['ok']} with data, {s['empty']} empty, {s['error']} errors"
        if s["last_error"]:
            line += f"  (last error: {s['last_error']})"
        log(line)
    log(f"\nRuns in window: {len(data['runs'])} in last {RECENT_RUN_DAYS} days")
    log(f"Wrote {args.out}")
    if args.open:
        webbrowser.open(args.out.resolve().as_uri())


if __name__ == "__main__":
    main()
