# Moonphase project runtime — Claude Code.
#
# One of these runs per project, long-lived, holding a detached tmux session.
# It is a development environment, not a minimal service image: the agent needs
# a real toolchain to be useful, and shaving layers here buys nothing because
# the container never scales out.
#
# The base is a build argument so the same recipe produces every environment
# Moonphase offers. Node is installed from the official tarball rather than a
# distro package precisely so this stays base-agnostic — Debian and Ubuntu ship
# very different Node versions, and the harness should not care which host
# distribution the user picked.
ARG BASE_IMAGE=debian:bookworm-slim
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG NODE_VERSION=22.20.0

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
      xz-utils \
      build-essential \
      python3 \
      python3-pip \
      python3-venv \
      sudo \
      tini \
      locales \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# Debian and Ubuntu both ship fd as fdfind; the agent (and muscle memory)
# expects `fd`.
RUN ln -sf "$(command -v fdfind)" /usr/local/bin/fd

# Node from the official tarball, so the version is identical across bases.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch='x64' ;; \
      arm64) node_arch='arm64' ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
      -o /tmp/node.tar.xz; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \
    rm /tmp/node.tar.xz; \
    node --version; npm --version

ENV LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    TERM=xterm-256color

# GitHub CLI, so the agent can open PRs and read issues with the same token
# git uses. From GitHub's own apt repo, which serves both Debian and Ubuntu.
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
# volumes, and the API chowns them to this user after first start. Ubuntu
# images ship a uid 1000 user of their own, so 1001 keeps both bases identical.
RUN userdel -r ubuntu 2>/dev/null || true; \
    useradd --create-home --shell /bin/bash --uid 1001 dev \
    && echo 'dev ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/dev \
    && chmod 0440 /etc/sudoers.d/dev \
    && mkdir -p /workspace /home/dev/.claude /home/dev/.moonphase \
    && chown -R dev:dev /workspace /home/dev

COPY --chown=dev:dev tmux.conf /home/dev/.tmux.conf

# Lets the harness's own clipboard-image paste (it shells out to xclip; see
# xclip-shim.sh) work in a container that has neither an X server nor a real
# OS clipboard.
COPY xclip-shim.sh /usr/local/bin/xclip
RUN chmod +x /usr/local/bin/xclip

USER dev
WORKDIR /workspace

ENV npm_config_prefix=/home/dev/.npm-global
ENV PATH=/home/dev/.npm-global/bin:/home/dev/.local/bin:$PATH

# tini reaps the processes tmux leaves behind when panes exit. Without it, a
# long-lived container accumulates zombies.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["sleep", "infinity"]
