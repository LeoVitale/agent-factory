# Agent Canvas all-in-one image + Cursor CLI, for running Cursor (ACP) agents
# inside a container instead of directly on the Mac.
FROM ghcr.io/openhands/agent-canvas:1.34.0

USER root

# The host home, so agent profiles written on the host (`$HOME/.local/bin/agent`) resolve here too.
ARG HOST_HOME

# Cursor CLI (official installer). Installed outside $HOME so the persisted home
# volume never shadows it.
RUN mkdir -p /opt/cursor \
 && HOME=/opt/cursor bash -c 'curl -fsSL https://cursor.com/install | bash' \
 && ln -sf /opt/cursor/.local/bin/agent /usr/local/bin/agent \
 && ln -sf /opt/cursor/.local/bin/cursor-agent /usr/local/bin/cursor-agent \
 && mkdir -p "${HOST_HOME}/.local/bin" \
 && ln -sf /usr/local/bin/agent "${HOST_HOME}/.local/bin/agent" \
 && ln -sf /usr/local/bin/cursor-agent "${HOST_HOME}/.local/bin/cursor-agent" \
 && chown -R openhands:openhands /opt/cursor

# Corepack runs the pnpm or yarn version each project pins in packageManager.
RUN corepack enable

# RTK rewrites the agents' shell commands so their output costs fewer tokens.
# Pinned release, checked against the SHA-256 in the release's checksums.txt.
ARG RTK_VERSION=v1.0.1
ARG RTK_SHA256=cd627c4091ad90b5e5aca83ca5af66ee4b8aa8537a736533fa90544f461f1c2b
RUN curl -fsSL -o /tmp/rtk.tar.gz \
      "https://github.com/rtk-ai/rtk/releases/download/${RTK_VERSION}/rtk-aarch64-unknown-linux-gnu.tar.gz" \
 && echo "${RTK_SHA256}  /tmp/rtk.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/rtk.tar.gz -C /usr/local/bin rtk \
 && rm /tmp/rtk.tar.gz \
 && rtk --version

# Claude Code for the Claude Code ACP profile. The ACP adapter bundles an older Claude Code that newer models
# refuse; CLAUDE_CODE_EXECUTABLE points it at this pinned release instead.
ARG CLAUDE_CODE_VERSION=2.1.308
RUN /acp-node/bin/npm install -g --prefix /opt/claude-code "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && /opt/claude-code/bin/claude --version

# Slack Socket Mode client for the factory orchestrator.
RUN python3 -m pip install --no-cache-dir slack_sdk==3.35.0

COPY --chmod=755 sandbox-entrypoint.sh /opt/sandbox/sandbox-entrypoint.sh

USER openhands
ENTRYPOINT ["tini", "--", "/opt/sandbox/sandbox-entrypoint.sh"]
