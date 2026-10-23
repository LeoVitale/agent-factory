"""The `factory` command: set up the machine, install the factory in a project, and run its instance.

  factory setup            build the image and start the shared canvas (once per machine)
  factory init             in a project: write .factory/ and register the instance
  factory up [<slug>]      check the instance, then start its orchestrator
  factory down [<slug>]    stop the instance's orchestrator
  factory status           one line per instance

FACTORY_HOME (default ~/agent-sandbox) holds the canvas state, the clones and `instances/<slug>/`.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ENGINE_ROOT / "templates" / "factory"
IMAGE = "agent-sandbox/agent-canvas-cursor:1.31.0"
CANVAS_PROJECT = "agent-sandbox"
CANVAS_PORT = 8200
FIRST_PORT = 8202
ENV_KEYS = ("LINEAR_API_KEY", "GH_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
PROMPTS = ("_result.md", "answer.md", "plan.md", "build.md", "verify.md", "judge.md", "fix.md", "demo.md")
SUPPORTED_VERSIONS = (1,)
IDLE_STAGES = ("review", "merged", "dropped", "waiting", "blocked", "queued", "refused")


class Fail(Exception):
    """Stop the command with this exit code; the message lines go to stderr."""

    def __init__(self, code, *lines):
        super().__init__("\n".join(lines))
        self.code = code


def run(args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode:
        raise Fail(p.returncode, f"{' '.join(args)} failed: {(p.stderr or p.stdout).strip()[:500]}")
    return p


def home():
    return Path(os.environ.get("FACTORY_HOME", Path.home() / "agent-sandbox"))


def instances_dir():
    return home() / "instances"


def instances():
    root = instances_dir()
    return sorted(p.name for p in root.iterdir() if (p / "instance.json").is_file()) if root.is_dir() else []


def read_instance(slug):
    path = instances_dir() / slug / "instance.json"
    if not path.is_file():
        raise Fail(1, f"No instance `{slug}` in {instances_dir()}.")
    return json.loads(path.read_text())


def read_env(slug):
    path = instances_dir() / slug / ".env"
    values = {}
    for line in path.read_text().splitlines() if path.is_file() else []:
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            values[key.strip()] = value.strip()
    return values


def github_repo(url):
    """`owner/name` from a github.com remote URL, or None."""
    for prefix in ("git@github.com:", "https://github.com/", "ssh://git@github.com/"):
        if url.startswith(prefix):
            path = url[len(prefix):].rstrip("/")
            return path[:-4] if path.endswith(".git") else path
    return None


def project_root(cwd):
    p = run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    if p.returncode:
        raise Fail(1, f"{cwd} is not a git repository.")
    return Path(p.stdout.strip())


def project_remote(top):
    p = run(["git", "remote", "get-url", "origin"], cwd=top, check=False)
    url = p.stdout.strip() if p.returncode == 0 else ""
    repo = github_repo(url)
    if not repo:
        raise Fail(1, f"The origin of {top} is not on github.com" + (f": {url}" if url else ": there is no origin."))
    return url, repo


def slug_here(cwd):
    _, repo = project_remote(project_root(cwd))
    return repo.split("/")[1]


# ── init ──────────────────────────────────────────────────────────────────────

def free_port():
    used = {read_instance(s).get("dashboard", {}).get("port") for s in instances()}
    port = FIRST_PORT
    while port in used:
        port += 1
    return port


def slack_manifest(slug):
    """A Socket Mode app with the scopes and events the engine uses."""
    return {
        "display_information": {"name": f"factory {slug}"},
        "features": {"bot_user": {"display_name": f"factory-{slug}", "always_online": True}},
        "oauth_config": {"scopes": {"bot": [
            "chat:write", "channels:manage", "channels:read", "channels:join", "channels:history",
            "reactions:write", "users:read"]}},
        "settings": {"event_subscriptions": {"bot_events": ["message.channels"]},
                     "socket_mode_enabled": True, "org_deploy_enabled": False, "token_rotation_enabled": False},
    }


def init(cwd):
    top = project_root(cwd)
    _, repo = project_remote(top)
    slug = repo.split("/")[1]
    head = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=top, check=False)
    base = head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else "main"
    kit, inst = top / ".factory", instances_dir() / slug
    if kit.exists():
        raise Fail(2, f"{kit} already exists. Nothing changed.")
    if inst.exists():
        raise Fail(2, f"{inst} already exists. Nothing changed.")
    # Values that belong to this machine and this person, not to the project: take them from an instance already here.
    known = read_instance(instances()[0]) if instances() else {}
    port = free_port()

    config = json.loads((TEMPLATES / "config.json").read_text())
    config.update(github_repo=repo, base_branch=base)
    (kit / "prompts").mkdir(parents=True)
    (kit / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    for name in PROMPTS:
        shutil.copyfile(TEMPLATES / "prompts" / name, kit / "prompts" / name)

    inst.mkdir(parents=True)
    instance = {
        "project_path": str(top),
        "repo_path": f"/projects/{slug}",
        "worktrees_path": f"/projects/{slug}-worktrees",
        "config_ref": f"origin/{base}",
        "dashboard": {"port": port},
        "canvas_public_url": known.get("canvas_public_url", f"http://localhost:{CANVAS_PORT}/canvas"),
        "agent_profile_id": known.get("agent_profile_id", ""),
        "station_profiles": known.get("station_profiles", {}),
        "slack_person_id": known.get("slack_person_id", ""),
        "mentions": {"channels": [], "workspace": f"/projects/{slug}-mentions"},
        "demo": {"evidence_path": f"/projects/{slug}-evidence", "tools_path": "/projects/.demo-tools"},
    }
    (inst / "instance.json").write_text(json.dumps(instance, indent=2) + "\n")
    env = inst / ".env"
    env.write_text("".join(f"{key}=\n" for key in ENV_KEYS))
    env.chmod(0o600)
    (inst / "slack-manifest.json").write_text(json.dumps(slack_manifest(slug), indent=2) + "\n")
    print(f"Wrote {kit} and registered the instance `{slug}` (dashboard on port {port}).\n\nNext:")
    print(f"1. Fill {env}.")
    print(f"2. Create the Slack app from {inst / 'slack-manifest.json'} (api.slack.com/apps > From a manifest), "
          "then put its bot and app tokens in the .env.")
    print(f"3. Commit and push .factory/ to {base}.")
    print(f"4. Run `factory up {slug}`.")
    return 0


# ── setup ─────────────────────────────────────────────────────────────────────

def machine_values():
    def git_config(key):
        return run(["git", "config", "--global", key], check=False).stdout.strip()
    return {"home": str(Path.home()), "name": git_config("user.name"), "email": git_config("user.email")}


def skill_dirs(host_home):
    """The skill folders the agents read, mounted at their host paths so the symlinks inside them resolve."""
    dirs = [f"{host_home}/{root}/skills" for root in (".agents", ".claude", ".cursor", ".codex")]
    machine = home() / "machine.json"
    if machine.is_file():
        dirs += json.loads(machine.read_text()).get("extra_skill_dirs", [])
    return [d for d in dirs if Path(d).is_dir()]


def identity(values):
    return {"GIT_AUTHOR_NAME": values["name"], "GIT_AUTHOR_EMAIL": values["email"],
            "GIT_COMMITTER_NAME": values["name"], "GIT_COMMITTER_EMAIL": values["email"]}


def canvas_compose(values, skills):
    runtime = ENGINE_ROOT / "runtime"
    env = {**identity(values), "HOST_HOME": values["home"], "AGENT_CANVAS_ALLOW_LAN_SESSION_KEY": "true",
           # Agents run without a TTY, so corepack must fetch the pinned package manager without asking.
           "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
           # RTK looks for a Claude Code hook because ~/.claude exists; the agents here use the Cursor hook.
           "RTK_SUPPRESS_HOOK_WARNING": "1",
           # The Cursor CLI reads its own skills from the mounted folders instead of the managed catalog.
           "OH_ACP_SKILL_SOURCING": "native",
           # The Claude Code ACP adapter runs the Claude Code the image pins, not the older one it bundles.
           "CLAUDE_CODE_EXECUTABLE": "/opt/claude-code/bin/claude"}
    volumes = ["home:/home/openhands", f"{home()}/openhands-state:/home/openhands/.openhands",
               f"{home()}/projects:/projects", f"{runtime}/cursor-mcp.json:/opt/sandbox/cursor-mcp.json:ro",
               f"{runtime}/claude-mcp.json:/opt/sandbox/claude-mcp.json:ro"] + [f"{d}:{d}:ro" for d in skills]
    return {
        "name": CANVAS_PROJECT,
        "services": {"canvas": {
            "build": {"context": str(runtime), "args": {"HOST_HOME": values["home"]}},
            "image": IMAGE, "container_name": CANVAS_PROJECT, "restart": "unless-stopped",
            "ports": [f"127.0.0.1:{CANVAS_PORT}:8000"], "env_file": f"{home()}/.env",
            "environment": env, "volumes": volumes}},
        "volumes": {"home": {}},
    }


def setup():
    values = machine_values()
    if not values["name"] or not values["email"]:
        raise Fail(1, "Set `git config --global user.name` and `user.email` first: the agents commit as you.")
    state = home() / "openhands-state"
    for sub in ("bin", "agent-profiles"):
        for src in (ENGINE_ROOT / "runtime" / "openhands-state" / sub).iterdir():
            dest = state / sub / src.name
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
    (home() / "projects").mkdir(parents=True, exist_ok=True)
    env = home() / ".env"
    if not env.exists():
        env.write_text("GH_TOKEN=\n")
        env.chmod(0o600)
    path = home() / "compose.yaml"
    path.write_text(json.dumps(canvas_compose(values, skill_dirs(values["home"])), indent=2) + "\n")
    p = subprocess.run(["docker", "compose", "-p", CANVAS_PROJECT, "-f", str(path), "up", "-d", "--build", "canvas"])
    return p.returncode


# ── up, down, status ──────────────────────────────────────────────────────────

def host_clone(instance):
    return home() / "projects" / Path(instance["repo_path"]).relative_to("/projects")


def ensure_clone(instance):
    clone = host_clone(instance)
    if not clone.exists():
        url, _ = project_remote(Path(instance["project_path"]))
        run(["git", "clone", "-q", url, str(clone)])
    run(["git", "fetch", "-q", "origin"], cwd=clone, check=False)
    return clone


def show(clone, ref, path):
    p = run(["git", "show", f"{ref}:{path}"], cwd=clone, check=False)
    return p.stdout if p.returncode == 0 else None


def check_kit(clone, ref):
    """The project config at the ref, after its version and every prompt it needs are there."""
    text = show(clone, ref, ".factory/config.json")
    if text is None:
        raise Fail(1, f"`.factory/config.json` is not at `{ref}`. Commit and push `.factory/` there.")
    config = json.loads(text)
    if config.get("version") not in SUPPORTED_VERSIONS:
        raise Fail(1, f"`.factory/config.json` has version {config.get('version')}; this engine supports "
                      + ", ".join(map(str, SUPPORTED_VERSIONS)) + ".")
    needed = [n for n in PROMPTS if n != "demo.md" or (config.get("demo") or {}).get("enabled")]
    missing = [f".factory/prompts/{n}" for n in needed if show(clone, ref, f".factory/prompts/{n}") is None]
    if missing:
        raise Fail(1, *[f"`{m}` is not at `{ref}`." for m in missing], f"Commit and push `.factory/` to `{ref}`.")
    return config


def check_env(slug):
    env = read_env(slug)
    empty = [key for key in ENV_KEYS if not env.get(key)]
    if empty:
        raise Fail(1, *[f"{key} is empty in {instances_dir() / slug / '.env'}." for key in empty])
    return env


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def check_slack_app(slug, env):
    """Socket Mode may hand a payload to any open connection of an app, so two instances cannot share one."""
    mine = token_hash(env["SLACK_APP_TOKEN"])
    for other in instances():
        if other != slug and read_env(other).get("SLACK_APP_TOKEN") and \
                token_hash(read_env(other)["SLACK_APP_TOKEN"]) == mine:
            raise Fail(1, f"The instance `{other}` already uses this SLACK_APP_TOKEN. Give `{slug}` its own Slack app.")


def linear_team(api_key, team_key):
    """The status and label names of a Linear team."""
    body = json.dumps({"query": """query($key: String!) { teams(filter: { key: { eq: $key } }) {
        nodes { states { nodes { name } } labels { nodes { name } } } } }""", "variables": {"key": team_key}}).encode()
    req = urllib.request.Request("https://api.linear.app/graphql", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": api_key})
    with urllib.request.urlopen(req, timeout=30) as r:
        nodes = json.loads(r.read())["data"]["teams"]["nodes"]
    if not nodes:
        return None
    return {"states": {s["name"] for s in nodes[0]["states"]["nodes"]},
            "labels": {l["name"] for l in nodes[0]["labels"]["nodes"]}}


def check_linear(config, env):
    linear = config["linear"]
    team = linear_team(env["LINEAR_API_KEY"], linear["team_key"])
    if team is None:
        raise Fail(1, f"Linear has no team `{linear['team_key']}`.")
    missing = [f"status `{name}`" for name in linear["states"].values() if name not in team["states"]]
    missing += [f"label `{name}`" for name in (linear["label"], linear["needs_human_label"]) if name not in team["labels"]]
    if missing:
        raise Fail(1, *[f"Linear team `{linear['team_key']}` has no {m}." for m in missing])


def instance_compose(slug, instance):
    port = instance["dashboard"]["port"]
    inst = instances_dir() / slug
    values = machine_values()
    return {
        "name": f"factory-{slug}",
        "services": {"factory": {
            "image": IMAGE, "container_name": f"factory-{slug}", "restart": "unless-stopped",
            "ports": [f"127.0.0.1:{port}:{port}"], "env_file": str(inst / ".env"),
            "environment": {**identity(values), "HOST_HOME": values["home"], "CANVAS_URL": "http://canvas:8000",
                            "FACTORY_INSTANCE": "/instance/instance.json", "FACTORY_STATE": "/state/state.json",
                            "SLACK_PERSON_ID": instance.get("slack_person_id", ""), "DASHBOARD_PORT": str(port)},
            "command": ["python3", "/opt/factory/factory.py"],
            "volumes": [f"{ENGINE_ROOT / 'engine'}:/opt/factory:ro", f"{inst / 'state'}:/state",
                        f"{inst / 'instance.json'}:/instance/instance.json:ro", f"{home()}/projects:/projects",
                        f"{home()}/openhands-state:/canvas-state:ro"],
            "networks": ["canvas"]}},
        "networks": {"canvas": {"external": True, "name": f"{CANVAS_PROJECT}_default"}},
    }


def up(slug):
    instance = read_instance(slug)
    config = check_kit(ensure_clone(instance), instance["config_ref"])
    env = check_env(slug)
    check_slack_app(slug, env)
    check_linear(config, env)
    inst = instances_dir() / slug
    (inst / "state").mkdir(exist_ok=True)
    path = inst / "compose.yaml"
    path.write_text(json.dumps(instance_compose(slug, instance), indent=2) + "\n")
    run(["docker", "compose", "-p", f"factory-{slug}", "-f", str(path), "up", "-d"])
    print(f"factory-{slug} is up. Dashboard: http://127.0.0.1:{instance['dashboard']['port']}/")
    return 0


def down(slug):
    read_instance(slug)
    run(["docker", "stop", f"factory-{slug}"])
    print(f"factory-{slug} stopped. Its state stays in {instances_dir() / slug / 'state'}.")
    return 0


def container_state(slug):
    p = run(["docker", "inspect", "-f", "{{.State.Status}}", f"factory-{slug}"], check=False)
    return p.stdout.strip() if p.returncode == 0 else "absent"


def active_issues(slug):
    try:
        state = json.loads((instances_dir() / slug / "state" / "state.json").read_text())
        return str(sum(r.get("stage") not in IDLE_STAGES for r in state["issues"].values()))
    except (OSError, ValueError, KeyError, AttributeError):
        return "?"


def status():
    for slug in instances():
        port = read_instance(slug).get("dashboard", {}).get("port", "?")
        print(f"{slug}  {container_state(slug)}  port {port}  {active_issues(slug)} active")
    return 0


def main(argv=None, cwd=None):
    argv = sys.argv[1:] if argv is None else argv
    cwd = cwd or os.getcwd()
    command, rest = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if command == "init":
            return init(cwd)
        if command == "setup":
            return setup()
        if command == "status":
            return status()
        if command in ("up", "down"):
            slug = rest[0] if rest else slug_here(cwd)
            return up(slug) if command == "up" else down(slug)
    except Fail as e:
        print(str(e), file=sys.stderr)
        return e.code
    print(__doc__.strip(), file=sys.stderr)
    return 2
