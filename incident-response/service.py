#!/usr/bin/env python3
"""
Receives Grafana alert webhooks at POST /alerts, saves an incident bundle
(alert payload, Loki logs, Tempo traces) under incidents/<id>/, then runs
Claude Code headlessly in the order-tracker repo to investigate and fix it.

Run directly on the host (not containerized) so it can reuse your existing
`claude` CLI login: python3 service.py
"""
import json
import queue
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8001
LOKI_URL = "http://localhost:3100"
TEMPO_URL = "http://localhost:3200"
REPO_DIR = Path(__file__).resolve().parent.parent
INCIDENTS_DIR = Path(__file__).resolve().parent / "incidents"
CLAUDE_TIMEOUT_SECONDS = 900
# ponytail: a still-firing alert gets re-sent by Grafana on every evaluation
# tick; this stops us from launching a second concurrent claude run (and a
# second git commit race) for the same incident while one is in flight.
DEDUP_COOLDOWN_SECONDS = 1800

work_queue: "queue.Queue[dict]" = queue.Queue()
_recent_fingerprints: dict[str, float] = {}
_recent_lock = threading.Lock()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def fetch_loki_logs(service_name: str, endpoint: str, start: datetime, end: datetime) -> str:
    query = f'{{service_name="{service_name}"}}'
    if endpoint and endpoint != "unknown":
        query += f' |= "{endpoint}"'
    params = {
        "query": query,
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": "200",
    }
    url = f"{LOKI_URL}/loki/api/v1/query_range?{urllib.parse.urlencode(params)}"
    try:
        data = fetch_json(url)
    except Exception as exc:
        return f"(could not fetch logs from Loki: {exc})"

    lines = []
    for stream in data.get("data", {}).get("result", []):
        for ts_ns, line in stream.get("values", []):
            ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
            lines.append(f"{ts.isoformat()}  {line}")
    lines.sort()
    return "\n".join(lines) if lines else "(no matching log lines found)"


def fetch_tempo_traces(endpoint: str, start: datetime, end: datetime) -> dict:
    params = {
        "start": str(int(start.timestamp())),
        "end": str(int(end.timestamp())),
        "limit": "20",
    }
    if endpoint and endpoint != "unknown":
        params["tags"] = f"http.target={endpoint}"
    url = f"{TEMPO_URL}/api/search?{urllib.parse.urlencode(params)}"
    try:
        return fetch_json(url)
    except Exception as exc:
        return {"error": f"could not fetch traces from Tempo: {exc}"}


def build_incident_bundle(alert: dict) -> Path:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    endpoint = labels.get("http_target", "unknown")
    alertname = labels.get("alertname", "alert")
    starts_at = parse_time(alert["startsAt"]) if alert.get("startsAt") else datetime.now(timezone.utc)
    window_start = starts_at - timedelta(minutes=10)
    window_end = datetime.now(timezone.utc)

    slug = f"{starts_at.strftime('%Y%m%dT%H%M%SZ')}-{alertname}".replace(" ", "_")
    incident_dir = INCIDENTS_DIR / slug
    incident_dir.mkdir(parents=True, exist_ok=True)

    (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2))

    logs = fetch_loki_logs("order-tracker", endpoint, window_start, window_end)
    (incident_dir / "logs.txt").write_text(logs)

    traces = fetch_tempo_traces(endpoint, window_start, window_end)
    (incident_dir / "traces.json").write_text(json.dumps(traces, indent=2))

    summary = f"""# Incident: {alertname}

- Endpoint: {endpoint}
- Alert fired at: {starts_at.isoformat()}
- Logs/traces window: {window_start.isoformat()} to {window_end.isoformat()}
- Summary: {annotations.get('summary', '')}
- Description: {annotations.get('description', '')}
- Dashboard: http://localhost:3000/d/{annotations.get('dashboardUid', '')}

See logs.txt and traces.json in this folder for the raw data.
"""
    (incident_dir / "summary.md").write_text(summary)
    return incident_dir


def run_claude(incident_dir: Path) -> None:
    prompt = (
        f"An automated production alert fired for the order-tracker service. "
        f"The incident bundle is saved at {incident_dir} "
        f"(summary.md, logs.txt, traces.json, alert.json). "
        f"Read it, investigate the root cause in this repository, and fix it "
        f"(including a test if appropriate), then commit the fix with a clear "
        f"commit message. If you cannot find or fix the root cause, do not "
        f"commit anything -- instead write a clear escalation report to "
        f"{incident_dir}/escalation.md explaining what you found and why it "
        f"needs a human."
    )
    result = subprocess.run(
        ["claude", "-p", prompt, "--dangerously-skip-permissions", "--output-format", "json"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT_SECONDS,
    )
    (incident_dir / "claude-result.json").write_text(result.stdout)
    (incident_dir / "claude-stderr.log").write_text(result.stderr)


def should_process(fingerprint: str) -> bool:
    now = time.time()
    with _recent_lock:
        last = _recent_fingerprints.get(fingerprint)
        if last is not None and now - last < DEDUP_COOLDOWN_SECONDS:
            return False
        _recent_fingerprints[fingerprint] = now
        return True


def worker() -> None:
    while True:
        alert = work_queue.get()
        try:
            incident_dir = build_incident_bundle(alert)
            run_claude(incident_dir)
        except Exception as exc:
            print(f"[incident-response] failed to process alert: {exc}")
        finally:
            work_queue.task_done()


class AlertHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/alerts":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")

        for alert in payload.get("alerts", []):
            if alert.get("status") != "firing":
                continue
            fingerprint = alert.get("fingerprint", json.dumps(alert.get("labels", {})))
            if should_process(fingerprint):
                work_queue.put(alert)

        self.send_response(200)
        self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[incident-response] {fmt % args}")


def main() -> None:
    INCIDENTS_DIR.mkdir(exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AlertHandler)
    print(f"[incident-response] listening on :{PORT}, repo={REPO_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    main()
