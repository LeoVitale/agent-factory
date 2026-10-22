"""Where an instance's settings come from.

Two sources, and nothing else: the machine's `instance.json` (paths, ports, OpenHands profiles, the Slack person)
and the project's `.factory/` as committed at the instance's `config_ref`. The engine has no project defaults, so
a missing `.factory/config.json` is an error, never a fallback.
"""
import json
import os
import subprocess
from pathlib import Path

SUPPORTED_VERSIONS = (1,)

# Timings belong to the engine; every project gets the same ones.
ENGINE_DEFAULTS = {
    "poll_seconds": 120,
    "active_poll_seconds": 15,
    "no_checks_timeout_seconds": 900,
    "worktree_cleanup_hours": 24,
    "mentions": {"channels": [], "workspace": "/projects/slack-mentions", "debounce_seconds": 15, "poll_seconds": 10},
    "dashboard": {"enabled": True, "port": 8201},
}


class ConfigError(Exception):
    """The instance or its project kit cannot be read."""


def instance_path():
    return Path(os.environ.get("FACTORY_INSTANCE", "/instance/instance.json"))


def show(repo, ref, path):
    """The file at `ref` in the clone, or None when it is not there."""
    p = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def project_config(instance):
    repo, ref = instance["repo_path"], instance["config_ref"]
    text = show(repo, ref, ".factory/config.json")
    if text is None:
        raise ConfigError(f"`.factory/config.json` is not at `{ref}` in `{repo}`. Push `.factory/` there.")
    project = json.loads(text)
    if project.get("version") not in SUPPORTED_VERSIONS:
        raise ConfigError(f"`.factory/config.json` has version {project.get('version')}; this engine supports "
                          + ", ".join(map(str, SUPPORTED_VERSIONS)) + ".")
    return project


def flatten(project):
    """The project's nested config as the flat keys the engine reads."""
    linear, limits = project["linear"], project["limits"]
    return {
        "team_key": linear["team_key"],
        "states": linear["states"],
        "label": linear["label"],
        "needs_human_label": linear["needs_human_label"],
        "github_repo": project["github_repo"],
        "base_branch": project["base_branch"],
        "artifacts": project["artifacts"],
        "schema_guard": project.get("schema_guard") or {"paths": [], "command": ""},
        "ci_ignore": (project.get("ci") or {}).get("ignore", []),
        "slack": project["slack"],
        "max_fix_rounds": limits["max_fix_rounds"],
        "max_base_syncs": limits["max_base_syncs"],
        "max_active": limits["max_active"],
        "demo": project.get("demo") or {"enabled": False},
    }


def load():
    path = instance_path()
    if not path.is_file():
        raise ConfigError(f"No instance file at `{path}`.")
    instance = json.loads(path.read_text())
    project = flatten(project_config(instance))
    cfg = {**ENGINE_DEFAULTS, **project, **instance}
    cfg["mentions"] = {**ENGINE_DEFAULTS["mentions"], **instance.get("mentions", {})}
    cfg["dashboard"] = {**ENGINE_DEFAULTS["dashboard"], **instance.get("dashboard", {})}
    # The project decides what the demo shows; the machine decides where its files go.
    cfg["demo"] = {**project["demo"], **instance.get("demo", {})}
    return cfg


def prompt(cfg, name):
    text = show(cfg["repo_path"], cfg["config_ref"], f".factory/prompts/{name}")
    if text is None:
        raise ConfigError(f"`.factory/prompts/{name}` is not at `{cfg['config_ref']}`. Push `.factory/` there.")
    return text


def demo_kit(cfg):
    """The project's demo kit, or None when it has none."""
    return show(cfg["repo_path"], cfg["config_ref"], ".factory/demo-kit.mjs")
