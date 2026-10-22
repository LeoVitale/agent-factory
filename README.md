# agent-factory

An agentic factory that takes a Linear issue from **Ready for agent** to a reviewed pull request. It runs one OpenHands (Cursor ACP) conversation per station (Plan, Build, Verify, Judge, Fix and Demo), checks each station's artifacts before the next one starts, and talks to you in one Slack thread per issue.

The engine is installed once per machine. A project receives only `.factory/`: its config, the station prompts and, optionally, a demo kit.

## Install on a machine

```bash
git clone git@github.com:LeoVitale/agent-factory.git ~/projects/vita/agent-factory
~/projects/vita/agent-factory/install.sh
factory setup
```

`setup` builds the image with your `$HOME` and your global git identity, then starts the shared canvas on `127.0.0.1:8200`. Its state lives in `FACTORY_HOME`, which defaults to `~/agent-sandbox`. Put the agents' `GH_TOKEN` in `$FACTORY_HOME/.env`. Extra skill folders go in `$FACTORY_HOME/machine.json` as `{"extra_skill_dirs": ["/abs/path"]}`.

## Install in a project

In the project's folder:

1. `factory init` writes `.factory/` and registers the instance in `$FACTORY_HOME/instances/<repo name>/`.
2. Fill `instances/<slug>/.env`: `LINEAR_API_KEY`, `GH_TOKEN`, `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.
3. Create the project's own Slack app from `instances/<slug>/slack-manifest.json`. Socket Mode can deliver a payload to any connection of an app, so two projects cannot share one.
4. Edit `.factory/config.json` and the prompts, then commit and push `.factory/` to the base branch.
5. Run `factory up`.

`init` does not commit, push or create anything outside the two folders it writes. `up` refuses to start, and names what is missing, when any of these is true:

- a `.env` key is empty;
- a Linear status or label is missing;
- `.factory/` is missing at `config_ref`;
- the config version is unsupported;
- another instance holds the same Slack app token.

## Commands

| Command | What it does |
| --- | --- |
| `factory setup` | Builds the image and starts the canvas. |
| `factory init` | Writes `.factory/` and registers the instance. Exits 1 outside a GitHub repository and 2 when either folder already exists. |
| `factory up [<slug>]` | Checks the instance, then starts its container `factory-<slug>`. |
| `factory down [<slug>]` | Stops that container. The instance's state stays. |
| `factory status` | Prints one line per instance: its container state, port and active issues. |

## Where the settings live

| File | Holds | Read |
| --- | --- | --- |
| `.factory/config.json` in the project | Linear team, statuses and labels; repository and base branch; artifact paths; schema guard; CI ignore rules; Slack channel naming; limits; demo variables | from the clone at `config_ref`, so a change applies once it is pushed there |
| `.factory/prompts/*.md` | The prompt of each station | the same way, every time a station starts |
| `instances/<slug>/instance.json` | Clone and worktree paths, `config_ref`, dashboard port, OpenHands profiles, your Slack user id, mention channels | when the instance starts |
| `instances/<slug>/.env` | The four secrets | when the instance starts |

## Tests

```bash
./scripts/test.sh
```

The tests run offline and need only `python3` and `git`.

`docs/history/` keeps the notes from when the factory was built inside its first project.
