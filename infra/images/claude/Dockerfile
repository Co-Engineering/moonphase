# Moonphase project runtime — Claude Code.
#
# One of these runs per project, long-lived, holding a detached tmux session.
# It is a development environment, not a minimal service image: the agent needs
# a real toolchain to be useful, and shaving layers here buys nothing because
# the container never scales out.
FROM node:22-bookworm-slim

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      gnupg \
      openssh-client \
      less \
      jq \
      ripgrep \
      fd-find \
      tmux \
      socat \
      iproute2 \
      procps \
      unzip \
      build-essential \
      python3 \
      python3-pip \
      python3-venv \
      sudo \
      tini \
      locales \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# Debian ships fd as fdfind; the agent (and muscle memory) expects `fd`.
RUN ln -s "$(command -v fdfind)" /usr/local/bin/fd

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    TERM=xterm-256color

# GitHub CLI, so the agent can open PRs and read issues with the same token
# git uses. Installed from GitHub's own apt repo rather than Debian's, which
# does not carry it.
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# The harness.
RUN npm install -g @anthropic-ai/claude-code@latest \
    && npm cache clean --force

# uv, for Python projects the agent may want to manage.
RUN curl -LsSf https://astral.sh/uv/install.sh | \
      env UV_INSTALL_DIR=/usr/local/bin sh

# Non-root by default. The uid matters: /workspace and /home/dev are named
# volumes, and the API chowns them to this user after first start.
RUN useradd --create-home --shell /bin/bash --uid 1001 dev \
    && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev \
    && mkdir -p /workspace /home/dev/.claude /home/dev/.moonphase \
    && chown -R dev:dev /workspace /home/dev

COPY --chown=dev:dev tmux.conf /home/dev/.tmux.conf

USER dev
WORKDIR /workspace

ENV npm_config_prefix=/home/dev/.npm-global
ENV PATH=/home/dev/.npm-global/bin:/home/dev/.local/bin:$PATH

# tini reaps the processes tmux leaves behind when panes exit. Without it, a
# long-lived container accumulates zombies.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["sleep", "infinity"]
