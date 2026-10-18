# Agent Canvas all-in-one image + Cursor CLI, for running Cursor (ACP) agents
# inside a container instead of directly on the Mac.
FROM ghcr.io/openhands/agent-canvas:1.27.1

USER root

# Cursor CLI (official installer). Installed outside $HOME so the persisted home
# volume never shadows it.
RUN mkdir -p /opt/cursor \
 && HOME=/opt/cursor bash -c 'curl -fsSL https://cursor.com/install | bash' \
 && ln -sf /opt/cursor/.local/bin/agent /usr/local/bin/agent \
 && ln -sf /opt/cursor/.local/bin/cursor-agent /usr/local/bin/cursor-agent \
 && mkdir -p /Users/vitale/.local/bin \
 && ln -sf /usr/local/bin/agent /Users/vitale/.local/bin/agent \
 && ln -sf /usr/local/bin/cursor-agent /Users/vitale/.local/bin/cursor-agent \
 && chown -R openhands:openhands /opt/cursor

# pnpm at the version project-hestia pins in packageManager.
RUN corepack enable && corepack prepare pnpm@11.17.0 --activate

# Slack Socket Mode client for the factory orchestrator.
RUN python3 -m pip install --no-cache-dir slack_sdk==3.35.0

COPY --chmod=755 sandbox-entrypoint.sh /opt/sandbox/sandbox-entrypoint.sh

USER openhands
ENTRYPOINT ["tini", "--", "/opt/sandbox/sandbox-entrypoint.sh"]
