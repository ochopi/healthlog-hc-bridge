#!/usr/bin/env python3
"""healthlog-hc-bridge

Receives the JSON that the HC Webhook Android app POSTs (Health Connect data)
and forwards it to HealthLog's ingest API (/api/measurements, /api/workouts/batch).

Only the Python standard library is used. No health data is stored on disk.
"""
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HEALTHLOG_URL = os.environ.get("HEALTHLOG_URL", "").rstrip("/")
MEASUREMENT_TOKEN = os.environ.get("HEALTHLOG_MEASUREMENT_TOKEN", "")
WORKOUT_TOKEN = os.environ.get("HEALTHLOG_WORKOUT_TOKEN", "")  # optional
BRIDGE_KEY = os.environ.get("BRIDGE_KEY", "")  # shared secret with HC Webhook
PORT = int(os.environ.get("PORT", "8088"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
LOG_PAYLOAD = os.environ.get("LOG_PAYLOAD", "false").lower() in ("1", "true", "yes")
MAX_BODY = 20 * 1024 * 1024

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hc-bridge")


# ---------------------------------------------------------------- helpers
def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Simple one-value-per-record types: key -> (HealthLog type, value field, time field)
SIMPLE = {
    "weight": ("WEIGHT", "kilograms", "time"),
    "body_fat": ("BODY_FAT", "percentage", "time"),
    "resting_heart_rate": ("RESTING_HEART_RATE", "bpm", "time"),
    "heart_rate_variability": ("HRV_RMSSD", "rmssd_millis", "time"),
    "oxygen_saturation": ("OXYGEN_SATURATION", "percentage", "time"),
    "respiratory_rate": ("RESPIRATORY_RATE", "rate", "time"),
    "vo2_max": ("VO2_MAX", "ml_per_kg_per_min", "time"),
    "body_temperature": ("BODY_TEMPERATURE", "celsius", "time"),
    "steps": ("ACTIVITY_STEPS", "count", "end_time"),
}

# Health Connect sleep stage -> HealthLog sleepStage (by name).
# HC Webhook sends the stage as a string; handle both enum names and numbers.
HC_STAGE_NUM = {
    "0": "ASLEEP", "1": "AWAKE", "2": "ASLEEP", "3": None,  # 3 = out of bed
    "4": "CORE", "5": "DEEP", "6": "REM", "7": "AWAKE",
}


def map_stage(raw):
    s = str(raw).strip().upper()
    if s in HC_STAGE_NUM:
        return HC_STAGE_NUM[s]
    if "OUT_OF_BED" in s:
        return None
    if "DEEP" in s:
        return "DEEP"
    if "REM" in s:
        return "REM"
    if "LIGHT" in s or "CORE" in s:
        return "CORE"
    if "AWAKE" in s:
        return "AWAKE"
    if "IN_BED" in s:
        return "IN_BED"
    return "ASLEEP"  # SLEEPING / UNKNOWN / anything else


SPORT = [  # substring of HC exercise type -> HealthLog sportType
    ("RUNNING", "running"), ("WALKING", "walking"), ("BIKING", "cycling"),
    ("CYCLING", "cycling"), ("HIKING", "hiking"), ("SWIMMING", "swimming"),
    ("ROWING", "rowing"), ("ELLIPTICAL", "elliptical"), ("STAIR", "stairClimber"),
    ("YOGA", "yoga"), ("STRENGTH", "strength"), ("WEIGHTLIFTING", "strength"),
    ("HIGH_INTENSITY", "hiit"), ("HIIT", "hiit"), ("DANCING", "dance"),
    ("GOLF", "golf"), ("BADMINTON", "badminton"), ("TENNIS", "tennis"),
    ("BASKETBALL", "basketball"), ("SOCCER", "soccer"), ("FOOTBALL_SOCCER", "soccer"),
]


def map_sport(raw):
    s = str(raw).upper()
    for needle, sport in SPORT:
        if needle in s:
            return sport
    return "other"


# ---------------------------------------------------------------- conversion
def build_measurements(payload):
    """Return a list of (label, [body, ...]) — each item is one POST /api/measurements."""
    out = []
    for key, (hl_type, vfield, tfield) in SIMPLE.items():
        for r in payload.get(key, []):
            if vfield not in r or tfield not in r:
                continue
            body = {"type": hl_type, "value": r[vfield], "measuredAt": r[tfield]}
            out.append((f"{hl_type}@{r[tfield]}", body))

    for r in payload.get("blood_pressure", []):
        t = r.get("time")
        if t and "systolic" in r and "diastolic" in r:
            out.append((f"BP@{t}", [
                {"type": "BLOOD_PRESSURE_SYS", "value": r["systolic"], "measuredAt": t},
                {"type": "BLOOD_PRESSURE_DIA", "value": r["diastolic"], "measuredAt": t},
            ]))

    now = datetime.now(timezone.utc)
    for s in payload.get("sleep", []):
        stages = s.get("stages") or []
        if not stages and s.get("session_end_time") and s.get("duration_seconds"):
            end = parse_ts(s["session_end_time"])
            stages = [{
                "stage": "SLEEPING",
                "start_time": iso(end - timedelta(seconds=s["duration_seconds"])),
                "end_time": s["session_end_time"],
            }]
        for st in stages:
            stage = map_stage(st.get("stage", ""))
            if stage is None or not st.get("start_time") or not st.get("end_time"):
                continue
            if parse_ts(st["end_time"]) > now:
                continue
            out.append((f"SLEEP-{stage}@{st['start_time']}", {
                "type": "SLEEP_DURATION", "sleepStage": stage,
                "startDate": st["start_time"], "endDate": st["end_time"],
            }))
    return out


def build_workouts(payload):
    ws = []
    for e in payload.get("exercise", []):
        if not e.get("start_time") or not e.get("end_time"):
            continue
        w = {
            "sportType": map_sport(e.get("type", "")),
            "startedAt": e["start_time"],
            "endedAt": e["end_time"],
            "externalId": f"hc-{e.get('type', 'x')}-{e['start_time']}",
        }
        if e.get("distance_meters") is not None:
            w["totalDistanceM"] = e["distance_meters"]
        ws.append(w)
    return ws


# ---------------------------------------------------------------- HTTP out
def post(path, token, body):
    """POST JSON to HealthLog. Returns (status, response_text). Raises on network error."""
    if DRY_RUN:
        log.info("DRY_RUN POST %s %s", path, json.dumps(body)[:300])
        return 201, "dry-run"
    req = urllib.request.Request(
        HEALTHLOG_URL + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "healthlog-hc-bridge/1.0",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < 2:
                time.sleep(min(int(e.headers.get("Retry-After", "5")), 30))
                continue
            return e.code, text
    return 429, "rate limited"


def forward(payload):
    stats = {"sent": 0, "duplicate": 0, "skipped": 0, "failed": 0}
    for label, body in build_measurements(payload):
        status, text = post("/api/measurements", MEASUREMENT_TOKEN, body)
        if status in (200, 201):
            stats["duplicate" if '"duplicate"' in text else "sent"] += 1
        elif status == 409:
            stats["duplicate"] += 1
        elif status in (401, 403):
            raise PermissionError(f"HealthLog rejected token ({status}): {text[:200]}")
        elif status == 422:
            stats["skipped"] += 1
            log.warning("skipped %s: %s", label, text[:200])
        else:
            stats["failed"] += 1
            log.error("failed %s: %s %s", label, status, text[:200])

    workouts = build_workouts(payload)
    if workouts:
        if not WORKOUT_TOKEN:
            log.info("%d exercise sessions ignored (no HEALTHLOG_WORKOUT_TOKEN)", len(workouts))
        else:
            status, text = post("/api/workouts/batch", WORKOUT_TOKEN, {"workouts": workouts})
            if status in (200, 201):
                stats["sent"] += len(workouts)
            else:
                stats["failed"] += len(workouts)
                log.error("workouts failed: %s %s", status, text[:200])
    return stats


# ---------------------------------------------------------------- HTTP in
class Handler(BaseHTTPRequestHandler):
    server_version = "hc-bridge"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._reply(200, {"ok": True, "service": "healthlog-hc-bridge", "dry_run": DRY_RUN})

    def do_POST(self):
        if self.path.rstrip("/") != "/healthconnect":
            return self._reply(404, {"error": "not found"})
        if BRIDGE_KEY and self.headers.get("X-Bridge-Key") != BRIDGE_KEY:
            return self._reply(401, {"error": "bad key"})
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > MAX_BODY:
            return self._reply(400, {"error": "bad length"})
        try:
            payload = json.loads(self.rfile.read(n))
        except ValueError:
            return self._reply(400, {"error": "invalid json"})
        keys = [k for k, v in payload.items() if isinstance(v, list)]
        log.info("received from app v%s: %s", payload.get("app_version"),
                 {k: len(payload[k]) for k in keys})
        if LOG_PAYLOAD:
            log.info("payload: %s", json.dumps(payload)[:4000])
        try:
            stats = forward(payload)
        except PermissionError as e:
            log.error("%s", e)
            return self._reply(502, {"error": "healthlog auth"})
        except (urllib.error.URLError, OSError) as e:
            log.error("HealthLog unreachable: %s", e)
            return self._reply(502, {"error": "healthlog unreachable"})  # app will retry
        log.info("result: %s", stats)
        self._reply(200, {"ok": True, **stats})


def main():
    if not DRY_RUN and (not HEALTHLOG_URL or not MEASUREMENT_TOKEN):
        sys.exit("Set HEALTHLOG_URL and HEALTHLOG_MEASUREMENT_TOKEN (or DRY_RUN=true)")
    log.info("listening on :%d -> %s (dry_run=%s)", PORT, HEALTHLOG_URL or "-", DRY_RUN)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
