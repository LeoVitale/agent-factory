"""The `factory` command: init, setup, up, down, status.

  python3 -m unittest tests.test_cli
"""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT, project_config, sh, write_kit

sys.path.insert(0, str(ROOT / "cli"))
import factory_cli as cli  # noqa: E402

PROMPTS = ["_result.md", "answer.md", "plan.md", "build.md", "verify.md", "judge.md", "fix.md", "demo.md"]


def snapshot(*roots):
    """Every file under the roots with its content and mtime, .git excluded."""
    out = {}
    for root in roots:
        for p in Path(root).rglob("*"):
            if p.is_file() and ".git" not in p.relative_to(root).parts:
                out[str(p)] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


class CliCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="factory-cli-"))
        self.home = self.tmp / "factory-home"
        self.home.mkdir()
        os.environ["FACTORY_HOME"] = str(self.home)
        self.real_run = cli.run

    def tearDown(self):
        cli.run = self.real_run

    def repo(self, name="widget", origin="git@github.com:acme/{name}.git", head="trunk"):
        repo = self.tmp / name
        repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=repo)
        (repo / "README.md").write_text("hi\n")
        sh("git", "add", ".", cwd=repo)
        sh("git", "commit", "-qm", "init", cwd=repo)
        if origin:
            sh("git", "remote", "add", "origin", origin.format(name=name), cwd=repo)
            sh("git", "symbolic-ref", f"refs/remotes/origin/HEAD", f"refs/remotes/origin/{head}", cwd=repo)
        return repo

    def cli(self, *argv, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), cwd=str(cwd or self.tmp))
        return code, out.getvalue(), err.getvalue()

    def instance(self, slug):
        return json.loads((self.home / "instances" / slug / "instance.json").read_text())


class InitTest(CliCase):

    def test_writes_kit_and_registers_instance(self):
        repo = self.repo()
        code, _, _ = self.cli("init", cwd=repo)
        self.assertEqual(code, 0)
        config = json.loads((repo / ".factory/config.json").read_text())
        self.assertEqual((config["version"], config["github_repo"], config["base_branch"]), (1, "acme/widget", "trunk"))
        self.assertEqual(sorted(p.name for p in (repo / ".factory/prompts").iterdir()), sorted(PROMPTS))
        self.assertEqual(self.instance("widget")["config_ref"], "origin/trunk")

    def test_env_has_four_empty_keys_mode_0600(self):
        repo = self.repo()
        self.cli("init", cwd=repo)
        env = self.home / "instances/widget/.env"
        self.assertEqual(env.read_text().splitlines(),
                         ["LINEAR_API_KEY=", "GH_TOKEN=", "SLACK_BOT_TOKEN=", "SLACK_APP_TOKEN="])
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)

    def test_slack_manifest_is_socket_mode(self):
        repo = self.repo()
        self.cli("init", cwd=repo)
        manifest = json.loads((self.home / "instances/widget/slack-manifest.json").read_text())
        self.assertIs(manifest["settings"]["socket_mode_enabled"], True)
        self.assertEqual(manifest["settings"]["event_subscriptions"]["bot_events"], ["message.channels"])
        for scope in ("chat:write", "channels:manage", "channels:read", "channels:join", "channels:history",
                      "reactions:write"):
            self.assertIn(scope, manifest["oauth_config"]["scopes"]["bot"])

    def test_port_is_lowest_free_from_8202(self):
        self.cli("init", cwd=self.repo("first"))
        self.assertEqual(self.instance("first")["dashboard"]["port"], 8202)
        for slug, port in (("taken-a", 8202), ("taken-b", 8204)):
            (self.home / "instances" / slug).mkdir(parents=True)
            (self.home / "instances" / slug / "instance.json").write_text(json.dumps({"dashboard": {"port": port}}))
        (self.home / "instances/first/instance.json").unlink()
        (self.home / "instances/first").rename(self.tmp / "gone")
        self.cli("init", cwd=self.repo("second"))
        self.assertEqual(self.instance("second")["dashboard"]["port"], 8203)

    def test_not_a_git_repository_exits_1_and_writes_nothing(self):
        folder = self.tmp / "plain"
        folder.mkdir()
        before = snapshot(folder, self.home)
        code, _, err = self.cli("init", cwd=folder)
        self.assertEqual(code, 1)
        self.assertIn("not a git repository", err)
        self.assertEqual(snapshot(folder, self.home), before)

    def test_non_github_origin_exits_1_and_writes_nothing(self):
        repo = self.repo(origin="git@gitlab.com:acme/{name}.git")
        before = snapshot(repo, self.home)
        code, _, err = self.cli("init", cwd=repo)
        self.assertEqual(code, 1)
        self.assertIn("not on github.com", err)
        self.assertEqual(snapshot(repo, self.home), before)

    def test_existing_kit_or_instance_exits_2(self):
        repo = self.repo()
        (repo / ".factory").mkdir()
        (repo / ".factory/keep.txt").write_text("mine")
        before = snapshot(repo, self.home)
        self.assertEqual(self.cli("init", cwd=repo)[0], 2)
        self.assertEqual(snapshot(repo, self.home), before)

        other = self.repo("other")
        (self.home / "instances/other").mkdir(parents=True)
        (self.home / "instances/other/instance.json").write_text("{}")
        before = snapshot(other, self.home)
        self.assertEqual(self.cli("init", cwd=other)[0], 2)
        self.assertEqual(snapshot(other, self.home), before)

    def test_init_runs_no_remote_or_writing_git_command(self):
        repo = self.repo()
        head = sh("git", "rev-parse", "HEAD", cwd=repo)
        calls = []
        cli.run = lambda args, **k: calls.append(args) or self.real_run(args, **k)
        self.assertEqual(self.cli("init", cwd=repo)[0], 0)
        self.assertTrue(calls)
        for args in calls:
            self.assertEqual(args[0], "git", args)
            self.assertIn(args[1], ("rev-parse", "remote", "symbolic-ref"), args)
            if args[1] == "remote":
                self.assertEqual(args[2], "get-url", args)
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=repo), head)
        self.assertEqual(sh("git", "status", "--porcelain", cwd=repo), "?? .factory/")

    def test_prints_four_next_steps_in_order(self):
        repo = self.repo()
        _, out, _ = self.cli("init", cwd=repo)
        steps = [line for line in out.splitlines() if line[:2] in ("1.", "2.", "3.", "4.")]
        self.assertEqual([s[:2] for s in steps], ["1.", "2.", "3.", "4."])
        self.assertIn(".env", steps[0])
        self.assertIn("slack-manifest.json", steps[1])
        self.assertIn("push .factory/", steps[2])
        self.assertIn("factory up widget", steps[3])


class UpCase(CliCase):
    """An initialised project whose kit is pushed to a local origin, with a clone in FACTORY_HOME/projects."""

    def setUp(self):
        super().setUp()
        self.origin = self.tmp / "origin.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.project = self.repo(origin=str(self.origin), head="main")
        sh("git", "push", "-q", "origin", "main", cwd=self.project)
        sh("git", "remote", "set-url", "origin", "git@github.com:acme/widget.git", cwd=self.project)
        sh("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=self.project)
        self.assertEqual(self.cli("init", cwd=self.project)[0], 0)
        sh("git", "remote", "set-url", "origin", str(self.origin), cwd=self.project)
        sh("git", "add", ".factory", cwd=self.project)
        sh("git", "commit", "-qm", "kit", cwd=self.project)
        self.config = project_config({"linear": {"team_key": "ENG"}})
        write_kit(self.project, self.config)
        sh("git", "push", "-q", "origin", "main", cwd=self.project)
        sh("git", "clone", "-q", str(self.origin), str(self.home / "projects/widget"))
        self.fill_env()
        self.docker = []
        self.team = {"states": {"Ready for agent", "In Progress", "In review"},
                     "labels": {"factory", "factory:needs-human"}}
        cli.linear_team = lambda key, team_key: self.team

        def run(args, **k):
            if args[0] == "docker":
                self.docker.append(args)
                return subprocess.CompletedProcess(args, 0, "", "")
            return self.real_run(args, **k)
        cli.run = run

    def fill_env(self, **values):
        env = {"LINEAR_API_KEY": "lin-key", "GH_TOKEN": "gh-token", "SLACK_BOT_TOKEN": "xoxb-1",
               "SLACK_APP_TOKEN": "xapp-widget-secret", **values}
        (self.home / "instances/widget/.env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))

    def push_kit_change(self, change):
        change(self.project / ".factory")
        sh("git", "add", "-A", ".factory", cwd=self.project)
        sh("git", "commit", "-qm", "change kit", cwd=self.project)
        sh("git", "push", "-q", "origin", "main", cwd=self.project)


class UpTest(UpCase):

    def test_compose_names_container_mounts_and_port(self):
        code, _, err = self.cli("up", "widget")
        self.assertEqual(code, 0, err)
        compose = json.loads((self.home / "instances/widget/compose.yaml").read_text())
        service = compose["services"]["factory"]
        self.assertEqual(service["container_name"], "factory-widget")
        self.assertIn(f"{ROOT / 'engine'}:/opt/factory:ro", service["volumes"])
        self.assertIn(f"{self.home / 'instances/widget/state'}:/state", service["volumes"])
        self.assertEqual(service["ports"], ["127.0.0.1:8202:8202"])
        self.assertEqual(self.docker[-1][:4], ["docker", "compose", "-p", "factory-widget"])

    def test_unknown_config_version_exits_1(self):
        self.push_kit_change(lambda kit: (kit / "config.json").write_text(
            json.dumps({**json.loads((kit / "config.json").read_text()), "version": 2})))
        code, _, err = self.cli("up", "widget")
        self.assertEqual(code, 1)
        self.assertIn("version 2", err)
        self.assertIn("supports 1", err)
        self.assertEqual(self.docker, [])

    def test_empty_env_keys_exit_1_named_without_values(self):
        self.fill_env(GH_TOKEN="", SLACK_APP_TOKEN="")
        code, out, err = self.cli("up", "widget")
        self.assertEqual(code, 1)
        self.assertIn("GH_TOKEN is empty", err)
        self.assertIn("SLACK_APP_TOKEN is empty", err)
        for value in ("lin-key", "xoxb-1"):
            self.assertNotIn(value, out + err)
        self.assertEqual(self.docker, [])

    def test_missing_linear_status_and_label_exit_1(self):
        self.team = {"states": {"In Progress", "In review"}, "labels": {"factory"}}
        code, _, err = self.cli("up", "widget")
        self.assertEqual(code, 1)
        self.assertIn("status `Ready for agent`", err)
        self.assertIn("label `factory:needs-human`", err)
        self.assertEqual(self.docker, [])

    def test_missing_prompt_at_ref_exits_1(self):
        self.push_kit_change(lambda kit: (kit / "prompts/judge.md").unlink())
        code, _, err = self.cli("up", "widget")
        self.assertEqual(code, 1)
        self.assertIn(".factory/prompts/judge.md", err)
        self.assertIn("origin/main", err)
        self.assertIn("push", err)
        self.assertEqual(self.docker, [])

    def test_missing_config_at_ref_exits_1(self):
        self.push_kit_change(lambda kit: (kit / "config.json").unlink())
        code, _, err = self.cli("up", "widget")
        self.assertEqual(code, 1)
        self.assertIn(".factory/config.json", err)
        self.assertIn("origin/main", err)
        self.assertEqual(self.docker, [])

    def test_shared_slack_app_token_exits_1(self):
        other = self.home / "instances/gadget"
        other.mkdir()
        (other / "instance.json").write_text(json.dumps({"dashboard": {"port": 8203}}))
        (other / ".env").write_text("SLACK_APP_TOKEN=xapp-widget-secret\n")
        code, out, err = self.cli("up", "widget")
        self.assertEqual(code, 1)
        self.assertIn("`gadget`", err)
        self.assertNotIn("xapp-widget-secret", out + err)
        self.assertEqual(self.docker, [])


class DownTest(UpCase):

    def test_stops_only_its_container_and_keeps_state(self):
        state = self.home / "instances/widget/state"
        state.mkdir()
        (state / "state.json").write_text('{"issues": {}}')
        before = snapshot(self.home / "instances/widget")
        code, _, _ = self.cli("down", "widget")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker, [["docker", "stop", "factory-widget"]])
        self.assertEqual(snapshot(self.home / "instances/widget"), before)


class StatusTest(CliCase):

    def test_one_line_per_instance(self):
        for slug, port, state in (("alpha", 8201, {"issues": {"A-1": {"stage": "build"}, "A-2": {"stage": "verify"},
                                                              "A-3": {"stage": "merged"}}}),
                                  ("beta", 8202, None)):
            inst = self.home / "instances" / slug
            (inst / "state").mkdir(parents=True)
            (inst / "instance.json").write_text(json.dumps({"dashboard": {"port": port}}))
            (inst / "state/state.json").write_text(json.dumps(state) if state else "not json")
        cli.run = lambda args, **k: subprocess.CompletedProcess(
            args, 0, "running\n" if args[-1] == "factory-alpha" else "exited\n", "")
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["alpha  running  port 8201  2 active", "beta  exited  port 8202  ? active"])


class SetupTest(CliCase):

    def test_machine_values_come_from_home_and_git_config(self):
        fake_home = self.tmp / "someone"
        (fake_home / ".claude/skills").mkdir(parents=True)
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Sam Example\n\temail = sam@example.com\n")
        real_home = str(Path.home())
        saved = {k: os.environ.get(k) for k in ("HOME", "GIT_CONFIG_GLOBAL")}
        os.environ.update(HOME=str(fake_home), GIT_CONFIG_GLOBAL=str(gitconfig))
        try:
            values = cli.machine_values()
            compose = cli.canvas_compose(values, cli.skill_dirs(values["home"]))
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        canvas = compose["services"]["canvas"]
        self.assertEqual(canvas["build"]["args"]["HOST_HOME"], str(fake_home))
        self.assertEqual(canvas["environment"]["GIT_AUTHOR_NAME"], "Sam Example")
        self.assertEqual(canvas["environment"]["GIT_AUTHOR_EMAIL"], "sam@example.com")
        self.assertIn(f"{fake_home}/.claude/skills:{fake_home}/.claude/skills:ro", canvas["volumes"])
        # Host-side values only: container paths such as `home:/home/openhands` are fixed by the image, and the
        # engine clone may sit anywhere. Nothing else may name the real home of whoever runs the test.
        host_side = [canvas["build"], canvas["environment"], canvas["env_file"],
                     [v.split(":")[0] for v in canvas["volumes"]]]
        self.assertNotIn(real_home, json.dumps(host_side).replace(str(ROOT), "<engine>"))


class CoexistTest(CliCase):

    def test_two_instances_isolated_and_share_canvas(self):
        for name in ("widget", "gadget"):
            self.assertEqual(self.cli("init", cwd=self.repo(name))[0], 0)
        a, b = self.instance("widget"), self.instance("gadget")
        ca = cli.instance_compose("widget", a)["services"]["factory"]
        cb = cli.instance_compose("gadget", b)["services"]["factory"]
        state = lambda c: next(v for v in c["volumes"] if v.endswith(":/state"))
        self.assertNotEqual(state(ca), state(cb))
        self.assertNotEqual(a["worktrees_path"], b["worktrees_path"])
        self.assertNotEqual(ca["ports"], cb["ports"])
        self.assertNotEqual(ca["env_file"], cb["env_file"])
        self.assertEqual(ca["environment"]["CANVAS_URL"], "http://canvas:8000")
        self.assertEqual(cb["environment"]["CANVAS_URL"], "http://canvas:8000")
        network = cli.instance_compose("widget", a)["networks"]["canvas"]
        self.assertEqual(network, {"external": True, "name": "agent-sandbox_default"})
        canvas = cli.canvas_compose({"home": "/h", "name": "n", "email": "e"}, [])
        self.assertEqual(canvas["services"]["canvas"]["ports"], ["127.0.0.1:8200:8000"])


if __name__ == "__main__":
    unittest.main()
