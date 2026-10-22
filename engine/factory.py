#!/usr/bin/env python3
"""Agentic factory orchestrator.

Polls Linear and GitHub, runs one OpenHands (Cursor ACP) conversation per
station - plan, build, verify, judge, fix, demo - and checks each station's
artifacts before moving on. Talks to the person through one Slack thread per
issue (Socket Mode) and through Linear comments. No LLM runs here.

  python3 factory.py            run the daemon
  python3 factory.py --once     run one tick and exit
  FACTORY_DRY_RUN=1             read everything, write nothing

Record the demo of a pull request already in review:
  touch <state dir>/requests/demo-ENG-104

Run the station an issue stopped at again, in a new conversation:
  touch <state dir>/requests/retry-ENG-114
"""
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import dashboard
import settings

HERE = Path(__file__).resolve().parent
CONFIG = settings.load()
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
MAX_RETRIES = 2
RETRY_NOTE = ("A previous conversation for this station stopped with a connection error from Cursor (RetriableError) "
              "before it finished. Its work may already be in this working tree: check `git status` and `git log` "
              "first, keep what it did, and continue from there. The job is unchanged:\n\n---\n\n")
STATIONS = ("plan", "build", "verify", "judge", "fix", "demo")
DONE_STAGES = ("review", "merged", "dropped")
FACTORY_MARK = "[factory]"

EVIDENCE = Path(CONFIG["demo"].get("evidence_path", "/projects/factory-evidence"))
REQUESTS = STATE_PATH.parent / "requests"
MEDIA_TYPES = {".png": "image/png", ".gif": "image/gif", ".mp4": "video/mp4"}
_STATE = {"issues": {}}

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


def run(args, cwd=REPO, check=True, env=None, input=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, input=input,
                       env={**os.environ, **env} if env else None)
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


def max_rounds():
    return CONFIG["max_fix_rounds"]


def refresh_config():
    """Fetch the clone and read `.factory/` again at `config_ref`; a broken kit keeps the last good settings."""
    run(["git", "fetch", "origin"], check=False)
    try:
        fresh = settings.load()
    except (settings.ConfigError, ValueError, KeyError) as e:
        log.error("keeping the previous settings: %s", e)
        return
    CONFIG.clear()
    CONFIG.update(fresh)


def render(name, **values):
    text = settings.prompt(CONFIG, name)
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
}
WAIT_PRS = re.compile(r"Start only after pull requests?\s+((?:#\d+(?:\s*,\s*|\s+and\s+)?)+)", re.I)


def artifact(key, feature):
    """A path from the project's `artifacts` config, for one feature."""
    return CONFIG["artifacts"][key].replace("{feature}", feature)


def feature_dir(feature):
    """The folder the tasks and the checklists of a feature share, e.g. `.specs/features/<feature>`."""
    return os.path.commonpath([artifact("tasks", feature), artifact("checks", feature)])


def task_line():
    before, _, after = CONFIG["artifacts"]["tasks"].partition("{feature}")
    return re.compile(r"^\s*Task:\s*`(" + re.escape(before) + r"[a-z0-9-]+" + re.escape(after) + r"[^`\s]+\.md)`", re.M)


def tech_debt_source(text):
    """The single tech-debt record an issue cites, or None when it cites none, more than one, or the project
    has no tech-debt records."""
    debt = CONFIG["artifacts"].get("debt")
    if not debt:
        return None
    paths = list(dict.fromkeys(m.group(0) for m in re.finditer(debt["pattern"], text or "")))
    return paths[0] if len(paths) == 1 else None


def feature_from_debt(path):
    """The record's id, the pattern's first group, names the folder: `TD-WEB-01` becomes `td-web-01`."""
    return re.fullmatch(CONFIG["artifacts"]["debt"]["pattern"], path).group(1).lower()


def issue_fields(description):
    """The Base, Branch, Task and "Start only after pull request #N" lines an issue may carry.

    A description that never names a feature folder still gets one when it cites exactly one tech-debt
    record. The folder is the record's id in lowercase. Two records, or none, leave the feature empty so
    pickup refuses the issue.
    """
    text = description or ""
    fields = {key: (m.group(1) if (m := rx.search(text)) else None) for key, rx in FIELD_LINES.items()}
    fields["task"] = (m.group(1) if (m := task_line().search(text)) else None)
    feature = re.search(CONFIG["artifacts"]["feature"], fields["task"] or text)
    fields["feature"] = feature.group(1) if feature else None
    fields["debt_source"] = None
    if not fields["feature"]:
        source = tech_debt_source(text)
        if source:
            fields["feature"] = feature_from_debt(source)
            fields["debt_source"] = source
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
    """Comment on the issue as the factory; returns the comment's URL."""
    if mutate(f"comment on {rec['identifier']}: {body[:80]}"):
        out = linear("mutation($id: String!, $b: String!) { commentCreate(input: { issueId: $id, body: $b }) "
                     "{ success comment { url } } }", id=rec["issue_id"], b=f"{FACTORY_MARK} {body}")
        return (out["commentCreate"].get("comment") or {}).get("url", "")
    return ""


def linear_upload(path):
    """Upload a file to Linear's storage; returns its asset URL for use in markdown."""
    ctype = MEDIA_TYPES[path.suffix.lower()]
    up = linear("""mutation($c: String!, $f: String!, $s: Int!) { fileUpload(contentType: $c, filename: $f, size: $s) {
      success uploadFile { uploadUrl assetUrl headers { key value } } } }""",
                c=ctype, f=path.name, s=path.stat().st_size)["fileUpload"]
    if not up.get("success") or not up.get("uploadFile"):
        raise RuntimeError(f"Linear refused the upload of {path.name}")
    target = up["uploadFile"]
    headers = {"Content-Type": ctype, "Cache-Control": "public, max-age=31536000",
               **{h["key"]: h["value"] for h in target["headers"]}}
    req = urllib.request.Request(target["uploadUrl"], data=path.read_bytes(), method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Upload of {path.name} -> {e.code}: {e.read()[:300]!r}") from None
    return target["assetUrl"]


def is_status_note(body):
    """The factory's own status comments (Build done, demo, ready for review); its questions and the answers stay."""
    return body.startswith(FACTORY_MARK) and not body[len(FACTORY_MARK):].lstrip().startswith(("🙋", "📨", "📝"))


def issue_block(issue):
    parts = [f"**{issue['identifier']}: {issue['title']}**", issue["url"], "", issue.get("description") or "(no description)"]
    notes = [c for c in issue.get("comments", {}).get("nodes", []) if not is_status_note(c["body"])]
    if notes:
        parts += ["", "### Comments"]
        for c in sorted(notes, key=lambda c: c["createdAt"]):
            parts.append(f"- {c['user']['name'] if c.get('user') else 'unknown'} ({c['createdAt'][:10]}): {c['body']}")
    return "\n".join(parts)


# ── how the factory reads to people ───────────────────────────────────────────
# Slack speaks Portuguese to the person; Linear and GitHub stay in English (AGENTS.md). Stations keep their
# AGENTS.md names everywhere, each with one emoji.

LOOK = {
    "queued": ("⏳", "Queue"),
    "blocked": ("🚧", "Blocked"),
    "plan": ("🗺️", "Plan"),
    "build": ("🔨", "Build"),
    "fix": ("🔧", "Fix"),
    "ci": ("🚦", "CI"),
    "verify": ("🔍", "Verify"),
    "judge": ("⚖️", "Judge"),
    "final": ("🏁", "Final checks"),
    "demo": ("🎬", "Demo"),
    "review": ("👀", "Review"),
    "merged": ("🎉", "Merged"),
    "waiting": ("🙋", "Needs you"),
    "dropped": ("🚪", "Out"),
    "refused": ("⛔", "Refused"),
}


def badge(stage):
    emoji, name = LOOK.get(stage, ("▪️", str(stage).capitalize()))
    return f"{emoji} {name}"


def esc(text):
    """Slack reads &, < and > as markup; escape them in text that did not come from the factory."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def quote(text):
    return "\n".join(f"> {line}" for line in (str(text).strip().splitlines() or [""]))


def blob(ref, path):
    return f"https://github.com/{CONFIG['github_repo']}/blob/{ref}/{path}"


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def pipeline(rec):
    """The progress line of the thread root: done steps ticked, the current one bold, the rest grey."""
    steps = ["plan", "build"] + (["fix"] if rec.get("round") else []) + ["verify", "judge", "ci"]
    steps += (["demo"] if CONFIG['demo'].get("enabled") else []) + ["review"]
    stage = rec["stage"]
    at = rec.get("waiting_stage") if stage == "waiting" else stage
    if at == "final":
        at = "ci"
    if at == "merged":
        at = "review"
    pos = steps.index(at) if at in steps else -1
    planned = any(c["station"] == "plan" for c in rec.get("conversations", []))
    out = []
    for i, step in enumerate(steps):
        emoji, name = LOOK[step]
        if step == "fix":
            name = f"Fix {rec['round']}/{max_rounds()}"
        if i < pos:
            out.append(f"{'⏭️' if step == 'plan' and not planned else '✅'} {name}")
        elif i > pos:
            out.append(f"⚪ {name}")
        elif stage == "waiting":
            out.append(f"🙋 *{name}*")
        elif stage == "final":
            out.append(f"⏳ {name}")
        elif stage == "merged":
            out.append(f"✅ {name}")
        else:
            out.append(f"{emoji} *{name}*")
    return " › ".join(out)


def root_note(rec):
    stage = rec["stage"]
    if stage == "waiting":
        return f"🙋 Esperando você no {badge(rec.get('waiting_stage'))}"
    if stage == "blocked":
        return "🚧 Esperando " + (", ".join(rec.get("blocked_on") or []) or "outras entregas")
    if stage == "merged":
        return f"🎉 Mergeado em `{rec['base']}`"
    return {"queued": "⏳ Na fila", "final": "✅ Aprovado pela Judge", "review": "👀 Pronto para sua revisão",
            "dropped": "🚪 Saiu da factory", "refused": "⛔ Não começou: falta algo na descrição"}.get(stage)


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
    facts = [note] if (note := root_note(rec)) else []
    facts.append(f"🔗 <{rec['pr_url']}|PR #{rec['pr_number']}>" if rec.get("pr_url") else "🔗 Sem PR ainda")
    if rec.get("round"):
        facts.append(f"🔧 Correções: {rec['round']} de {max_rounds()}")
    return (f"*<{rec['issue_url']}|{rec['identifier']} · {esc(rec['title'])}>*\n"
            f"{pipeline(rec)}\n" + " · ".join(facts))


def notify(rec, text, human=False, broadcast=False):
    """Post a station update in the issue thread. A stop for the person mentions them and reaches the channel;
    `broadcast` sends the reply to the channel too, without the mention."""
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
              text=text, reply_broadcast=human or broadcast)
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
                    rec.setdefault("inbox", []).append({"source": "Slack", "text": text.replace(mention, "").strip(" :")})
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


STARTS = {
    "plan": "transforma a issue em tarefas com critérios que dá para conferir.",
    "build": "implementa as tarefas, roda as provas do checklist e abre um PR em rascunho.",
    "verify": "um agente que não escreveu o código refaz cada prova do checklist.",
    "judge": "um agente revisa o PR com a skill the-judge e dá o veredito.",
    "fix": "corrige o que a última etapa apontou.",
    "demo": "grava prints de antes e depois e um vídeo por caso de uso.",
}


def start_station(rec, station, prompt, title=None, detail=None, retry=False):
    """`title` and `detail` replace the station's usual Slack line, for a fix round or a handoff. A `retry` runs the
    same station again in a new conversation and keeps its original prompt and retry count."""
    rec["stage"] = station
    first = not rec.get("conversations")
    if not mutate(f"start {station} conversation for {rec['identifier']}"):
        return
    profile = CONFIG.get("station_profiles", {}).get(station, CONFIG["agent_profile_id"])
    out = canvas("POST", "/api/conversations", {
        "agent_profile_id": profile,
        "workspace": {"kind": "LocalWorkspace", "working_dir": rec["worktree"]},
        "worktree": False,
        "autotitle": False,
        "tags": {"factory": rec["identifier"].lower(), "station": station},
        "initial_message": {"role": "user", "content": [{"type": "text", "text": prompt}], "run": True},
    })
    rec["conversation_id"] = out["id"]
    rec["events_mark"] = 0
    if not retry:
        rec["retries"] = 0
        rec["prompt"] = prompt
    rec["station_started_at"] = now()
    rec.setdefault("conversations", []).append({"station": station, "id": out["id"], "at": now(), "profile": profile,
                                                **({"retry": True} if retry else {})})
    emoji, name = LOOK[station]
    # The thread may have been opened long ago, while the issue waited; the first station also reaches the channel.
    notify(rec, f"{emoji} *{title or name + ' começou'}*: {detail or STARTS[station]}\n"
                f"<{CONFIG['canvas_public_url']}/conversations/{out['id']}|👀 Acompanhar ao vivo>", broadcast=first)


def send_to_conversation(rec, text):
    if not mutate(f"send message to {rec['conversation_id']}"):
        return
    rec["events_mark"] = event_count(rec["conversation_id"])
    canvas("POST", f"/api/conversations/{rec['conversation_id']}/events",
           {"role": "user", "content": [{"type": "text", "text": text}], "run": True})


def station_prompt(rec):
    """The prompt the station started with. A station started before the factory kept it reads it back from the Canvas."""
    if rec.get("prompt"):
        return rec["prompt"]
    page = canvas("GET", f"/api/conversations/{rec['conversation_id']}/events/search?limit=10")
    for event in page.get("items", []):
        if event.get("kind") == "MessageEvent" and event.get("source") == "user":
            return "".join(part.get("text", "") for part in event["llm_message"]["content"])
    raise Stop("There is no station prompt to retry with.")


def retry_station(rec, error=None):
    """Cursor dropped its backend connection, and the same conversation keeps the broken process: run the station
    again in a new conversation, which starts a new Cursor process. It is not a Fix round. Without `error`, the
    person asked for it."""
    rec["prompt"] = station_prompt(rec)
    name = LOOK[rec["stage"]][1]
    if error:
        rec["retries"] = rec.get("retries", 0) + 1
        detail = (f"a conversa anterior caiu por erro de conexão do Cursor (`{esc(error[:120])}`). Uma conversa nova, "
                  f"com um processo novo do Cursor, segue com o mesmo pedido (tentativa {rec['retries']} de "
                  f"{MAX_RETRIES}). Não conta como rodada de correção.")
    else:
        rec["retries"] = 0
        detail = "a seu pedido, uma conversa nova, com um processo novo do Cursor, segue com o mesmo pedido."
    start_station(rec, rec["stage"], RETRY_NOTE + rec["prompt"], title=f"{name} recomeçou", detail=detail, retry=True)
    return None


def resume_station(rec):
    """`requests/retry-<ISSUE>`: run the station that stopped again, in a new conversation."""
    rec["stage"] = rec.pop("waiting_stage")
    rec.pop("question", None)
    set_needs_human(rec, False)
    retry_station(rec)


def station_result(rec):
    """None while the station runs; the parsed FACTORY_RESULT once it finished."""
    conv = canvas("GET", f"/api/conversations/{rec['conversation_id']}")
    status = conv.get("execution_status")
    name = LOOK[rec["stage"]][1]
    if status in ("error", "stuck"):
        raise Stop(f"The {name} conversation ended with status `{status}`.")
    if status == "waiting_for_confirmation":
        raise Stop(f"The {name} conversation is waiting for a confirmation in the Canvas.")
    if status != "finished" or event_count(rec["conversation_id"]) <= rec.get("events_mark", 0):
        return None
    text = canvas("GET", f"/api/conversations/{rec['conversation_id']}/agent_final_response").get("response", "")
    match = None
    for match in re.finditer(r"FACTORY_RESULT:\s*(\{.*\})", text):
        pass
    if not match:
        lines = text.strip().splitlines()
        if lines and "RetriableError" in lines[-1] and rec.get("retries", 0) < MAX_RETRIES:
            return retry_station(rec, lines[-1])
        raise Stop(f"The {name} station finished without a FACTORY_RESULT line.")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        raise Stop(f"The {name} station returned an unreadable FACTORY_RESULT: `{match.group(1)[:200]}`")


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
    notify(rec, f"🔄 *Base atualizada*: trouxe o `{base}` mais recente para `{branch}`.")
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
        if s["state"] == "failure" and ci_ignored(s):
            out["pass"].append(s["context"])
            continue
        bucket = {"success": "pass", "pending": "pending"}.get(s["state"], "fail")
        out[bucket].append(s["context"])
    return out


def ci_ignored(status):
    """A failing commit status the project's `ci.ignore` rules say is not about the change, such as a rate limit."""
    description = (status.get("description") or "").lower()
    return any(status["context"].startswith(rule.get("context_prefix", ""))
               and rule.get("description_contains", "").lower() in description for rule in CONFIG["ci_ignore"])


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
    feature = feature_dir(rec["feature"])
    checklist = rec.get("checklist") or ""
    return dict(ci_note=CI_NOTE, identifier=rec["identifier"], issue_url=rec["issue_url"], worktree=rec["worktree"],
                branch=rec["branch"], base=rec["base"], feature=rec["feature"], tasks_file=rec.get("tasks_file", ""),
                checklist=checklist, verified=checklist[:-3] + ".verified.md" if checklist else "",
                pr_url=rec.get("pr_url", ""), pr_number=rec.get("pr_number", ""),
                round=rec.get("round", 0), max_rounds=max_rounds(), feature_dir=feature,
                tasks_dir=artifact("tasks", rec["feature"]), checks_dir=artifact("checks", rec["feature"]),
                discovery=artifact("discovery", rec["feature"]))


def debt_note(rec):
    """Tell Plan to copy the tech-debt record into the feature folder. Empty when the issue already named one."""
    source = rec.get("debt_source")
    if not source:
        return ""
    feature = rec["feature"]
    return (
        "\n## Tech-debt source\n\n"
        f"This issue cites `{source}` and does not name a feature folder. "
        f"Before planning, if `{artifact('discovery', feature)}` does not exist, "
        f"copy `{source}` to that path. Leave `{source}` in `{os.path.dirname(source)}/`. "
        "Do not overwrite an existing `discovery.md`. "
        f"If `{source}` is missing from the worktree, stop with `needs_human`. "
        "Commit the copy together with the tasks file. Do not push.\n"
    )


def start_plan(rec):
    issue = issue_detail(rec["issue_id"])
    start_station(rec, "plan", render("plan.md", issue=issue_block(issue), debt_note=debt_note(rec), result=result_block(
        extra_fields=', "tasks_file": "<path of the tasks file>"'), **paths(rec)))


def start_build(rec, handoff=False):
    issue = issue_detail(rec["issue_id"])
    note = ("A previous Build conversation handed off. Continue from the handoff recorded in the checklist "
            f"`{rec.get('checklist')}`. Do not redo landed slices.") if handoff else ""
    start_station(rec, "build", render("build.md", issue=issue_block(issue), handoff=note, result=result_block(
        extra_status=" | handoff", extra_fields=', "checklist": "<path of the checklist>"'), **paths(rec)),
        **(dict(title="Build continua", detail="a conversa anterior encheu o contexto e passou o bastão. "
                                               "Uma conversa nova segue do ponto marcado no checklist.")
           if handoff else {}))


def start_verify(rec, head):
    start_station(rec, "verify", render("verify.md", head=head, result=result_block(), **paths(rec)))


def start_judge(rec, head):
    start_station(rec, "judge", render("judge.md", head=head, result=result_block(), **paths(rec)))


def start_fix(rec, reason, head, why):
    """`reason` instructs the agent; `why` tells the person, in Slack, what the round fixes."""
    rec["round"] = rec.get("round", 0) + 1
    if rec["round"] > max_rounds():
        rec["round"] = max_rounds()
        rec["rounds_exhausted"] = True
        raise Stop(f"No `APPROVE` after {max_rounds()} fix rounds. Last failure: {reason}")
    rec["fix_from"] = head
    start_station(rec, "fix", render("fix.md", head=head, reason=reason, result=result_block(), **paths(rec)),
                  title=f"Correção {rec['round']} de {max_rounds()}", detail=why)


# ── demo ──────────────────────────────────────────────────────────────────────

def demo_vars():
    """The project's demo variables; `DEMO_VAR_<NAME>` in the environment replaces one, for a value kept out of git."""
    values = dict(CONFIG["demo"].get("vars") or {})
    for key in values:
        values[key] = os.environ.get(f"DEMO_VAR_{key.upper()}", values[key])
    return values


def demo_busy():
    return sum(r["stage"] == "demo" for r in _STATE["issues"].values()) >= CONFIG['demo'].get("max_parallel", 1)


def start_demo(rec, head, back):
    """Record before/after screenshots and use-case videos of the approved head; `back` is the stage to return to."""
    git("fetch", "origin", rec["base"], cwd=rec["worktree"])
    base_sha = git("rev-parse", f"origin/{rec['base']}", cwd=rec["worktree"])
    demo_dir = EVIDENCE / rec["identifier"] / head[:12]
    if mutate(f"create {demo_dir}"):
        demo_dir.mkdir(parents=True, exist_ok=True)
    rec.update(demo_for=head, demo_back=back, demo_dir=str(demo_dir))
    kit = settings.demo_kit(CONFIG)
    if kit is not None and mutate("install the demo kit"):
        tools = Path(CONFIG['demo'].get("tools_path", "/projects/.demo-tools"))
        tools.mkdir(parents=True, exist_ok=True)
        (tools / "demo-kit.mjs").write_text(kit)
    rec.pop("demo_wait_noticed", None)
    issue = issue_detail(rec["issue_id"])
    start_station(rec, "demo", render(
        "demo.md", head=head, base_sha=base_sha, demo_dir=demo_dir, before_worktree=f"{rec['worktree']}-before",
        **demo_vars(), port_before=CONFIG['demo'].get("port_before", 5181), port_after=CONFIG['demo'].get("port_after", 5182), issue=issue_block(issue),
        result=result_block(extra_fields=', "manifest": "<path of manifest.json>"'), **paths(rec)))


def load_manifest(demo_dir):
    """The demo manifest, with every file it names checked to exist inside the demo folder."""
    path = demo_dir / "manifest.json"
    if not path.is_file():
        raise Stop(f"The demo station finished without `{path}`.")
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise Stop(f"`{path}` is not valid JSON: {e}") from None
    root = demo_dir.resolve()
    limit = CONFIG['demo'].get("max_file_mb", 50)

    def check(name, suffixes):
        if not name:
            return
        f = (demo_dir / name).resolve()
        if root not in f.parents or not f.is_file() or f.suffix.lower() not in suffixes:
            raise Stop(f"The demo manifest names `{name}`, which is not a {'/'.join(suffixes)} file in `{demo_dir}`.")
        if f.stat().st_size > limit * 1_000_000:
            raise Stop(f"`{name}` is over {limit} MB.")

    screens, videos = manifest.get("screens") or [], manifest.get("videos") or []
    for screen in screens:
        for shot in screen.get("shots") or []:
            if not shot.get("after"):
                raise Stop(f"The demo screen `{screen.get('title')}` has a shot without an `after` image.")
            for key in ("before", "after", "design"):
                check(shot.get(key), {".png"})
    for video in videos:
        if not video.get("mp4"):
            raise Stop(f"The demo video `{video.get('title')}` has no MP4.")
        check(video["mp4"], {".mp4"})
        check(video.get("gif"), {".gif"})
    if not videos:
        raise Stop("The demo has no video.")
    if not any(screen.get("shots") for screen in screens):
        raise Stop("The demo has no screenshots.")
    return manifest


def demo_files(manifest):
    names = [shot.get(k) for s in manifest["screens"] for shot in s.get("shots") or [] for k in ("design", "before", "after")]
    names += [v.get(k) for v in manifest["videos"] for k in ("gif", "mp4")]
    return list(dict.fromkeys(n for n in names if n))


VIEWPORT_LOOK = {"1440": "🖥️ Desktop", "390": "📱 Mobile"}
THEME_LOOK = {"light": "☀️ Light", "night": "🌙 Night", "dark": "🌙 Night"}
SHOT_COLUMNS = (("🎨 Design", "design"), ("⬅️ Before", "before"), ("✨ After", "after"))


def demo_markdown(rec, manifest, url, target, link=""):
    """The demo as markdown. `url(name)` gives a file's URL; GitHub gets tables and GIFs, Linear plays the MP4s."""
    pr = f"[PR #{rec['pr_number']}]({rec['pr_url']})"
    shots = sum(len(s.get("shots") or []) for s in manifest["screens"])
    gaps = manifest.get("gaps") or []
    counts = [plural(shots, "before/after comparison", "before/after comparisons"),
              plural(len(manifest["videos"]), "video", "videos")] + ([f"{len(gaps)} not shown"] if gaps else [])
    out = [f"🎬 **Feature demo** for {pr} · commit `{rec['demo_for'][:8]}`", "",
           "✅ This commit passed Verify and the-judge. Approve the **feature** from what you see here; "
           "the code review stays on the pull request.", ""]
    if manifest.get("summary", "").strip():
        out += [f"**What changes:** {manifest['summary'].strip()}", ""]
    out += [" · ".join(counts)]
    if link:
        out += ["", link]
    out += ["", "## 🖼️ Screens"]
    for screen in manifest["screens"]:
        out += ["", f"### {screen.get('title', 'Screen')}"]
        if screen.get("criterion"):
            out += [f"🎯 _{screen['criterion']}_"]
        for shot in screen.get("shots") or []:
            viewport = str(shot.get("viewport") or "")
            label = " · ".join(x for x in (VIEWPORT_LOOK.get(viewport, f"{viewport} px" if viewport else ""),
                                           THEME_LOOK.get(shot.get("theme"), shot.get("theme") or "")) if x)
            cols = [(h, shot.get(k)) for h, k in SHOT_COLUMNS if shot.get(k) or k == "before"]
            if target == "github":
                width = 220 if viewport == "390" else (300 if len(cols) == 3 else 420)
                cells = [f'<img src="{url(f)}" width="{width}">' if f else "🆕 _new screen_" for _, f in cols]
                out += ["", f"**{label}**", "", "| " + " | ".join(h for h, _ in cols) + " |",
                        "|" + " --- |" * len(cols), "| " + " | ".join(cells) + " |"]
            else:
                out += ["", f"**{label}**"]
                for h, f in cols:
                    out += [f"{h}: 🆕 new screen, nothing to compare." if not f
                            else f"{h}:\n![{h}: {screen.get('title', '')}]({url(f)})"]
        if screen.get("note"):
            out += ["", f"> 👉 {screen['note']}"]
    out += ["", "## ▶️ Use cases"]
    for n, video in enumerate(manifest["videos"], 1):
        out += ["", f"### {n}. {video.get('title', 'Use case')}"]
        if video.get("criterion"):
            out += [f"🎯 _{video['criterion']}_"]
        out += [f"{i}. {step}" for i, step in enumerate(video.get("steps") or [], 1)]
        seconds = f", {video['seconds']} s" if video.get("seconds") else ""
        if target == "github":
            if video.get("gif"):
                out += ["", f"![{video.get('title', '')}]({url(video['gif'])})"]
            out += ["", f"[▶️ Watch the full video (MP4{seconds})]({url(video['mp4'], raw=False)})"]
        else:
            out += ["", f"![{video.get('title', '')}]({url(video['mp4'])})",
                    f"[▶️ Open the video{seconds}]({url(video['mp4'])})"]
    if gaps:
        out += ["", "## 🙈 Not shown in this demo"] + [f"- {gap}" for gap in gaps]
    return "\n".join(out)


def publish_linear(rec, manifest, demo_dir):
    if not mutate(f"upload the demo of {rec['identifier']} to Linear"):
        return ""
    urls = {name: linear_upload(demo_dir / name) for name in demo_files(manifest) if not name.endswith(".gif")}
    return comment(rec, demo_markdown(rec, manifest, lambda name, raw=True: urls[name], "linear"))


# Vercel builds every pushed branch and reads vercel.json from the pushed commit, so the evidence branch
# carries its own vercel.json that turns its deployments off.
EVIDENCE_BRANCH_FILES = {
    "vercel.json": json.dumps({"git": {"deploymentEnabled": False}}, indent=2) + "\n",
    "README.md": "# factory-evidence\n\nScreenshots and videos the agentic factory records for pull request review.\n"
                 "Media only: never merge this branch, and nothing here is deployed.\n",
}


def push_evidence(rec, files, demo_dir):
    """Commit the demo files to the evidence branch with a private index, never touching a work tree."""
    branch = CONFIG['demo'].get("evidence_branch", "factory-evidence")
    prefix = f"{rec['identifier']}/{rec['demo_for'][:12]}"
    for _ in range(3):
        run(["git", "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], check=False)
        parent = git("rev-parse", f"refs/remotes/origin/{branch}") if ref_exists(f"refs/remotes/origin/{branch}") else ""
        fd, index = tempfile.mkstemp(prefix="evidence-index-")
        os.close(fd)
        os.unlink(index)
        env = {"GIT_INDEX_FILE": index}
        try:
            run(["git", "read-tree", parent] if parent else ["git", "read-tree", "--empty"], env=env)
            for path, text in EVIDENCE_BRANCH_FILES.items():
                blob = run(["git", "hash-object", "-w", "--stdin"], input=text).stdout.strip()
                run(["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"], env=env)
            for name in files:
                blob = git("hash-object", "-w", str(demo_dir / name))
                run(["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{prefix}/{name}"], env=env)
            tree = run(["git", "write-tree"], env=env).stdout.strip()
        finally:
            Path(index).unlink(missing_ok=True)
        commit = git("commit-tree", tree, *(["-p", parent] if parent else []),
                     "-m", f"evidence: {rec['identifier']} demo of {rec['demo_for'][:12]}")
        if run(["git", "push", "origin", f"{commit}:refs/heads/{branch}"], check=False).returncode == 0:
            return commit, prefix
    raise RuntimeError(f"Could not push the demo to `{branch}`.")


def publish_github(rec, manifest, demo_dir, linear_url):
    if not mutate(f"push the demo of {rec['identifier']} and comment on PR #{rec['pr_number']}"):
        return ""
    commit, prefix = push_evidence(rec, demo_files(manifest), demo_dir)
    repo = CONFIG["github_repo"]

    def url(name, raw=True):
        return f"https://github.com/{repo}/blob/{commit}/{prefix}/{name}" + ("?raw=true" if raw else "")

    link = f"▶️ The videos play inline on the [Linear issue]({linear_url})." if linear_url else ""
    body = f"{FACTORY_MARK} " + demo_markdown(rec, manifest, url, "github", link)
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(body)
    try:
        out = run(["gh", "pr", "comment", str(rec["pr_number"]), "--body-file", path], cwd=rec["worktree"]).stdout
    finally:
        os.unlink(path)
    found = re.search(r"https://github\.com/\S+", out)
    return found.group(0) if found else rec["pr_url"]


def after_demo(rec, result):
    head = rec["demo_for"]
    if git("status", "--porcelain", cwd=rec["worktree"]):
        raise Stop("The demo station left uncommitted changes in the working tree.")
    if git("rev-parse", "HEAD", cwd=rec["worktree"]) != head:
        raise Stop("The head commit changed during the demo.")
    demo_dir = Path(rec["demo_dir"])
    manifest = load_manifest(demo_dir)
    try:
        linear_url = publish_linear(rec, manifest, demo_dir)
        github_url = publish_github(rec, manifest, demo_dir, linear_url)
    except RuntimeError as e:
        raise Stop(f"Recording worked, but publishing the demo failed: {e}") from None
    rec["demo_head"] = head
    rec.setdefault("demos", []).append({"head": head, "linear": linear_url, "github": github_url, "at": now()})
    shots = sum(len(s.get("shots") or []) for s in manifest["screens"])
    notify(rec, f"🎬 *Demo pronta*: {plural(shots, 'comparação', 'comparações')} de antes e depois e "
                f"{plural(len(manifest['videos']), 'vídeo', 'vídeos')}.\n"
                f"<{linear_url}|▶️ Assistir no Linear> · <{github_url}|🖼️ Ver no PR>")
    rec["stage"] = rec.pop("demo_back", "final")
    if rec["stage"] == "final":
        check_final(rec)


def take_requests(state):
    """A file `demo-<ISSUE>` in the requests folder records the demo of a pull request already in review;
    `retry-<ISSUE>` runs the station an issue stopped at again, in a new conversation."""
    if not REQUESTS.is_dir():
        return
    for req in sorted(REQUESTS.iterdir(), key=lambda p: p.stat().st_mtime):
        kind, _, ident = req.name.partition("-")
        rec = state["issues"].get(ident)
        if (kind == "retry" and rec and rec["stage"] == "waiting" and rec.get("waiting_stage") in STATIONS
                and rec.get("conversation_id")):
            if not mutate(f"retry the {rec['waiting_stage']} of {ident}"):
                continue
            req.unlink(missing_ok=True)
            try:
                resume_station(rec)
            except Stop as stop:
                stop_for_human(rec, str(stop))
            except Exception:
                log.error("%s retry request failed:\n%s", ident, traceback.format_exc())
            save_state(state)
            continue
        if kind != "demo" or not rec or rec["stage"] != "review" or not rec.get("pr_number"):
            log.warning("ignoring request %s: no issue that it applies to", req.name)
            if mutate(f"remove request {req.name}"):
                req.unlink(missing_ok=True)
            continue
        if demo_busy() or not mutate(f"start the demo of {ident}"):
            return
        req.unlink(missing_ok=True)
        try:
            start_demo(rec, require_clean_and_pushed(rec), "review")
        except Stop as stop:
            stop_for_human(rec, str(stop))
        except Exception:
            log.error("%s demo request failed:\n%s", ident, traceback.format_exc())
        save_state(state)


def after_plan(rec, result):
    tasks = result.get("tasks_file", "")
    require_tracked(rec, tasks)
    if not tasks.startswith(artifact("tasks", rec["feature"])):
        raise Stop(f"The tasks file `{tasks}` is outside `{artifact('tasks', rec['feature'])}`.")
    blocks, go_live = blocking_unresolved(Path(rec["worktree"]) / tasks)
    if blocks:
        raise Stop("The plan has blocking Unresolved questions:\n" + "\n".join(f"- {q}" for q in blocks))
    rec["tasks_file"] = tasks
    notify(rec, f"✅ *Plan pronto*: `{tasks}`."
                + (f"\n⚠️ *Bloqueia o go-live*: {esc('; '.join(go_live))}" if go_live else ""))
    start_build(rec)


def after_build(rec, result):
    if result.get("status") == "handoff":
        rec["checklist"] = result.get("checklist") or rec.get("checklist")
        return start_build(rec, handoff=True)
    checklist = result.get("checklist", "")
    require_tracked(rec, checklist)
    if not checklist.startswith(artifact("checks", rec["feature"])) or checklist.endswith(".verified.md"):
        raise Stop(f"The checklist `{checklist}` is outside `{artifact('checks', rec['feature'])}`.")
    rec["checklist"] = checklist
    head = require_clean_and_pushed(rec)
    prs = gh_json("pr", "list", "--head", rec["branch"], "--base", rec["base"], "--state", "open",
                  "--json", "number,url", cwd=rec["worktree"]) or []
    if not prs:
        raise Stop(f"No open pull request from `{rec['branch']}` into `{rec['base']}`.")
    rec["pr_number"], rec["pr_url"] = prs[0]["number"], prs[0]["url"]
    notify(rec, f"✅ *Build pronto*: PR em rascunho <{rec['pr_url']}|#{rec['pr_number']}> aberto. "
                f"<{blob(head, checklist)}|📋 Checklist>")
    comment(rec, f"🔨 **Build done.** Draft pull request: [PR #{rec['pr_number']}]({rec['pr_url']})")
    changed = git("diff", "--name-only", f"origin/{rec['base']}...HEAD", cwd=rec["worktree"]).splitlines()
    guard = CONFIG["schema_guard"]
    schema = [f for f in changed if any(f.startswith(p) for p in guard["paths"])]
    if schema and not rec.get("schema_acknowledged"):
        rec["schema_acknowledged"] = True
        raise Stop("This change touches schema or RLS (" + ", ".join(schema[:5]) + f"), and `{guard['command']}` "
                   "cannot run in the sandbox. Run it locally and reply with the result.")
    sync_then_verify(rec, head)


def sync_then_verify(rec, head):
    """Bring the latest base in before Verify, so what gets verified and judged is current. CI waits for APPROVE."""
    try:
        head = sync_with_base(rec)
    except Conflict as conflict:
        files = conflict.args[0]
        return start_fix(rec, f"Merging `origin/{rec['base']}` into `{rec['branch']}` conflicts in "
                              f"{', '.join(files)}. Merge `origin/{rec['base']}`, resolve the conflicts "
                              "keeping the intent of both sides, run the affected proofs, commit and push.", head,
                         why=f"trazer o `{rec['base']}` mais recente conflita em "
                             + ", ".join(f"`{f}`" for f in files[:5]) + (" e outros" if len(files) > 5 else "") + ".")
    start_verify(rec, head)


def check_final(rec):
    """Before In review: the branch holds the current base, GitHub can merge the pull request and CI is green on the head."""
    head = require_clean_and_pushed(rec)
    if not base_is_merged(rec):
        rec["base_syncs"] = rec.get("base_syncs", 0) + 1
        if rec["base_syncs"] > CONFIG["max_base_syncs"]:
            raise Stop(f"`{rec['base']}` kept moving after review {CONFIG['max_base_syncs']} times. "
                       "Reply to merge it once more and run Verify and Judge again.")
        notify(rec, f"🔄 *O `{rec['base']}` andou depois da revisão*: vou trazer as mudanças, "
                    "e Verify e Judge rodam de novo.")
        return sync_then_verify(rec, head)
    pr = gh_json("pr", "view", str(rec["pr_number"]), "--json", "mergeable,headRefOid", cwd=rec["worktree"])
    if pr["headRefOid"] != head or pr["mergeable"] == "UNKNOWN":
        return
    if pr["mergeable"] == "CONFLICTING":
        return start_fix(rec, f"GitHub reports that the pull request conflicts with `{rec['base']}`. "
                              f"Merge `origin/{rec['base']}`, resolve the conflicts, commit and push.", head,
                         why=f"o GitHub aponta conflito entre o PR e o `{rec['base']}`.")
    checks = pr_checks(rec, head)
    if checks["fail"]:
        rec.pop("final_ci_since", None)
        return start_fix(rec, "CI failing on the head commit: " + ", ".join(checks["fail"]), head,
                         why="o CI falhou em " + ", ".join(checks["fail"]) + " no commit final.")
    if checks["pending"]:
        return
    if not checks["pass"]:
        since = rec.setdefault("final_ci_since", now())
        if time.time() - datetime.fromisoformat(since).timestamp() >= CONFIG["no_checks_timeout_seconds"]:
            rec.pop("final_ci_since", None)
            raise Stop("No CI checks reported on the head commit.")
        return
    rec.pop("final_ci_since", None)
    if CONFIG['demo'].get("enabled") and rec.get("demo_head") != head:
        if not demo_busy():
            return start_demo(rec, head, "final")
        if not rec.get("demo_wait_noticed"):
            rec["demo_wait_noticed"] = True
            notify(rec, "⏳ *Demo na fila*: outra demo está gravando agora. Esta começa assim que ela terminar.")
        return
    demo = next((d for d in reversed(rec.get("demos", [])) if d["head"] == head), None)
    if mutate(f"mark PR #{rec['pr_number']} ready"):
        run(["gh", "pr", "ready", str(rec["pr_number"])], cwd=rec["worktree"])
    set_status(rec, "review")
    rec["stage"] = "review"
    pr_link = f"PR #{rec['pr_number']}"
    comment(rec, "👀 **Ready for your review**\n\n"
                 f"✅ the-judge approved the head commit: [review]({rec['last_review']})\n\n"
                 + (f"🎬 Demo for your approval: [watch it]({demo['linear']})\n\n" if demo else "")
                 + f"🔗 Pull request: [{pr_link}]({rec['pr_url']})\n\n"
                 + ("Approve the feature from the demo, then review the pull request." if demo
                    else "Review the pull request.") + " Merging is up to you.")
    notify(rec, f"🎉 *Pronto para sua revisão!* A Judge aprovou o <{rec['pr_url']}|{pr_link}>."
                + (f"\n🎬 Demo: <{demo['linear']}|▶️ assistir no Linear> · <{demo['github']}|🖼️ ver no PR>"
                   if demo else "")
                + ("\n👉 Aprove a feature pela demo e revise o PR." if demo else "\n👉 Revise o PR.")
                + " O merge é com você.", human=True)


def after_verify(rec, result):
    head = require_clean_and_pushed(rec)
    verified = paths(rec)["verified"]
    require_tracked(rec, verified)
    if git("log", "-1", "--format=%H", "--", verified, cwd=rec["worktree"]) != head:
        raise Stop(f"`{verified}` is not the last commit, so it does not verify the head.")
    report = (Path(rec["worktree"]) / verified).read_text()
    report_link = f"<{blob(head, verified)}|📄 Relatório>"
    if re.search(r"\*\*Verdict\*\*:\s*PASS", report):
        notify(rec, f"✅ *Verify passou*: todas as provas do checklist conferem. {report_link}")
        return start_judge(rec, head)
    notify(rec, f"❌ *Verify reprovou*: algumas provas não conferem. {report_link}")
    start_fix(rec, f"The Verifier returned FAIL. Read `{verified}` for the failing checks.", head,
              why="corrige as provas que o Verify reprovou, a partir do relatório.")


JUDGE_SAYS = {"REQUEST_CHANGES": ("✋", "pediu mudanças"), "COMMENT": ("💬", "deixou comentários")}


def after_judge(rec, result):
    head = require_clean_and_pushed(rec)
    verdict, url = judge_verdict(rec, head)
    rec["last_review"] = url
    if verdict != "APPROVE":
        emoji, says = JUDGE_SAYS.get(verdict, ("💬", f"respondeu {verdict}"))
        notify(rec, f"{emoji} *Judge {says}*. <{url}|📝 Ver a revisão>")
        return start_fix(rec, f"the-judge returned {verdict}. Address the review at {url}.", head,
                         why=f"responde à <{url}|revisão> da Judge.")
    notify(rec, f"✅ *Judge aprovou*. <{url}|📝 Ver a revisão>\n🚦 Agora confiro o CI do commit aprovado"
                + (", e depois vem a Demo." if CONFIG['demo'].get("enabled") else "."))
    rec["stage"] = "final"
    check_final(rec)


def after_fix(rec, result):
    head = require_clean_and_pushed(rec)
    if head == rec.get("fix_from"):
        checks = pr_checks(rec, head)
        if checks["fail"] or checks["pending"] or not checks["pass"]:
            raise Stop("The fix station finished without a new commit.")
        notify(rec, "📨 *Correção sem commit*: o head continua. O que restava no CI não é defeito deste PR.")
        rec["stage"] = "final"
        return check_final(rec)
    notify(rec, f"📤 *Correção {rec['round']} enviada* (`{head[:8]}`): Verify e Judge rodam de novo, e o CI roda depois do APPROVE.")
    sync_then_verify(rec, head)


AFTER = {"plan": after_plan, "build": after_build, "verify": after_verify, "judge": after_judge, "fix": after_fix,
         "demo": after_demo}


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
        "agent_profile_id": CONFIG.get("station_profiles", {}).get("mention", CONFIG["agent_profile_id"]),
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
          text=f"👀 Deixa comigo! <{CONFIG['canvas_public_url']}/conversations/{out['id']}|Acompanhar ao vivo>")


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
        final = f"🛑 O agente parou com status *{status}*." + (f"\n\n{final}" if final else "")
    if mutate(f"post the mention answer in {rec['channel']}/{rec['thread']}"):
        slack("chat.postMessage", channel=rec["channel"], thread_ts=rec["thread"],
              text=(final or "✅ Feito, sem mensagem final.")[:39000])
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
    rec["question"] = question
    where = badge(rec["waiting_stage"])
    notify(rec, f"🙋 *Preciso de você* no {where}\n{quote(esc(question))}\n"
                "💬 Responda aqui na thread ou na issue do Linear.", human=True)
    set_needs_human(rec, True)
    comment(rec, f"🙋 **Needs you at {where}**\n\n{quote(question)}\n\n"
                 "Reply here or in the Slack thread, and the station picks it up.")


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


def fix_after_answer(rec, replies):
    """Fix rounds ran out on a Verify FAIL. The answer opens one more Fix round, because the finished Verify
    conversation would only verify the same head again."""
    try:
        head = require_clean_and_pushed(rec)
    except Stop as stop:
        return stop_for_human(rec, str(stop))
    answers = "\n\n".join(f"The person answered through {r['source']}:\n\n{quote(r['text'])}" for r in replies)
    reason = (f"The Verifier returned FAIL. Read `{paths(rec)['verified']}` for the failing checks. The factory "
              f"stopped after {max_rounds()} fix rounds, and the person answered.\n\n{answers}\n\n"
              f"First, add a comment on Linear issue {rec['identifier']} with this answer translated to English. "
              f"Start the comment with `[factory] 📨 Answer from the person (via {replies[0]['source']}):`. "
              "Use the Linear MCP. Then fix what failed, following the answer.")
    for key in ("waiting_stage", "question", "rounds_exhausted", "base_syncs"):
        rec.pop(key, None)
    rec["round"] = max_rounds() - 1
    set_needs_human(rec, False)
    notify(rec, "📨 *Recebi sua resposta*: abro mais uma rodada de 🔧 Fix com ela, sem voltar ao Verify.")
    start_fix(rec, reason, head, why="corrige o que o Verify reprovou, seguindo a sua resposta.")


def deliver_replies(rec):
    replies = rec.pop("inbox", []) + linear_replies(rec)
    if not replies:
        return
    if rec["stage"] == "waiting" and rec.get("waiting_stage") == "verify" and rec.get("rounds_exhausted"):
        return fix_after_answer(rec, replies)
    if not rec.get("conversation_id"):
        # A stop before any conversation (a base or tasks file problem): the answer means "check again".
        if rec["stage"] == "waiting":
            rec.pop("waiting_stage", None)
            rec["stage"] = "blocked"
            set_needs_human(rec, False)
            notify(rec, "📨 *Recebi sua resposta*: vou conferir a issue de novo.")
        return
    for reply in replies:
        send_to_conversation(rec, render("answer.md", source=reply["source"], answer=reply["text"],
                                         identifier=rec["identifier"]))
    if rec["stage"] == "waiting":
        # The answer went to the last conversation, so its station's gate runs again when it finishes.
        rec.pop("waiting_stage", None)
        rec["stage"] = rec["conversations"][-1]["station"]
        if rec.pop("rounds_exhausted", False):
            rec["round"] = max_rounds() - 1
        rec.pop("base_syncs", None)
        set_needs_human(rec, False)
    notify(rec, f"📨 *Recebi sua resposta*: o {badge(rec['stage'])} continua.")


def reopen_from_review(rec):
    """A message from the person in the thread of an issue in review asks for a change: one more Fix round."""
    requests = rec.pop("inbox", [])
    if not requests:
        return
    pr = gh_json("pr", "view", str(rec["pr_number"]), "--json", "state", cwd=rec["worktree"])
    if pr["state"] != "OPEN":
        notify(rec, f"🚪 *Não reabro*: o <{rec['pr_url']}|PR #{rec['pr_number']}> está "
                    f"{'com merge' if pr['state'] == 'MERGED' else 'fechado'}. Abra uma issue nova para a mudança.")
        return
    head = require_clean_and_pushed(rec)
    request = "\n\n".join(re.sub(r"<@\w+>\s*", "", r["text"]).strip(" :") for r in requests)
    if not mutate(f"reopen {rec['identifier']} from review for a Fix round"):
        return
    run(["gh", "pr", "ready", str(rec["pr_number"]), "--undo"], cwd=rec["worktree"])
    set_status(rec, "progress")
    for key in ("rounds_exhausted", "base_syncs", "review_checks", "final_ci_since"):
        rec.pop(key, None)
    rec["round"] = 0
    notify(rec, f"📝 *Pedido recebido na revisão*: o <{rec['pr_url']}|PR #{rec['pr_number']}> volta para "
                "rascunho, a issue volta para In Progress e uma rodada de Fix começa com o seu pedido.")
    start_fix(rec, "The person asked for this change while the pull request was in review (via Slack):\n\n"
                   + quote(request) + "\n\n"
                   f"First, add a comment on Linear issue {rec['identifier']} with this request translated to "
                   "English. Start the comment with `[factory] 📝 Change requested in review (via Slack):`. "
                   "Use the Linear MCP. Then make the change.", head,
              why="faz o que você pediu aqui na thread.")


def advance(rec):
    deliver_replies(rec)
    if rec["stage"] == "ci":
        # A record saved before CI moved after the Judge: go straight to Verify.
        return start_verify(rec, require_clean_and_pushed(rec))
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
    if fields.get("debt_source"):
        rec["debt_source"] = fields["debt_source"]
    return rec


def refuse(rec, why, why_pt):
    """An issue the factory cannot take; the person fixes the description and removes the label to retry."""
    label = CONFIG["needs_human_label"]
    rec["stage"] = "refused"
    notify(rec, f"⛔ *Não consigo começar*: {esc(why_pt)}\n"
                f"👉 Corrija a descrição e remova a label `{label}` para tentar de novo.", human=True)
    set_needs_human(rec, True)
    comment(rec, f"⛔ **Can't start yet:** {why}\n\nFix the description, then remove the `{label}` label to retry.")


def begin(rec, issue=None):
    """Start the issue's first station once nothing holds it. Returns True when it took a slot."""
    issue = issue or issue_detail(rec["issue_id"])
    labels = {l["name"] for l in issue["labels"]["nodes"]}
    if issue["state"]["name"] != CONFIG["states"]["ready"] or CONFIG["label"] not in labels:
        rec["stage"] = "dropped"
        notify(rec, f"🚪 *Saiu da factory*: a issue não está mais em {CONFIG['states']['ready']} "
                    f"com a label `{CONFIG['label']}`.")
        return False
    reasons = open_blockers(issue)
    if reasons:
        rec["stage"] = "blocked"
        if rec.get("blocked_on") != reasons:
            rec["blocked_on"] = reasons
            notify(rec, "🚧 *Esperando*: " + ", ".join(reasons) + ". Começa sozinha quando estiverem Done ou com merge.")
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
        notify(rec, f"⏭️ *Plan pulado*: `{task}` já está planejado no `{rec['base']}`.")
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
            folder = feature_dir("<feature>") + "/"
            refuse(rec, f"the description must name the feature folder as `{folder}`.",
                   f"a descrição precisa citar a pasta da feature, como `{folder}`.")
            continue
        if rec["branch"] == rec["base"] or rec["branch"] in ("main", "master"):
            refuse(rec, f"`Branch:` `{rec['branch']}` is the base. Name a branch of its own.",
                   f"a `Branch:` `{rec['branch']}` é a própria base. Use uma branch só desta issue.")
            continue
        if not mutate(f"pick up {issue['identifier']} ({rec['branch']} from {rec['base']})"):
            continue
        state["issues"][issue["identifier"]] = rec
        notify(rec, f"📥 *Entrou na factory*: parte do `{rec['base']}` na branch `{rec['branch']}`.")


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
                    notify(rec, f"⏳ *Na fila*: posição {position}. Já há {CONFIG['max_active']} issues rodando; "
                                "esta começa quando uma vaga abrir.")
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
        refresh_config()
        _STATE["issues"] = state["issues"]
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
        try:
            announce_merges(state)
        except Exception:
            log.error("merge check failed:\n%s", traceback.format_exc())
        try:
            clean_worktrees(state)
        except Exception:
            log.error("worktree cleanup failed:\n%s", traceback.format_exc())
        for rec in [r for r in state["issues"].values() if r["stage"] == "review" and r.get("inbox")]:
            try:
                reopen_from_review(rec)
            except Stop as stop:
                stop_for_human(rec, str(stop))
            except Exception:
                log.error("%s reopen failed:\n%s", rec["identifier"], traceback.format_exc())
            save_state(state)
        for rec in (r for r in state["issues"].values() if r["stage"] == "review" and r.get("review_checks", 0) < 3):
            try:
                keep_in_review(rec)
            except Exception:
                log.error("%s review status failed:\n%s", rec["identifier"], traceback.format_exc())
        try:
            take_requests(state)
        except Exception:
            log.error("requests failed:\n%s", traceback.format_exc())
        try:
            pickup(state)
        except Exception:
            log.error("pickup failed:\n%s", traceback.format_exc())
        fill_slots(state)
        save_state(state)


FINISHED = ("finished", "error", "stuck", "waiting_for_confirmation")


def stations_active(state):
    return any(r["stage"] in STATIONS and r.get("conversation_id") for r in state["issues"].values())


def quick_tick(state):
    """Between full ticks: advance only the stations whose conversation just ended. Touches the Canvas, not Linear."""
    with LOCK:
        _STATE["issues"] = state["issues"]
        for rec in [r for r in state["issues"].values() if r["stage"] in STATIONS and r.get("conversation_id")]:
            try:
                if canvas("GET", f"/api/conversations/{rec['conversation_id']}").get("execution_status") in FINISHED:
                    advance(rec)
                    save_state(state)
            except Stop as stop:
                stop_for_human(rec, str(stop))
                save_state(state)
            except Exception:
                log.error("%s quick check failed:\n%s", rec["identifier"], traceback.format_exc())


def announce_merges(state):
    """A pull request in review that got merged: its thread hears it once, and the issue leaves the review loops.
    Merges from before the factory first looked are marked without a message."""
    review = {r["pr_number"]: r for r in state["issues"].values() if r["stage"] == "review" and r.get("pr_number")}
    if not review:
        return
    since = datetime.fromisoformat(state.setdefault("merge_notices_since", now()))
    merged = gh_json("pr", "list", "--repo", CONFIG["github_repo"], "--state", "merged", "--limit", "50",
                     "--json", "number,mergedAt,mergedBy,baseRefName") or []
    for pr in merged:
        rec = review.get(pr["number"])
        if not rec:
            continue
        rec.update(stage="merged", merged_at=pr["mergedAt"])
        if datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00")) < since:
            log.info("%s: PR #%s was merged before merge notices started", rec["identifier"], pr["number"])
            continue
        by = (pr.get("mergedBy") or {}).get("login")
        notify(rec, f"🎉 *PR mergeado!* <{rec['pr_url']}|PR #{rec['pr_number']}> entrou em `{pr['baseRefName']}`"
                    + (f" por {esc(by)}" if by else "") + ". A issue saiu da factory. Valeu! 🙌", broadcast=True)


def clean_worktrees(state):
    """Once the grace period after a merge is over, the issue's worktree and local branch go. What the merge did not
    carry keeps them, and the issue thread hears why once."""
    grace = timedelta(hours=CONFIG.get("worktree_cleanup_hours", 24))
    removed = False
    for rec in state["issues"].values():
        if rec["stage"] != "merged" or rec.get("worktree_removed_at") or not rec.get("merged_at"):
            continue
        if datetime.now(timezone.utc) - datetime.fromisoformat(rec["merged_at"].replace("Z", "+00:00")) < grace:
            continue
        try:
            why = clean_worktree(rec)
        except Exception:
            log.error("%s worktree cleanup failed:\n%s", rec["identifier"], traceback.format_exc())
            continue
        if why is None:
            removed = True
        elif why != rec.get("worktree_kept"):
            rec["worktree_kept"] = why
            notify(rec, f"🧹 *Worktree mantido*: {why} Ele fica em `{rec['worktree']}` até isso ser resolvido.")
    if removed and mutate("prune worktrees"):
        git("worktree", "prune")


def clean_worktree(rec):
    """None once the worktree and branch are gone; otherwise why they stay."""
    path, branch = Path(rec["worktree"]), rec["branch"]
    if path.exists():
        conv = rec.get("conversation_id")
        if conv and canvas("GET", f"/api/conversations/{conv}").get("execution_status") == "running":
            return "uma conversa da Canvas ainda está rodando nele."
        if git("status", "--porcelain", cwd=path):
            return "ele tem mudanças fora de commit."
    head = git("rev-parse", f"refs/heads/{branch}") if ref_exists(f"refs/heads/{branch}") else None
    if head:
        # The base takes pull requests as merge commits and GitHub deletes the remote branch, so `git branch -d`
        # refuses. The branch is safe to delete when its head is inside the pull request's merge commit.
        pr = gh_json("pr", "view", str(rec["pr_number"]), "--repo", CONFIG["github_repo"], "--json", "mergeCommit")
        merge = ((pr or {}).get("mergeCommit") or {}).get("oid")
        if not merge:
            return f"não achei o merge commit do PR #{rec['pr_number']}."
        if run(["git", "cat-file", "-e", f"{merge}^{{commit}}"], check=False).returncode:
            git("fetch", "origin", merge)
        if run(["git", "merge-base", "--is-ancestor", head, merge], check=False).returncode:
            return f"a branch `{branch}` tem commits que não entraram no PR #{rec['pr_number']}."
    if not mutate(f"remove worktree {path} and branch {branch}"):
        return None
    if path.exists():
        git("worktree", "remove", str(path))
    if head:
        git("branch", "-D", branch)
    rec["worktree_removed_at"] = now()
    rec.pop("worktree_kept", None)
    log.info("%s: removed worktree %s and branch %s", rec["identifier"], path, branch)
    return None


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
    if CONFIG.get("dashboard", {}).get("enabled", True):
        try:
            log.info("dashboard on %s:%s", *dashboard.start())
        except Exception:
            log.error("dashboard failed to start:\n%s", traceback.format_exc())
    last_tick = 0.0
    while True:
        woken = WAKE.is_set()
        WAKE.clear()
        if woken or time.time() - last_tick >= CONFIG["poll_seconds"]:
            tick(state)
            last_tick = time.time()
        elif stations_active(state):
            quick_tick(state)
        handle_mentions(state)
        # Running conversations are checked often, so the next station starts soon after one ends
        # and a mention's answer reaches Slack quickly. Linear is only read on the full tick.
        waits = [max(1.0, CONFIG["poll_seconds"] - (time.time() - last_tick))]
        if mentions_active(state):
            waits.append(CONFIG["mentions"]["poll_seconds"])
        if stations_active(state):
            waits.append(CONFIG.get("active_poll_seconds", 15))
        WAKE.wait(min(waits))


if __name__ == "__main__":
    main()
