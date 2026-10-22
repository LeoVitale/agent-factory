"""A throwaway project with a `.factory/` kit committed, and an instance file pointing at it."""
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
TEMPLATES = ROOT / "templates" / "factory"
FIXTURE_CONFIG = Path(__file__).resolve().parent / "fixtures" / "config.json"
IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
os.environ.update(IDENTITY)
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def project_config(overrides=None):
    return merge(json.loads(FIXTURE_CONFIG.read_text()), overrides)


def write_kit(repo, config, commit=True):
    kit = Path(repo) / ".factory"
    (kit / "prompts").mkdir(parents=True, exist_ok=True)
    (kit / "config.json").write_text(json.dumps(config, indent=2))
    for prompt in (TEMPLATES / "prompts").iterdir():
        shutil.copyfile(prompt, kit / "prompts" / prompt.name)
    if commit:
        sh("git", "add", ".factory", cwd=repo)
        sh("git", "commit", "-qm", "factory kit", cwd=repo)


def project(prefix, repo=None, config=None, kit=True, instance=None):
    """Set FACTORY_INSTANCE and FACTORY_STATE for a project whose kit sits at HEAD. Returns (root, repo)."""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    if repo is None:
        repo = root / "clone"
        repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=repo)
        (repo / "README.md").write_text("hi\n")
        sh("git", "add", ".", cwd=repo)
        sh("git", "commit", "-qm", "init", cwd=repo)
    if kit:
        write_kit(repo, project_config(config))
    data = merge({"repo_path": str(repo), "worktrees_path": str(root / "worktrees"), "config_ref": "HEAD",
                  "canvas_public_url": "http://localhost:8200/canvas", "agent_profile_id": "profile",
                  "station_profiles": {}, "demo": {"evidence_path": str(root / "evidence"),
                                                     "tools_path": str(root / "tools")}}, instance)
    (root / "instance.json").write_text(json.dumps(data))
    os.environ.update(FACTORY_INSTANCE=str(root / "instance.json"), FACTORY_STATE=str(root / "state" / "state.json"),
                      LINEAR_API_KEY="x")
    return root, Path(repo)


def load_engine():
    """A fresh copy of factory.py, reading the instance the last `project()` call set."""
    spec = importlib.util.spec_from_file_location(f"factory_{os.urandom(4).hex()}", ENGINE / "factory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
