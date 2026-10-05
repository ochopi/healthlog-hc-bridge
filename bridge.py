#!/usr/bin/env python3
"""healthlog-hc-bridge

Receives the JSON that the HC Webhook Android app POSTs (Health Connect data)
and forwards it to HealthLog's ingest API (/api/measurements, /api/workouts/batch).

Only the Python standard library is used. No health data is stored on disk.
"""
import base64
import collections
import hmac
import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.1.0"
START = time.time()
HERE = os.path.dirname(os.path.abspath(__file__))

HEALTHLOG_URL = os.environ.get("HEALTHLOG_URL", "").rstrip("/")
MEASUREMENT_TOKEN = os.environ.get("HEALTHLOG_MEASUREMENT_TOKEN", "")
WORKOUT_TOKEN = os.environ.get("HEALTHLOG_WORKOUT_TOKEN", "")  # optional
BRIDGE_KEY = os.environ.get("BRIDGE_KEY", "")  # shared secret with HC Webhook
PORT = int(os.environ.get("PORT", "8088"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
LOG_PAYLOAD = os.environ.get("LOG_PAYLOAD", "false").lower() in ("1", "true", "yes")
MAX_BODY = 300 * 1024 * 1024  # a multi-year backfill can be big
# Stay below HealthLog's single-write limit (300/min shared bucket)
RATE_PER_SEC = float(os.environ.get("RATE_PER_SEC", "4"))

# Optional HTTP Basic auth for the dashboard (/ and /status.json). Off when empty.
DASH_USER = os.environ.get("DASH_USER", "")
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")

JOBS = queue.Queue()  # (job, payload) waiting to be forwarded
STATS = {"received_jobs": 0, "records_total": 0, "records_done": 0, "sent": 0,
         "duplicate": 0, "skipped": 0, "failed": 0, "current": "idle"}
STATE = {"last_received_at": None, "last_write_ok_at": None, "token_ok": None,
         "current_job": None}
HISTORY = collections.deque(maxlen=25)  # recent job summaries (counts only, no values)
EVENTS = collections.deque(maxlen=40)   # recent warnings/errors


class RingHandler(logging.Handler):
    """Keeps the last warnings/errors in memory for the dashboard."""

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            EVENTS.append({"at": now_iso(), "level": record.levelname,
                           "msg": record.getMessage()[:200]})


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("hc-bridge")
log.addHandler(RingHandler())


# ---------------------------------------------------------------- helpers
def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def err_code(text):
    """Short error code from a HealthLog error body (never echoes values)."""
    try:
        d = json.loads(text)
        return (d.get("meta") or {}).get("errorCode") or str(d.get("error"))[:60]
    except (ValueError, AttributeError):
        return "unparseable"


def num(v):
    """Round floats (Health Connect gives 66.80000305175781) to 2 decimals."""
    return round(v, 2) if isinstance(v, float) else v


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
            body = {"type": hl_type, "value": num(r[vfield]), "measuredAt": r[tfield]}
            out.append((f"{hl_type}@{r[tfield]}", body))

    for r in payload.get("blood_pressure", []):
        t = r.get("time")
        if t and "systolic" in r and "diastolic" in r:
            out.append((f"BP@{t}", [
                {"type": "BLOOD_PRESSURE_SYS", "value": num(r["systolic"]), "measuredAt": t},
                {"type": "BLOOD_PRESSURE_DIA", "value": num(r["diastolic"]), "measuredAt": t},
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
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        retry_after = e.headers.get("Retry-After", "")
        text = e.read().decode("utf-8", "replace")
        if e.code == 429 and retry_after.isdigit():
            text = f"retry-after={retry_after} {text}"
        return e.code, text


def post_retry(path, token, body):
    """Keep retrying while HealthLog is down, restarting or rate limiting us."""
    delay = 2
    while True:
        try:
            status, text = post(path, token, body)
        except (urllib.error.URLError, OSError) as e:
            status, text = 0, str(e)
        if status == 429:
            wait = 30
            if text.startswith("retry-after="):
                wait = min(int(text.split()[0].split("=")[1]), 120)
            log.warning("rate limited, waiting %ss", wait)
            time.sleep(wait)
        elif status == 0 or status >= 500:
            log.warning("HealthLog unavailable (%s %s), retry in %ss", status, text[:100], delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            return status, text


def process(job, payload):
    """Forward one payload to HealthLog (runs in the background worker)."""
    items = build_measurements(payload)
    workouts = build_workouts(payload)
    STATS["records_total"] += len(items)
    job.update(status="running", total=len(items), done=0, workouts=len(workouts),
               started_at=now_iso())
    STATE["current_job"] = job
    base = {k: STATS[k] for k in ("sent", "duplicate", "skipped", "failed")}
    log.info("job %d started: %d measurements, %d workouts", job["id"], len(items), len(workouts))
    pause = 1.0 / RATE_PER_SEC if RATE_PER_SEC > 0 else 0
    started = time.time()

    for label, body in items:
        status, text = post_retry("/api/measurements", MEASUREMENT_TOKEN, body)
        if status in (200, 201):
            STATS["duplicate" if '"duplicate"' in text else "sent"] += 1
            STATE["last_write_ok_at"], STATE["token_ok"] = now_iso(), True
        elif status == 409:
            STATS["duplicate"] += 1
            STATE["token_ok"] = True
        elif status in (401, 403):
            STATE["token_ok"] = False
            log.error("HealthLog rejected the token (%s): %s. Job aborted.", status, err_code(text))
            job.update(status="aborted: token rejected")
            break
        elif status == 422:
            STATS["skipped"] += 1
            STATE["token_ok"] = True
            log.warning("skipped %s: %s", label, err_code(text))  # label has no value
        else:
            STATS["failed"] += 1
            log.error("failed %s: HTTP %s %s", label, status, err_code(text))
        STATS["records_done"] += 1
        job["done"] += 1
        if STATS["records_done"] % 500 == 0:
            log.info("progress: %d/%d | %s", STATS["records_done"], STATS["records_total"],
                     {k: STATS[k] for k in ("sent", "duplicate", "skipped", "failed")})
        if not DRY_RUN:
            time.sleep(pause)
    else:
        if workouts:
            if not WORKOUT_TOKEN:
                log.info("%d exercise sessions ignored (no HEALTHLOG_WORKOUT_TOKEN)", len(workouts))
            else:
                for i in range(0, len(workouts), 100):  # API limit: 100 per call
                    chunk = workouts[i:i + 100]
                    status, text = post_retry("/api/workouts/batch", WORKOUT_TOKEN,
                                              {"workouts": chunk})
                    key = "sent" if status in (200, 201) else "failed"
                    STATS[key] += len(chunk)
                    if key == "failed":
                        log.error("workouts failed: HTTP %s %s", status, err_code(text))
        job["status"] = "done"

    job["finished_at"] = now_iso()
    job["seconds"] = round(time.time() - started)
    job["result"] = {k: STATS[k] - base[k] for k in base}
    log.info("job %d %s in %.0fs | %s", job["id"], job["status"], time.time() - started,
             job["result"])


def worker():
    while True:
        job, payload = JOBS.get()
        STATS["current"] = "working"
        try:
            process(job, payload)
        except Exception:
            job["status"] = "crashed"
            log.exception("job crashed")
        finally:
            STATE["current_job"] = None
            STATS["current"] = "idle"
            HISTORY.append(job)
            JOBS.task_done()


# ---------------------------------------------------------------- status
_PING = {"at": 0, "value": None}


def ping_healthlog():
    """Is HealthLog answering? Any HTTP response counts. Cached for 15 s."""
    if time.time() - _PING["at"] < 15 and _PING["value"] is not None:
        return _PING["value"]
    res = {"host": urllib.parse.urlparse(HEALTHLOG_URL).netloc or "-", "reachable": False,
           "latency_ms": None}
    if HEALTHLOG_URL:
        t = time.time()
        try:
            urllib.request.urlopen(HEALTHLOG_URL + "/", timeout=3).close()
            res["reachable"] = True
        except urllib.error.HTTPError:
            res["reachable"] = True  # it answered, just not 200
        except (urllib.error.URLError, OSError):
            pass
        res["latency_ms"] = round((time.time() - t) * 1000)
    _PING.update(at=time.time(), value=res)
    return res


def memory_mb():
    for p in ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            with open(p) as f:
                return round(int(f.read().strip()) / 1048576)
        except (OSError, ValueError):
            continue
    return None


def status_snapshot():
    cur = STATE["current_job"]
    current = None
    if cur and cur.get("total"):
        elapsed = max(time.time() - parse_ts(cur["started_at"]).timestamp(), 1)
        rate = cur["done"] / elapsed
        eta = round((cur["total"] - cur["done"]) / rate) if rate > 0 else None
        current = {"id": cur["id"], "done": cur["done"], "total": cur["total"],
                   "rate_per_s": round(rate, 2), "eta_s": eta, "counts": cur["counts"]}
    last = STATE["last_received_at"]
    return {
        "service": "healthlog-hc-bridge", "version": VERSION, "dry_run": DRY_RUN,
        "now": now_iso(), "uptime_s": round(time.time() - START),
        "memory_mb": memory_mb(), "rate_limit_per_s": RATE_PER_SEC,
        "healthlog": {**ping_healthlog(), "token_ok": STATE["token_ok"],
                      "last_write_ok_at": STATE["last_write_ok_at"],
                      "workout_token": bool(WORKOUT_TOKEN)},
        "app": {"last_contact_at": last,
                "last_contact_age_s": round(time.time() - parse_ts(last).timestamp())
                if last else None},
        "queue": {"jobs_waiting": JOBS.qsize(), "current": current},
        "totals": {k: STATS[k] for k in
                   ("received_jobs", "sent", "duplicate", "skipped", "failed")},
        "recent_jobs": ([dict(cur)] if cur else []) + list(HISTORY)[::-1],
        "recent_events": list(EVENTS)[::-1],
    }


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

    def _authorized(self):
        if not (DASH_USER and DASH_PASSWORD):
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                ok_user = hmac.compare_digest(user, DASH_USER)
                ok_pw = hmac.compare_digest(pw, DASH_PASSWORD)
                return ok_user and ok_pw
            except ValueError:
                pass
        return False

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/healthz":  # used by the Docker HEALTHCHECK, no auth
            return self._reply(200, {"ok": True})
        if path not in ("/", "/status.json"):
            return self._reply(404, {"error": "not found"})
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="hc-bridge"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        wants_html = "text/html" in self.headers.get("Accept", "")
        if path == "/" and wants_html:
            try:
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    data = f.read()
            except OSError:
                return self._reply(500, {"error": "dashboard.html missing"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        self._reply(200, status_snapshot())  # curl / fetch get JSON

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
        # Answer right away so the app never times out; the worker does the slow part.
        STATS["received_jobs"] += 1
        STATE["last_received_at"] = now_iso()
        job = {"id": STATS["received_jobs"], "received_at": STATE["last_received_at"],
               "status": "queued", "app_version": str(payload.get("app_version", ""))[:20],
               "counts": {k: len(payload[k]) for k in keys}}  # counts only, never values
        JOBS.put((job, payload))
        self._reply(202, {"ok": True, "queued": True, "jobs_waiting": JOBS.qsize()})


def main():
    if not DRY_RUN and (not HEALTHLOG_URL or not MEASUREMENT_TOKEN):
        sys.exit("Set HEALTHLOG_URL and HEALTHLOG_MEASUREMENT_TOKEN (or DRY_RUN=true)")
    threading.Thread(target=worker, daemon=True).start()
    log.info("listening on :%d -> %s (dry_run=%s)", PORT, HEALTHLOG_URL or "-", DRY_RUN)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
