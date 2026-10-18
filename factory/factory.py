#!/usr/bin/env python3
"""Hestia agentic factory orchestrator.

Polls Linear and GitHub, runs one OpenHands (Cursor ACP) conversation per
station - plan, build, verify, judge, fix - and checks each station's
artifacts before moving on. Talks to the person through one Slack thread per
issue (Socket Mode) and through Linear comments. No LLM runs here.

  python3 factory.py            run the daemon
  python3 factory.py --once     run one tick and exit
  FACTORY_DRY_RUN=1             read everything, write nothing
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads(Path(os.environ.get("FACTORY_CONFIG", HERE / "config.json")).read_text())
PROMPTS = HERE / "prompts"
STATE_PATH = Path(os.environ.get("FACTORY_STATE", "/state/state.json"))
CANVAS_URL = os.environ.get("CANVAS_URL", "http://canvas:8000").rstrip("/")
CANVAS_KEY_FILE = Path(os.environ.get("CANVAS_KEY_FILE", "/canvas-state/agent-canvas/api-key.txt"))
LINEAR_KEY = os.environ.get("LINEAR_API_KEY", "")
SLACK_BOT = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP = os.environ.get("SLACK_APP_TOKEN", "")
SLACK_PERSON = os.environ.get("SLACK_PERSON_ID", "")
DRY_RUN = os.environ.get("FACTORY_DRY_RUN") == "1"

REPO = CONFIG["repo_path"]
WORKTREES = Path(CONFIG["worktrees_path"])
MAX_ROUNDS = CONFIG["max_fix_rounds"]
STATIONS = ("plan", "build", "verify", "judge", "fix")
DONE_STAGES = ("review", "dropped")
FACTORY_MARK = "[factory]"

LOCK = threading.RLock()
WAKE = threading.Event()
log = logging.getLogger("factory")


class Stop(Exception):
    """A gate failed in a way only the person can resolve."""


# ── plumbing ──────────────────────────────────────────────────────────────────

def now():
    return datetime.now(timezone.utc).isoformat()


def http(method, url, headers=None, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {url} -> {e.code}: {e.read()[:500]!r}") from None
    return json.loads(raw) if raw else {}


def run(args, cwd=REPO, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(args)} -> {p.returncode}: {(p.stderr or p.stdout).strip()[:500]}")
    return p


def git(*args, cwd=REPO):
    return run(["git", *args], cwd=cwd).stdout.strip()


def gh_json(*args, cwd=REPO):
    out = run(["gh", *args], cwd=cwd).stdout.strip()
    return json.loads(out) if out else None


def mutate(what):
    if DRY_RUN:
        log.info("dry-run: would %s", what)
        return False
    return True


def render(name, **values):
    text = (PROMPTS / name).read_text()
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def result_block(extra_status="", extra_fields=""):
    return render("_result.md", extra_status=extra_status, extra_fields=extra_fields)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"issues": {}}


def save_state(state):
    if DRY_RUN:
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


# ── Linear ────────────────────────────────────────────────────────────────────

def linear(query, **variables):
    out = http("POST", "https://api.linear.app/graphql", {"Authorization": LINEAR_KEY},
               {"query": query, "variables": variables})
    if out.get("errors"):
        raise RuntimeError(f"Linear: {out['errors']}")
    return out["data"]


_TEAM = {}


def team():
    if not _TEAM:
        data = linear("""query($key: String!) { viewer { id }
          teams(filter: { key: { eq: $key } }) { nodes { id
            states { nodes { id name } } labels { nodes { id name } } } } }""", key=CONFIG["team_key"])
        node = data["teams"]["nodes"][0]
        _TEAM.update(viewer=data["viewer"]["id"], id=node["id"],
                     states={s["name"]: s["id"] for s in node["states"]["nodes"]},
                     labels={l["name"]: l["id"] for l in node["labels"]["nodes"]})
    return _TEAM


def ready_issues():
    data = linear("""query($key: String!, $state: String!, $label: String!) {
      issues(first: 50, filter: { team: { key: { eq: $key } }, state: { name: { eq: $state } },
                                  labels: { some: { name: { eq: $label } } } }) {
        nodes { """ + ISSUE_FIELDS + """ createdAt } } }""",
                  key=CONFIG["team_key"], state=CONFIG["states"]["ready"], label=CONFIG["label"])
    return sorted(data["issues"]["nodes"], key=lambda i: i["createdAt"])


ISSUE_FIELDS = """id identifier title description url state { name type }
      project { id name } labels { nodes { name } }
      inverseRelations(first: 50) { nodes { type issue { identifier state { name type } } } }"""


def issue_detail(issue_id):
    return linear("""query($id: String!) { issue(id: $id) { """ + ISSUE_FIELDS + """
      comments(first: 100) { nodes { id body createdAt user { id name } } } } }""", id=issue_id)["issue"]


FIELD_LINES = {
    "base": re.compile(r"^\s*Base:\s*`([^`\s]+)`", re.M),
    "branch": re.compile(r"^\s*Branch:\s*`([^`\s]+)`", re.M),
    "task": re.compile(r"^\s*Task:\s*`(\.specs/features/[a-z0-9-]+/tasks/[^`\s]+\.md)`", re.M),
}
WAIT_PRS = re.compile(r"Start only after pull requests?\s+((?:#\d+(?:\s*,\s*|\s+and\s+)?)+)", re.I)


def issue_fields(description):
    """The Base, Branch, Task and "Start only after pull request #N" lines an issue may carry."""
    text = description or ""
    fields = {key: (m.group(1) if (m := rx.search(text)) else None) for key, rx in FIELD_LINES.items()}
    feature = re.search(r"\.specs/features/([a-z0-9-]+)/", fields["task"] or text)
    fields["feature"] = feature.group(1) if feature else None
    waits = WAIT_PRS.search(text)
    fields["wait_prs"] = [int(n) for n in re.findall(r"#(\d+)", waits.group(1))] if waits else []
    return fields


def open_blockers(issue):
    """What still holds the issue: Linear blockers not Done, and pull requests it waits on that are not merged."""
    reasons = [f"{r['issue']['identifier']} ({r['issue']['state']['name']})"
               for r in issue["inverseRelations"]["nodes"]
               if r["type"] == "blocks" and r["issue"]["state"]["type"] != "completed"]
    for number in issue_fields(issue.get("description"))["wait_prs"]:
        pr = gh_json("pr", "view", str(number), "--repo", CONFIG["github_repo"], "--json", "state")
        if pr["state"] != "MERGED":
            reasons.append(f"PR #{number} ({pr['state'].lower()})")
    return reasons


def set_status(rec, key):
    if mutate(f"move {rec['identifier']} to {CONFIG['states'][key]}"):
        linear("mutation($id: String!, $s: String!) { issueUpdate(id: $id, input: { stateId: $s }) { success } }",
               id=rec["issue_id"], s=team()["states"][CONFIG["states"][key]])


def set_needs_human(rec, on):
    field = "addedLabelIds" if on else "removedLabelIds"
    if mutate(f"{'add' if on else 'remove'} {CONFIG['needs_human_label']} on {rec['identifier']}"):
        linear(f"mutation($id: String!, $l: [String!]) {{ issueUpdate(id: $id, input: {{ {field}: $l }}) {{ success }} }}",
               id=rec["issue_id"], l=[team()["labels"][CONFIG["needs_human_label"]]])


def comment(rec, body):
    if mutate(f"comment on {rec['identifier']}: {body[:80]}"):
        linear("mutation($id: String!, $b: String!) { commentCreate(input: { issueId: $id, body: $b }) { success } }",
               id=rec["issue_id"], b=f"{FACTORY_MARK} {body}")


def issue_block(issue):
    parts = [f"**{issue['identifier']}: {issue['title']}**", issue["url"], "", issue.get("description") or "(no description)"]
    notes = [c for c in issue.get("comments", {}).get("nodes", [])]
    if notes:
        parts += ["", "### Comments"]
        for c in sorted(notes, key=lambda c: c["createdAt"]):
            parts.append(f"- {c['user']['name'] if c.get('user') else 'unknown'} ({c['createdAt'][:10]}): {c['body']}")
    return "\n".join(parts)


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack(method, **params):
    out = http("POST", f"https://slack.com/api/{method}",
               {"Authorization": f"Bearer {SLACK_BOT}", "Content-Type": "application/json; charset=utf-8"}, params)
    if not out.get("ok"):
        raise RuntimeError(f"Slack {method}: {out.get('error')}")
    return out


def slugify(name):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def channel_for(rec):
    cfg = CONFIG["slack"]
    project = rec.get("project")
    if project and project in cfg["project_channels"]:
        return cfg["project_channels"][project]
    name = (cfg["channel_prefix"] + slugify(project))[:80] if project else cfg["default_channel"]
    try:
        return invite_person(slack("conversations.create", name=name)["channel"]["id"])
    except RuntimeError as e:
        if "name_taken" not in str(e):
            raise
    cursor = ""
    while True:
        page = slack("conversations.list", types="public_channel", exclude_archived=True, limit=1000, cursor=cursor)
        for ch in page["channels"]:
            if ch["name"] == name:
                if not ch.get("is_member"):
                    slack("conversations.join", channel=ch["id"])
                return invite_person(ch["id"])
        cursor = page.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            raise RuntimeError(f"Slack channel #{name} exists but was not found")


def invite_person(channel):
    """A mention only notifies channel members, so the person joins every factory channel."""
    if SLACK_PERSON:
        try:
            slack("conversations.invite", channel=channel, users=SLACK_PERSON)
        except RuntimeError as e:
            if "already_in_channel" not in str(e):
                raise
    return channel


def root_text(rec):
    pr = f"<{rec['pr_url']}|PR #{rec['pr_number']}>" if rec.get("pr_url") else "no PR yet"
    stage = rec["stage"] if rec["stage"] != "waiting" else f"waiting for you ({rec.get('waiting_stage')})"
    return (f"*<{rec['issue_url']}|{rec['identifier']}>* · {rec['title']}\n"
            f"Station: *{stage}* · {pr} · round {rec.get('round', 0)}/{MAX_ROUNDS}")


def notify(rec, text, human=False):
    """Post a station update in the issue thread. A stop for the person mentions them and reaches the channel."""
    log.info("%s: %s", rec["identifier"], text)
    if not SLACK_BOT or not mutate(f"Slack: {text[:80]}"):
        return
    try:
        if not rec.get("slack_ts"):
            rec["slack_channel"] = channel_for(rec)
            rec["slack_ts"] = slack("chat.postMessage", channel=rec["slack_channel"], text=root_text(rec))["ts"]
        if human and SLACK_PERSON:
            text = f"<@{SLACK_PERSON}> {text}"
        slack("chat.postMessage", channel=rec["slack_channel"], thread_ts=rec["slack_ts"],
              text=text, reply_broadcast=human)
        slack("chat.update", channel=rec["slack_channel"], ts=rec["slack_ts"], text=root_text(rec))
    except Exception:
        log.error("Slack failed for %s:\n%s", rec["identifier"], traceback.format_exc())


def start_socket_mode(state):
    if not (SLACK_BOT and SLACK_APP):
        log.warning("Slack tokens missing: no Slack updates or replies")
        return
    if not SLACK_PERSON:
        log.warning("SLACK_PERSON_ID missing: Slack replies are ignored")
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse

    client = SocketModeClient(app_token=SLACK_APP, web_client=WebClient(token=SLACK_BOT))
    mention = f"<@{slack('auth.test')['user_id']}>"

    def on_request(c, req):
        c.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        ev = req.payload.get("event", {}) if req.type == "events_api" else {}
        log.debug("Slack envelope: %s %s %s", req.type, ev.get("type"), ev.get("channel"))
        if req.type != "events_api":
            return
        if ev.get("type") != "message" or ev.get("subtype") or ev.get("bot_id"):
            return
        if not SLACK_PERSON or ev.get("user") != SLACK_PERSON:
            return
        text, channel, thread = ev.get("text", ""), ev.get("channel"), ev.get("thread_ts")
        with LOCK:
            for rec in state["issues"].values():
                if thread and rec.get("slack_channel") == channel and rec.get("slack_ts") == thread:
                    rec.setdefault("inbox", []).append({"source": "Slack", "text": text})
                    save_state(state)
                    WAKE.set()
                    return
            if mention in text and channel in CONFIG["mentions"]["channels"]:
                request = text.replace(mention, "").strip(" :")
                state.setdefault("mention_inbox", []).append(
                    {"channel": channel, "ts": ev["ts"], "thread": thread or ev["ts"], "reply": bool(thread),
                     "text": text, "request": request})
                save_state(state)
                WAKE.set()

    client.socket_mode_request_listeners.append(on_request)
    client.connect()
    log.info("Slack Socket Mode connected")


# ── OpenHands conversations ───────────────────────────────────────────────────

def canvas(method, path, body=None):
    return http(method, CANVAS_URL + path, {"X-Session-API-Key": CANVAS_KEY_FILE.read_text().strip()}, body)


def event_count(conv_id):
    return canvas("GET", f"/api/conversations/{conv_id}/events/count")


def start_station(rec, station, prompt):
    rec["stage"] = station
    if not mutate(f"start {station} conversation for {rec['identifier']}"):
        return
    out = canvas("POST", "/api/conversations", {
        "agent_profile_id": CONFIG["agent_profile_id"],
        "workspace": {"kind": "LocalWorkspace", "working_dir": rec["worktree"]},
        "worktree": False,
        "autotitle": False,
        "tags": {"factory": rec["identifier"].lower(), "station": station},
        "initial_message": {"role": "user", "content": [{"type": "text", "text": prompt}], "run": True},
    })
    rec["conversation_id"] = out["id"]
    rec["events_mark"] = 0
    rec["station_started_at"] = now()
    rec.setdefault("conversations", []).append({"station": station, "id": out["id"], "at": now()})
    notify(rec, f"{station.capitalize()} started. <{CONFIG['canvas_public_url']}/conversations/{out['id']}"
                f"|Open the conversation>")


def send_to_conversation(rec, text):
    if not mutate(f"send message to {rec['conversation_id']}"):
        return
    rec["events_mark"] = event_count(rec["conversation_id"])
    canvas("POST", f"/api/conversations/{rec['conversation_id']}/events",
           {"role": "user", "content": [{"type": "text", "text": text}], "run": True})


def station_result(rec):
    """None while the station runs; the parsed FACTORY_RESULT once it finished."""
    conv = canvas("GET", f"/api/conversations/{rec['conversation_id']}")
    status = conv.get("execution_status")
    if status in ("error", "stuck"):
        raise Stop(f"The {rec['stage']} conversation ended with status `{status}`.")
    if status == "waiting_for_confirmation":
        raise Stop(f"The {rec['stage']} conversation is waiting for a confirmation in the Canvas.")
    if status != "finished" or event_count(rec["conversation_id"]) <= rec.get("events_mark", 0):
        return None
    text = canvas("GET", f"/api/conversations/{rec['conversation_id']}/agent_final_response").get("response", "")
    match = None
    for match in re.finditer(r"FACTORY_RESULT:\s*(\{.*\})", text):
        pass
    if not match:
        raise Stop(f"The {rec['stage']} station finished without a FACTORY_RESULT line.")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        raise Stop(f"The {rec['stage']} station returned an unreadable FACTORY_RESULT: `{match.group(1)[:200]}`")


# ── git and GitHub ────────────────────────────────────────────────────────────

def ensure_worktree(rec):
    path = Path(rec["worktree"])
    if path.exists() or not mutate(f"create worktree {path} on {rec['branch']}"):
        return
    git("fetch", "origin")
    WORKTREES.mkdir(parents=True, exist_ok=True)
    branch, base = rec["branch"], rec["base"]
    if not ref_exists(f"origin/{base}"):
        raise Stop(f"The base `{base}` does not exist on origin.")
    if ref_exists(f"refs/heads/{branch}"):
        git("worktree", "add", str(path), branch)
    elif ref_exists(f"origin/{branch}"):
        git("worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}")
    else:
        git("worktree", "add", "--no-track", "-b", branch, str(path), f"origin/{base}")


def ref_exists(ref):
    return run(["git", "rev-parse", "--verify", "--quiet", ref], check=False).returncode == 0


def require_clean_and_pushed(rec):
    wt = rec["worktree"]
    if git("status", "--porcelain", cwd=wt):
        raise Stop("The working tree has uncommitted changes after the station finished.")
    git("fetch", "origin", rec["branch"], cwd=wt)
    head = git("rev-parse", "HEAD", cwd=wt)
    if head != git("rev-parse", f"origin/{rec['branch']}", cwd=wt):
        raise Stop(f"`{rec['branch']}` is not pushed: local head differs from origin.")
    return head


class Conflict(Exception):
    """Merging the base into the issue branch conflicts; the files are in args[0]."""


def base_is_merged(rec):
    git("fetch", "origin", rec["base"], cwd=rec["worktree"])
    return run(["git", "merge-base", "--is-ancestor", f"origin/{rec['base']}", "HEAD"],
               cwd=rec["worktree"], check=False).returncode == 0


def sync_with_base(rec):
    """Merge the latest base into the issue branch and push it. Merge, never rebase, so no push is forced."""
    wt, base, branch = rec["worktree"], rec["base"], rec["branch"]
    if base_is_merged(rec) or not mutate(f"merge origin/{base} into {branch}"):
        return git("rev-parse", "HEAD", cwd=wt)
    if run(["git", "merge", "--no-edit", f"origin/{base}"], cwd=wt, check=False).returncode:
        files = git("diff", "--name-only", "--diff-filter=U", cwd=wt).splitlines()
        run(["git", "merge", "--abort"], cwd=wt, check=False)
        raise Conflict(files or ["(git reported a failed merge)"])
    git("push", "origin", f"HEAD:{branch}", cwd=wt)
    notify(rec, f"Merged the latest `{base}` into `{branch}`.")
    return git("rev-parse", "HEAD", cwd=wt)


def require_tracked(rec, rel):
    if not rel or not git("ls-files", rel, cwd=rec["worktree"]):
        raise Stop(f"Expected a committed `{rel}`, and it is not in the branch.")


def blocking_unresolved(path):
    text = Path(path).read_text()
    section = re.search(r"^## Unresolved\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    rows = []
    for line in (section.group(1) if section else "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].isdigit():
            rows.append((cells[1], cells[2]))
    return [q for kind, q in rows if kind == "blocks"], [q for kind, q in rows if kind == "blocks go-live"]


def pr_checks(rec, head):
    """CI on a commit from Actions runs and commit statuses: a fine-grained token cannot read the Checks API."""
    repo = CONFIG["github_repo"]
    out = {"pass": [], "fail": [], "pending": []}
    runs = gh_json("api", f"repos/{repo}/actions/runs?head_sha={head}&per_page=100", cwd=rec["worktree"])
    latest = {}
    for r in runs["workflow_runs"]:
        if r["workflow_id"] not in latest or r["id"] > latest[r["workflow_id"]]["id"]:
            latest[r["workflow_id"]] = r
    for r in latest.values():
        if r["status"] != "completed":
            out["pending"].append(r["name"])
        elif r["conclusion"] in ("success", "skipped", "neutral"):
            out["pass"].append(r["name"])
        else:
            out["fail"].append(f"{r['name']} ({r['conclusion']})")
    for s in gh_json("api", f"repos/{repo}/commits/{head}/status", cwd=rec["worktree"])["statuses"]:
        bucket = {"success": "pass", "pending": "pending"}.get(s["state"], "fail")
        out[bucket].append(s["context"])
    return out


def judge_verdict(rec, head):
    reviews = gh_json("api", f"repos/{CONFIG['github_repo']}/pulls/{rec['pr_number']}/reviews",
                      cwd=rec["worktree"]) or []
    started = rec.get("station_started_at", "")
    for review in reversed(reviews):
        if review.get("commit_id") != head or (review.get("submitted_at") or "") < started[:19]:
            continue
        if review["state"] == "APPROVED":
            return "APPROVE", review["html_url"]
        if review["state"] == "CHANGES_REQUESTED":
            return "REQUEST_CHANGES", review["html_url"]
        intended = re.search(r"Intended verdict: \*\*(APPROVE|REQUEST_CHANGES)\*\*", review.get("body") or "")
        return (intended.group(1) if intended else "COMMENT"), review["html_url"]
    raise Stop("The judge finished, but there is no review on the head commit.")


# ── stations ──────────────────────────────────────────────────────────────────

CI_NOTE = ("The GitHub token here cannot read the Checks API, so `gh pr checks` fails. Read CI with "
           "`gh run list --commit <sha>` and `gh run view <id> --log-failed`, and commit statuses with "
           f"`gh api repos/{CONFIG['github_repo']}/commits/<sha>/status`. The token cannot re-run jobs: if a "
           "failure is unrelated to this change, stop with `needs_human` and name the flaky test.")


def paths(rec):
    feature = f".specs/features/{rec['feature']}"
    checklist = rec.get("checklist") or ""
    return dict(ci_note=CI_NOTE, identifier=rec["identifier"], issue_url=rec["issue_url"], worktree=rec["worktree"],
                branch=rec["branch"], base=rec["base"], feature=rec["feature"], tasks_file=rec.get("tasks_file", ""),
                checklist=checklist, verified=checklist[:-3] + ".verified.md" if checklist else "",
                pr_url=rec.get("pr_url", ""), pr_number=rec.get("pr_number", ""),
                round=rec.get("round", 0), max_rounds=MAX_ROUNDS, feature_dir=feature)


def start_plan(rec):
    issue = issue_detail(rec["issue_id"])
    start_station(rec, "plan", render("plan.md", issue=issue_block(issue), result=result_block(
        extra_fields=', "tasks_file": "<path of the tasks file>"'), **paths(rec)))


def start_build(rec, handoff=False):
    issue = issue_detail(rec["issue_id"])
    note = ("A previous Build conversation handed off. Continue from the handoff recorded in the checklist "
            f"`{rec.get('checklist')}`. Do not redo landed slices.") if handoff else ""
    start_station(rec, "build", render("build.md", issue=issue_block(issue), handoff=note, result=result_block(
        extra_status=" | handoff", extra_fields=', "checklist": "<path of the checklist>"'), **paths(rec)))


def start_verify(rec, head):
    start_station(rec, "verify", render("verify.md", head=head, result=result_block(), **paths(rec)))


def start_judge(rec, head):
    start_station(rec, "judge", render("judge.md", head=head, result=result_block(), **paths(rec)))


def start_fix(rec, reason, head):
    rec["round"] = rec.get("round", 0) + 1
    if rec["round"] > MAX_ROUNDS:
        rec["round"] = MAX_ROUNDS
        rec["rounds_exhausted"] = True
        raise Stop(f"No `APPROVE` after {MAX_ROUNDS} fix rounds. Last failure: {reason}")
    rec["fix_from"] = head
    notify(rec, f"Fix round {rec['round']}/{MAX_ROUNDS}: {reason}")
    start_station(rec, "fix", render("fix.md", head=head, reason=reason, result=result_block(), **paths(rec)))


def after_plan(rec, result):
    tasks = result.get("tasks_file", "")
    require_tracked(rec, tasks)
    if not tasks.startswith(f".specs/features/{rec['feature']}/tasks/"):
        raise Stop(f"The tasks file `{tasks}` is outside `.specs/features/{rec['feature']}/tasks/`.")
    blocks, go_live = blocking_unresolved(Path(rec["worktree"]) / tasks)
    if blocks:
        raise Stop("The plan has blocking Unresolved questions:\n" + "\n".join(f"- {q}" for q in blocks))
    rec["tasks_file"] = tasks
    notify(rec, f"Plan done: `{tasks}`." + (f" Blocks go-live: {'; '.join(go_live)}" if go_live else ""))
    start_build(rec)


def after_build(rec, result):
    if result.get("status") == "handoff":
        rec["checklist"] = result.get("checklist") or rec.get("checklist")
        notify(rec, "Build handed off to a fresh conversation.")
        return start_build(rec, handoff=True)
    checklist = result.get("checklist", "")
    require_tracked(rec, checklist)
    if not checklist.startswith(f".specs/features/{rec['feature']}/checks/") or checklist.endswith(".verified.md"):
        raise Stop(f"The checklist `{checklist}` is outside `.specs/features/{rec['feature']}/checks/`.")
    rec["checklist"] = checklist
    head = require_clean_and_pushed(rec)
    prs = gh_json("pr", "list", "--head", rec["branch"], "--base", rec["base"], "--state", "open",
                  "--json", "number,url", cwd=rec["worktree"]) or []
    if not prs:
        raise Stop(f"No open pull request from `{rec['branch']}` into `{rec['base']}`.")
    rec["pr_number"], rec["pr_url"] = prs[0]["number"], prs[0]["url"]
    notify(rec, f"Build done: checklist `{checklist}`, draft <{rec['pr_url']}|PR #{rec['pr_number']}>.")
    comment(rec, f"Draft pull request: {rec['pr_url']}")
    changed = git("diff", "--name-only", f"origin/{rec['base']}...HEAD", cwd=rec["worktree"]).splitlines()
    schema = [f for f in changed if any(f.startswith(p) for p in CONFIG["schema_paths"])]
    if schema and not rec.get("schema_acknowledged"):
        rec["schema_acknowledged"] = True
        raise Stop("This change touches schema or RLS (" + ", ".join(schema[:5]) + "), and `pnpm run test:rls` "
                   "cannot run in the sandbox. Run it locally and reply with the result.")
    sync_then_ci(rec, head)


def sync_then_ci(rec, head):
    """Bring the latest base in before CI and Verify, so what gets verified and judged is current."""
    try:
        head = sync_with_base(rec)
    except Conflict as conflict:
        return start_fix(rec, f"Merging `origin/{rec['base']}` into `{rec['branch']}` conflicts in "
                              f"{', '.join(conflict.args[0])}. Merge `origin/{rec['base']}`, resolve the conflicts "
                              "keeping the intent of both sides, run the affected proofs, commit and push.", head)
    wait_ci(rec, head, then="verify")


def check_final(rec):
    """Before In review: the branch holds the current base and GitHub can merge the pull request."""
    head = require_clean_and_pushed(rec)
    if not base_is_merged(rec):
        rec["base_syncs"] = rec.get("base_syncs", 0) + 1
        if rec["base_syncs"] > CONFIG["max_base_syncs"]:
            raise Stop(f"`{rec['base']}` kept moving after review {CONFIG['max_base_syncs']} times. "
                       "Reply to merge it once more and run Verify and Judge again.")
        notify(rec, f"`{rec['base']}` moved after the review. Merging it, then Verify and Judge run again.")
        return sync_then_ci(rec, head)
    pr = gh_json("pr", "view", str(rec["pr_number"]), "--json", "mergeable,headRefOid", cwd=rec["worktree"])
    if pr["headRefOid"] != head or pr["mergeable"] == "UNKNOWN":
        return
    if pr["mergeable"] == "CONFLICTING":
        return start_fix(rec, f"GitHub reports that the pull request conflicts with `{rec['base']}`. "
                              f"Merge `origin/{rec['base']}`, resolve the conflicts, commit and push.", head)
    if mutate(f"mark PR #{rec['pr_number']} ready"):
        run(["gh", "pr", "ready", str(rec["pr_number"])], cwd=rec["worktree"])
    set_status(rec, "review")
    rec["stage"] = "review"
    comment(rec, f"the-judge approved the head commit ({rec['last_review']}). "
                 f"The pull request is ready for review: {rec['pr_url']}")
    notify(rec, f"the-judge: APPROVE. <{rec['pr_url']}|PR #{rec['pr_number']}> is ready for your review.", human=True)


def wait_ci(rec, head, then):
    rec.update(stage="ci", ci_head=head, ci_then=then, ci_since=now())


def check_ci(rec):
    head = require_clean_and_pushed(rec)
    if head != rec["ci_head"]:
        rec["ci_head"], rec["ci_since"] = head, now()
    checks = pr_checks(rec, head)
    if checks["fail"]:
        return start_fix(rec, "CI failing on the head commit: " + ", ".join(checks["fail"]), head)
    if checks["pending"]:
        return
    if not checks["pass"]:
        waited = time.time() - datetime.fromisoformat(rec["ci_since"]).timestamp()
        if waited < CONFIG["no_checks_timeout_seconds"]:
            return
        raise Stop("No CI checks reported on the head commit.")
    if rec["ci_then"] == "verify":
        start_verify(rec, head)


def after_verify(rec, result):
    head = require_clean_and_pushed(rec)
    verified = paths(rec)["verified"]
    require_tracked(rec, verified)
    if git("log", "-1", "--format=%H", "--", verified, cwd=rec["worktree"]) != head:
        raise Stop(f"`{verified}` is not the last commit, so it does not verify the head.")
    report = (Path(rec["worktree"]) / verified).read_text()
    if re.search(r"\*\*Verdict\*\*:\s*PASS", report):
        notify(rec, f"Verify: PASS (`{verified}`).")
        return start_judge(rec, head)
    notify(rec, f"Verify: FAIL (`{verified}`).")
    start_fix(rec, f"The Verifier returned FAIL. Read `{verified}` for the failing checks.", head)


def after_judge(rec, result):
    head = require_clean_and_pushed(rec)
    verdict, url = judge_verdict(rec, head)
    rec["last_review"] = url
    if verdict != "APPROVE":
        notify(rec, f"the-judge: {verdict} (<{url}|review>).")
        return start_fix(rec, f"the-judge returned {verdict}. Address the review at {url}.", head)
    rec["stage"] = "final"
    check_final(rec)


def after_fix(rec, result):
    head = require_clean_and_pushed(rec)
    if head == rec.get("fix_from"):
        raise Stop("The fix station finished without a new commit.")
    notify(rec, f"Fix round {rec['round']} pushed `{head[:8]}`.")
    sync_then_ci(rec, head)


AFTER = {"plan": after_plan, "build": after_build, "verify": after_verify, "judge": after_judge, "fix": after_fix}


# ── Slack mentions: ad-hoc requests outside the issue flow ───────────────────

MENTION_RULES = (
    "Regras fixas deste canal do Slack:\n"
    "- Sua resposta final será publicada somente na thread da mensagem que disparou o pedido. "
    "Não publique mensagens no Slack e não use ferramentas para enviar mensagens em nenhum canal, "
    "DM ou thread.\n"
    "- Se pedirem o status de um projeto, consulte o Linear apenas em leitura. Não crie, edite, "
    "comente nem arquive nada no Linear. Responda com um resumo curto: nome, estado, prazo se houver "
    "e próximo passo.\n"
)


def thread_history(channel, thread):
    msgs = slack("conversations.replies", channel=channel, ts=thread, limit=50).get("messages", [])
    return [f"[{m.get('user', '?')}]: {m.get('text', '')}" for m in msgs if not m.get("bot_id")]


def start_mention(state, m):
    """Open a conversation for a mention, like the Slack Channel Monitor automation did."""
    if not mutate(f"open a conversation for the mention at {m['channel']}/{m['ts']}"):
        return
    try:
        slack("reactions.add", channel=m["channel"], timestamp=m["ts"], name="eyes")
    except RuntimeError as e:
        if "already_reacted" not in str(e):
            log.warning("reaction failed: %s", e)
    history = "\n".join(thread_history(m["channel"], m["thread"])) if m["reply"] else ""
    prompt = (
        "You are an AI assistant responding to a Slack message.\n\n"
        f"Channel ID  : {m['channel']}\nThread root : {m['thread']}\nFull message: {m['text']}\n"
        f"User request: {m['request'] or '(no explicit request, use your best judgement)'}\n\n"
        + (f"--- Thread history (oldest to newest) ---\n{history}\n--- End of thread history ---\n\n" if history else "")
        + "Respond to the User request above. Earlier thread messages are context only and are not "
          "directed at you unless the request refers to them.\n\n"
          "When you are finished, summarise what you did clearly. That summary is posted to the Slack thread, "
          "so keep links clickable: never wrap a URL in asterisks or backticks.\n\n" + MENTION_RULES)
    workspace = CONFIG["mentions"]["workspace"]
    Path(workspace).mkdir(parents=True, exist_ok=True)
    out = canvas("POST", "/api/conversations", {
        "agent_profile_id": CONFIG["agent_profile_id"],
        "workspace": {"kind": "LocalWorkspace", "working_dir": workspace},
        "worktree": False,
        "tags": {"factory": "slack-mention"},
        "agent_launch_additions": {"system_message_suffix_append": MENTION_RULES},
        "initial_message": {"role": "user", "content": [{"type": "text", "text": prompt}], "run": True},
    })
    state.setdefault("mentions", {})[f"{m['channel']}:{m['thread']}"] = {
        "conversation_id": out["id"], "channel": m["channel"], "thread": m["thread"],
        "status": "active", "events_mark": 0, "since": time.time()}
    slack("chat.postMessage", channel=m["channel"], thread_ts=m["thread"],
          text=f"On it. <{CONFIG['canvas_public_url']}/conversations/{out['id']}|View progress>")


def follow_up_mention(rec, m):
    if not mutate(f"forward the mention at {m['channel']}/{m['ts']}"):
        return
    try:
        slack("reactions.add", channel=m["channel"], timestamp=m["ts"], name="eyes")
    except RuntimeError:
        pass
    rec["events_mark"] = event_count(rec["conversation_id"])
    canvas("POST", f"/api/conversations/{rec['conversation_id']}/events",
           {"role": "user", "content": [{"type": "text", "text": m["request"] or m["text"]}], "run": True})
    rec.update(status="active", since=time.time())


def finish_mention(rec):
    """Post the final answer once the conversation stops working."""
    if time.time() - rec["since"] < CONFIG["mentions"]["debounce_seconds"]:
        return
    conv = canvas("GET", f"/api/conversations/{rec['conversation_id']}")
    status = conv.get("execution_status")
    # `idle` also shows before the agent starts, so only a finished run counts as an answer.
    if status not in ("finished", "error", "stuck"):
        return
    if status == "finished" and event_count(rec["conversation_id"]) <= rec["events_mark"]:
        return
    final = canvas("GET", f"/api/conversations/{rec['conversation_id']}/agent_final_response").get("response", "")
    if status in ("error", "stuck"):
        final = f"The agent stopped with status *{status}*." + (f"\n\n{final}" if final else "")
    if mutate(f"post the mention answer in {rec['channel']}/{rec['thread']}"):
        slack("chat.postMessage", channel=rec["channel"], thread_ts=rec["thread"],
              text=(final or "Done, with no message.")[:39000])
    rec["status"] = "answered"


def handle_mentions(state):
    with LOCK:
        for m in state.pop("mention_inbox", []):
            rec = state.get("mentions", {}).get(f"{m['channel']}:{m['thread']}")
            try:
                follow_up_mention(rec, m) if rec else start_mention(state, m)
            except Exception:
                log.error("mention at %s/%s failed:\n%s", m["channel"], m["ts"], traceback.format_exc())
        for rec in state.get("mentions", {}).values():
            if rec["status"] == "active":
                try:
                    finish_mention(rec)
                except Exception:
                    log.error("mention answer for %s failed:\n%s", rec["conversation_id"], traceback.format_exc())
        save_state(state)


def mentions_active(state):
    return any(r["status"] == "active" for r in state.get("mentions", {}).values())


# ── the loop ──────────────────────────────────────────────────────────────────

def stop_for_human(rec, question):
    rec["waiting_stage"] = rec["stage"] if rec["stage"] != "waiting" else rec.get("waiting_stage")
    rec["stage"] = "waiting"
    rec["waiting_since"] = now()
    notify(rec, f"Needs you at *{rec['waiting_stage']}*: {question}\nReply in this thread or on the Linear issue.",
           human=True)
    set_needs_human(rec, True)
    comment(rec, f"Stopped at {rec['waiting_stage']}. {question}\n\nReply in the Slack thread or here.")


def linear_replies(rec):
    since = rec.get("waiting_since") or rec.get("station_started_at") or rec["picked_at"]
    seen = set(rec.setdefault("seen_comments", []))
    out = []
    for c in issue_detail(rec["issue_id"])["comments"]["nodes"]:
        if c["id"] in seen or c["createdAt"] < since or (c.get("user") or {}).get("id") != team()["viewer"]:
            continue
        rec["seen_comments"].append(c["id"])
        if not c["body"].startswith(FACTORY_MARK):
            out.append({"source": "Linear", "text": c["body"]})
    return out


def deliver_replies(rec):
    replies = rec.pop("inbox", []) + linear_replies(rec)
    if not replies:
        return
    if not rec.get("conversation_id"):
        # A stop before any conversation (a base or tasks file problem): the answer means "check again".
        if rec["stage"] == "waiting":
            rec.pop("waiting_stage", None)
            rec["stage"] = "blocked"
            set_needs_human(rec, False)
            notify(rec, "Got your answer. Checking the issue again.")
        return
    for reply in replies:
        send_to_conversation(rec, render("answer.md", source=reply["source"], answer=reply["text"],
                                         identifier=rec["identifier"]))
    if rec["stage"] == "waiting":
        # The answer went to the last conversation, so its station's gate runs again when it finishes.
        rec.pop("waiting_stage", None)
        rec["stage"] = rec["conversations"][-1]["station"]
        if rec.pop("rounds_exhausted", False):
            rec["round"] = MAX_ROUNDS - 1
        rec.pop("base_syncs", None)
        set_needs_human(rec, False)
    notify(rec, f"Got your answer. {rec['stage'].capitalize()} continues.")


def advance(rec):
    deliver_replies(rec)
    if rec["stage"] == "ci":
        return check_ci(rec)
    if rec["stage"] == "final":
        return check_final(rec)
    if rec["stage"] in STATIONS and rec.get("conversation_id"):
        result = station_result(rec)
        if result is None:
            return
        if result.get("status") == "needs_human":
            raise Stop(result.get("question") or "The station asked for help without a question.")
        AFTER[rec["stage"]](rec, result)


def new_record(issue):
    fields = issue_fields(issue.get("description"))
    rec = {"issue_id": issue["id"], "identifier": issue["identifier"], "title": issue["title"],
           "issue_url": issue["url"], "project": (issue.get("project") or {}).get("name"),
           "stage": "queued", "round": 0, "picked_at": now(), "base": fields["base"] or CONFIG["base_branch"],
           "task": fields["task"], "worktree": str(WORKTREES / issue["identifier"])}
    if fields["feature"]:
        rec.update(feature=fields["feature"],
                   branch=fields["branch"] or f"feat/{fields['feature']}-{issue['identifier'].lower()}")
    return rec


def refuse(rec, why):
    """An issue the factory cannot take; the person fixes the description and removes the label to retry."""
    notify(rec, f"Cannot start: {why} Fix the description and remove `{CONFIG['needs_human_label']}` to retry.",
           human=True)
    set_needs_human(rec, True)
    comment(rec, f"Cannot start: {why}")


def begin(rec, issue=None):
    """Start the issue's first station once nothing holds it. Returns True when it took a slot."""
    issue = issue or issue_detail(rec["issue_id"])
    labels = {l["name"] for l in issue["labels"]["nodes"]}
    if issue["state"]["name"] != CONFIG["states"]["ready"] or CONFIG["label"] not in labels:
        rec["stage"] = "dropped"
        notify(rec, f"Left the factory: the issue is no longer {CONFIG['states']['ready']} with `{CONFIG['label']}`.")
        return False
    reasons = open_blockers(issue)
    if reasons:
        rec["stage"] = "blocked"
        if rec.get("blocked_on") != reasons:
            rec["blocked_on"] = reasons
            notify(rec, "Waiting on " + ", ".join(reasons) + ". Starts when they are Done or merged.")
        return False
    if not mutate(f"start {rec['identifier']} on `{rec['branch']}` from `{rec['base']}`"):
        return False
    ensure_worktree(rec)
    set_status(rec, "progress")
    task = rec.get("task")
    if task and ref_exists(f"origin/{rec['base']}") and run(
            ["git", "cat-file", "-e", f"origin/{rec['base']}:{task}"], check=False).returncode == 0:
        blocks, _ = blocking_unresolved(Path(rec["worktree"]) / task)
        if blocks:
            rec["stage"] = "plan"
            raise Stop(f"`{task}` has blocking Unresolved questions:\n" + "\n".join(f"- {q}" for q in blocks))
        rec["tasks_file"] = task
        notify(rec, f"Plan skipped: `{task}` is already planned on `{rec['base']}`.")
        start_build(rec)
    else:
        start_plan(rec)
    return True


def pickup(state):
    """Register every new Ready for agent issue with `factory`; slots are filled separately."""
    for issue in ready_issues():
        labels = {l["name"] for l in issue["labels"]["nodes"]}
        if issue["identifier"] in state["issues"] or CONFIG["needs_human_label"] in labels:
            continue
        rec = new_record(issue)
        if not rec.get("feature"):
            refuse(rec, "the description must name the feature folder as `.specs/features/<feature>/`.")
            continue
        if rec["branch"] == rec["base"] or rec["branch"] in ("main", "master"):
            refuse(rec, f"`Branch:` `{rec['branch']}` is the base. Name a branch of its own.")
            continue
        if not mutate(f"pick up {issue['identifier']} ({rec['branch']} from {rec['base']})"):
            continue
        state["issues"][issue["identifier"]] = rec
        notify(rec, f"Picked up. Base `{rec['base']}`, branch `{rec['branch']}`, worktree `{rec['worktree']}`.")


def active_count(state):
    return sum(r["stage"] not in IDLE_STAGES for r in state["issues"].values())


def fill_slots(state):
    """Start held issues oldest first while slots are free; tell a queued issue its position once."""
    pending = sorted((r for r in state["issues"].values() if r["stage"] in ("queued", "blocked")),
                     key=lambda r: r["picked_at"])
    position = 0
    for rec in pending:
        if active_count(state) >= CONFIG["max_active"]:
            if rec["stage"] == "queued":
                position += 1
                if not rec.get("queue_noticed"):
                    rec["queue_noticed"] = True
                    notify(rec, f"Queued, position {position}. {CONFIG['max_active']} issues are running; "
                                "this one starts when a slot frees.")
            continue
        try:
            begin(rec)
        except Stop as stop:
            stop_for_human(rec, str(stop))
        except Exception:
            log.error("%s start failed:\n%s", rec["identifier"], traceback.format_exc())


IDLE_STAGES = DONE_STAGES + ("waiting", "blocked", "queued")


def tick(state):
    with LOCK:
        live = [r for r in state["issues"].values() if r["stage"] not in IDLE_STAGES]
        for rec in live:
            try:
                advance(rec)
            except Stop as stop:
                stop_for_human(rec, str(stop))
            except Exception:
                log.error("%s failed:\n%s", rec["identifier"], traceback.format_exc())
            save_state(state)
        for rec in (r for r in state["issues"].values() if r["stage"] == "waiting"):
            try:
                deliver_replies(rec)
            except Exception:
                log.error("%s reply failed:\n%s", rec["identifier"], traceback.format_exc())
        for rec in (r for r in state["issues"].values() if r["stage"] == "review" and r.get("review_checks", 0) < 3):
            try:
                keep_in_review(rec)
            except Exception:
                log.error("%s review status failed:\n%s", rec["identifier"], traceback.format_exc())
        try:
            pickup(state)
        except Exception:
            log.error("pickup failed:\n%s", traceback.format_exc())
        fill_slots(state)
        save_state(state)


def keep_in_review(rec):
    """Linear's GitHub automation can move the issue back when the pull request leaves draft; put it back."""
    rec["review_checks"] = rec.get("review_checks", 0) + 1
    issue = issue_detail(rec["issue_id"])
    if issue["state"]["name"] == CONFIG["states"]["progress"]:
        log.info("%s: Linear moved it back to %s; setting %s again", rec["identifier"],
                 issue["state"]["name"], CONFIG["states"]["review"])
        set_status(rec, "review")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if not LINEAR_KEY:
        sys.exit("LINEAR_API_KEY is not set")
    state = load_state()
    if "--once" in sys.argv:
        return tick(state)
    start_socket_mode(state)
    last_tick = 0.0
    while True:
        woken = WAKE.is_set()
        WAKE.clear()
        if woken or time.time() - last_tick >= CONFIG["poll_seconds"]:
            tick(state)
            last_tick = time.time()
        handle_mentions(state)
        # A running mention conversation is checked often so its answer reaches Slack quickly.
        WAKE.wait(CONFIG["mentions"]["poll_seconds"] if mentions_active(state)
                  else max(1.0, CONFIG["poll_seconds"] - (time.time() - last_tick)))


if __name__ == "__main__":
    main()
