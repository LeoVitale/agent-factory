#!/usr/bin/env python3
"""Read-only dashboard of one factory instance.

Shows every issue on the pipeline, the pull requests, the stops that need the person and the station
metrics. It reads the factory's state file, the Canvas and GitHub, and writes nothing. The factory starts
it in a thread; it also runs alone:

  python3 dashboard.py          serve on dashboard.port (8201)
  DASHBOARD_PORT=8202           another port
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import settings

HERE = Path(__file__).resolve().parent
CONFIG = settings.load()
STATE_PATH = Path(os.environ.get("FACTORY_STATE", "/state/state.json"))
CANVAS_URL = os.environ.get("CANVAS_URL", "http://canvas:8000").rstrip("/")
CANVAS_KEY_FILE = Path(os.environ.get("CANVAS_KEY_FILE", "/canvas-state/agent-canvas/api-key.txt"))
SLACK_BOT = os.environ.get("SLACK_BOT_TOKEN", "")
REPO = CONFIG["github_repo"]
MAX_ROUNDS = CONFIG["max_fix_rounds"]
DEMO_ON = CONFIG.get("demo", {}).get("enabled", False)
SETTINGS = CONFIG.get("dashboard", {})

STATIONS = ("plan", "build", "verify", "judge", "fix", "demo")
FINISHED = ("finished", "error", "stuck", "waiting_for_confirmation")
LABELS = {
    "queued": "Na fila", "blocked": "Bloqueada", "plan": "Plan", "build": "Build", "ci": "CI", "verify": "Verify",
    "judge": "Judge", "fix": "Fix", "final": "Checagem final", "approval": "Aprovar o plano", "demo": "Demo", "review": "Em revisão",
    "waiting": "Precisa de você", "dropped": "Saiu", "refused": "Recusada", "MERGED": "Mergeado", "merged": "Mergeado", "CLOSED": "PR fechado",
}
GROUPS = {"waiting": "needs_you", "approval": "needs_you", "refused": "needs_you", "review": "review", "merged": "done", "queued": "later", "blocked": "later",
          "dropped": "out"}


# ── sources ───────────────────────────────────────────────────────────────────

class Cache:
    """Values that expire after `ttl` seconds; `ttl=None` keeps them for good."""

    def __init__(self):
        self.values = {}
        self.lock = threading.Lock()

    def get(self, key, ttl, fetch):
        with self.lock:
            hit = self.values.get(key)
        if hit and (hit[1] is None or time.time() < hit[1]):
            return hit[0]
        value, keep = fetch()
        with self.lock:
            self.values[key] = (value, None if keep else time.time() + ttl)
        return value


CACHE = Cache()


def http(url, headers=None, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def gh(*args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])}: {(p.stderr or p.stdout).strip()[:300]}")
    return json.loads(p.stdout) if p.stdout.strip() else None


def canvas(path):
    return http(CANVAS_URL + path, {"X-Session-API-Key": CANVAS_KEY_FILE.read_text().strip()})


def conversation(conv_id):
    """Status, size and duration of one Canvas conversation. A finished one never changes, so it stays cached."""
    def fetch():
        conv = canvas(f"/api/conversations/{conv_id}")
        info = {"status": conv.get("execution_status"), "events": canvas(f"/api/conversations/{conv_id}/events/count"),
                "created_at": conv.get("created_at"), "updated_at": conv.get("updated_at"),
                "model": conv.get("current_model_id")}
        return info, info["status"] in FINISHED
    return CACHE.get(("conv", conv_id), 10, fetch)


def slack_link(channel, ts):
    if not (SLACK_BOT and channel and ts):
        return None

    def fetch():
        out = http("https://slack.com/api/chat.getPermalink?" + f"channel={channel}&message_ts={ts}",
                   {"Authorization": f"Bearer {SLACK_BOT}"})
        return out.get("permalink"), out.get("ok", False)
    return CACHE.get(("slack", channel, ts), 300, fetch)


def ci(head):
    """CI on a commit from Actions runs and commit statuses, the way the factory reads it."""
    def fetch():
        out = {"pass": [], "fail": [], "pending": []}
        latest = {}
        for r in gh("api", f"repos/{REPO}/actions/runs?head_sha={head}&per_page=100")["workflow_runs"]:
            if r["workflow_id"] not in latest or r["id"] > latest[r["workflow_id"]]["id"]:
                latest[r["workflow_id"]] = r
        for r in latest.values():
            if r["status"] != "completed":
                out["pending"].append(r["name"])
            elif r["conclusion"] in ("success", "skipped", "neutral"):
                out["pass"].append(r["name"])
            else:
                out["fail"].append(r["name"])
        for s in gh("api", f"repos/{REPO}/commits/{head}/status")["statuses"]:
            out[{"success": "pass", "pending": "pending"}.get(s["state"], "fail")].append(s["context"])
        state = "fail" if out["fail"] else "pending" if out["pending"] else "pass" if out["pass"] else "none"
        return {"state": state, **out}, state in ("pass", "fail")
    return CACHE.get(("ci", head), 30, fetch)


def verdict(number, head):
    """The last review on the head commit: the-judge posts COMMENT reviews with its intended verdict in the body."""
    def fetch():
        for review in reversed(gh("api", f"repos/{REPO}/pulls/{number}/reviews") or []):
            if review.get("commit_id") != head:
                continue
            if review["state"] in ("APPROVED", "CHANGES_REQUESTED"):
                v = "APPROVE" if review["state"] == "APPROVED" else "REQUEST_CHANGES"
            else:
                intended = re.search(r"Intended verdict: \*\*(APPROVE|REQUEST_CHANGES)\*\*", review.get("body") or "")
                v = intended.group(1) if intended else "COMMENT"
            return {"verdict": v, "url": review["html_url"]}, False
        return None, False
    return CACHE.get(("verdict", number, head), 60, fetch)


def pull_requests():
    def fetch():
        fields = "number,title,url,state,isDraft,headRefName,baseRefName,headRefOid,mergeable,updatedAt,author"
        prs = gh("pr", "list", "--repo", REPO, "--state", "all", "--limit", "40", "--json", fields) or []
        return prs, False
    return CACHE.get("prs", 60, fetch)


# ── views ─────────────────────────────────────────────────────────────────────

def minutes(start, end):
    if not (start and end):
        return None
    a, b = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (start, end))
    return max(0, round((b - a).total_seconds() / 60))


def steps(rec):
    """The same progress line the Slack thread root shows."""
    keys = ["plan", "build"] + (["fix"] if rec.get("round") else []) + ["verify", "judge", "ci"]
    keys += (["demo"] if DEMO_ON else []) + ["review"]
    stage = rec["stage"]
    at = rec.get("waiting_stage") if stage == "waiting" else stage
    if at == "final":
        at = "ci"
    if at == "merged":
        at = "review"
    pos = keys.index(at) if at in keys else -1
    if stage in ("queued", "blocked", "refused"):
        pos = 0
    planned = any(c["station"] == "plan" for c in rec.get("conversations", []))
    out = []
    for i, key in enumerate(keys):
        label = f"Fix {rec['round']}/{MAX_ROUNDS}" if key == "fix" else LABELS[key]
        if i < pos:
            state = "skipped" if key == "plan" and not planned else "done"
        elif i > pos or stage in ("queued", "blocked", "refused"):
            state = "todo"
        else:
            state = "stopped" if stage == "waiting" else "done" if key == "review" else "current"
        out.append({"key": key, "label": label, "state": state})
    return out


def since(rec):
    stage = rec["stage"]
    if stage == "waiting":
        return rec.get("waiting_since")
    if stage == "ci":
        return rec.get("ci_since")
    if stage in STATIONS:
        return rec.get("station_started_at")
    return None


def issue_view(rec, convs, pull):
    """`pull` is the issue's pull request from `pull_view`; once it is merged or closed the issue is done."""
    stage = rec["stage"]
    closed = pull and pull["state"] in ("MERGED", "CLOSED") and stage in ("review", "dropped")
    live = rec.get("conversation_id") if stage in STATIONS else None
    last_demo = (rec.get("demos") or [None])[-1]
    history = [{"station": c["station"], "at": c["at"], "url": f"{CONFIG['canvas_public_url']}/conversations/{c['id']}",
                **{k: convs.get(c["id"], {}).get(k) for k in ("status", "events", "model")}}
               for c in rec.get("conversations", [])]
    return {
        "id": rec["identifier"], "title": rec["title"], "url": rec["issue_url"], "project": rec.get("project"),
        "stage": stage, "label": LABELS[pull["state"]] if closed else LABELS.get(stage, stage),
        "group": "done" if closed else GROUPS.get(stage, "running"),
        "waiting_stage": LABELS.get(rec.get("waiting_stage") or "", rec.get("waiting_stage")),
        "question": rec.get("question") if stage == "waiting" else None,
        "since": since(rec), "round": rec.get("round", 0), "max_rounds": MAX_ROUNDS,
        "blocked_on": rec.get("blocked_on") or [], "base": rec.get("base"), "branch": rec.get("branch"),
        "pr": {"number": rec["pr_number"], "url": rec["pr_url"], **({k: pull[k] for k in ("state", "draft", "ci", "verdict",
               "mergeable")} if pull else {})} if rec.get("pr_number") else None,
        "review_url": rec.get("last_review"), "demo": last_demo,
        "live_url": f"{CONFIG['canvas_public_url']}/conversations/{live}" if live else None,
        "slack_url": slack_link(rec.get("slack_channel"), rec.get("slack_ts")),
        "steps": steps(rec), "conversations": history,
    }


def pull_view(pr, by_branch):
    rec = by_branch.get(pr["headRefName"])
    view = {"number": pr["number"], "title": pr["title"], "url": pr["url"], "state": pr["state"],
            "draft": pr["isDraft"], "head": pr["headRefName"], "base": pr["baseRefName"],
            "mergeable": pr.get("mergeable"), "updated_at": pr["updatedAt"],
            "author": (pr.get("author") or {}).get("login"),
            "issue": {"id": rec["identifier"], "url": rec["issue_url"], "stage": LABELS.get(rec["stage"], rec["stage"])}
            if rec else None, "ci": None, "verdict": None}
    if pr["state"] == "OPEN":
        view["ci"] = ci(pr["headRefOid"])
        view["verdict"] = verdict(pr["number"], pr["headRefOid"])
    return view


def first_pass(order, station):
    """True when the first run of `station` was not followed by a Fix; None when it never ran or nothing followed."""
    if station not in order:
        return None
    rest = order[order.index(station) + 1:]
    return None if not rest else rest[0] != "fix"


def metrics(issues, convs):
    per_station = {s: {"runs": 0, "events": 0, "minutes": []} for s in STATIONS}
    rows = []
    for rec in issues:
        # A retry reruns the same station after a Cursor connection drop, so it is not a step of its own.
        order = [c["station"] for c in rec.get("conversations", []) if not c.get("retry")]
        if not order:
            continue
        for c in rec["conversations"]:
            info = convs.get(c["id"], {})
            s = per_station.setdefault(c["station"], {"runs": 0, "events": 0, "minutes": []})
            s["runs"] += 1
            s["events"] += info.get("events") or 0
            if info.get("status") in FINISHED:
                s["minutes"].append(minutes(info.get("created_at"), info.get("updated_at")))
        ready = None
        if rec["stage"] == "review":
            demo = (rec.get("demos") or [None])[-1]
            last = convs.get(rec["conversations"][-1]["id"], {})
            ready = demo["at"] if demo else last.get("updated_at")
        rows.append({
            "id": rec["identifier"], "url": rec["issue_url"], "stage": LABELS.get(rec["stage"], rec["stage"]),
            "conversations": len(rec["conversations"]), "fix_rounds": order.count("fix"),
            "events": sum(convs.get(c["id"], {}).get("events") or 0 for c in rec["conversations"]),
            "verify_first": first_pass(order, "verify"), "judge_first": first_pass(order, "judge"),
            "minutes_to_review": minutes(rec["conversations"][0]["at"], ready) if ready else None,
        })
    total = sum(s["events"] for s in per_station.values()) or 1
    stations = [{"station": k, "label": LABELS[k], "runs": v["runs"], "events": v["events"],
                 "share": round(100 * v["events"] / total), "avg_events": round(v["events"] / v["runs"]) if v["runs"]
                 else 0, "avg_minutes": round(sum(v["minutes"]) / len(v["minutes"])) if v["minutes"] else None}
                for k, v in per_station.items() if v["runs"]]

    def rate(key):
        known = [r[key] for r in rows if r[key] is not None]
        return round(100 * sum(known) / len(known)) if known else None
    reviewed = [r["minutes_to_review"] for r in rows if r["minutes_to_review"] is not None]
    return {"stations": stations, "issues": rows, "summary": {
        "conversations": sum(s["runs"] for s in stations), "events": sum(s["events"] for s in stations),
        "verify_first_rate": rate("verify_first"), "judge_first_rate": rate("judge_first"),
        "avg_fix_rounds": round(sum(r["fix_rounds"] for r in rows) / len(rows), 1) if rows else None,
        "avg_minutes_to_review": round(sum(reviewed) / len(reviewed)) if reviewed else None,
    }}


def board():
    state = json.loads(STATE_PATH.read_text())
    issues = sorted(state["issues"].values(), key=lambda r: r["identifier"])
    errors = []
    ids = [c["id"] for r in issues for c in r.get("conversations", [])]
    ids += [m["conversation_id"] for m in state.get("mentions", {}).values() if m.get("status") == "active"]
    convs = {}

    def load(conv_id):
        try:
            convs[conv_id] = conversation(conv_id)
        except Exception as e:
            errors.append(f"Canvas: {e}")
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(load, ids))

    by_branch = {r["branch"]: r for r in issues if r.get("branch")}
    pulls = []
    try:
        with ThreadPoolExecutor(6) as pool:
            pulls = list(pool.map(lambda pr: pull_view(pr, by_branch), pull_requests()))
    except Exception as e:
        errors.append(f"GitHub: {e}")
    by_number = {p["number"]: p for p in pulls}

    views = []
    for rec in issues:
        try:
            views.append(issue_view(rec, convs, by_number.get(rec.get("pr_number"))))
        except Exception as e:
            errors.append(f"{rec.get('identifier')}: {e}")

    mentions = [{"url": f"{CONFIG['canvas_public_url']}/conversations/{m['conversation_id']}",
                 "since": datetime.fromtimestamp(m["since"], timezone.utc).isoformat()}
                for m in state.get("mentions", {}).values() if m.get("status") == "active"]
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "issues": views, "pulls": pulls,
            "mentions": mentions, "metrics": metrics(issues, convs), "errors": sorted(set(errors))[:10],
            "repo": REPO, "canvas_url": CONFIG["canvas_public_url"]}


# ── server ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self.send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/board":
            try:
                return self.send(200, json.dumps(board()).encode(), "application/json")
            except Exception as e:
                return self.send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        self.send(404, b"not found", "text/plain")

    def send(self, code, body, kind):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve():
    port = int(os.environ.get("DASHBOARD_PORT", SETTINGS.get("port", 8201)))
    bind = os.environ.get("DASHBOARD_BIND", SETTINGS.get("bind", "0.0.0.0"))
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def start():
    """Serve in a daemon thread, so the factory loop never waits on the dashboard."""
    server = serve()
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    threading.Thread(target=board, name="dashboard-warmup", daemon=True).start()
    return server.server_address


if __name__ == "__main__":
    s = serve()
    print(f"dashboard on http://{s.server_address[0]}:{s.server_address[1]}", flush=True)
    s.serve_forever()
