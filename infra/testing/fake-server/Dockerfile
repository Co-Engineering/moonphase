# A stand-in for a "server the user added", used by the integration test.
#
# Runs sshd with password auth enabled and has the Docker CLI pointed at the
# host's daemon socket, so Moonphase's real SSH + Docker code paths run
# unmodified against it. Project containers become siblings on the host daemon,
# which is exactly the topology on a real box.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      openssh-server \
      docker.io \
      sudo \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /run/sshd \
    && useradd --create-home --shell /bin/bash deploy \
    && echo 'deploy:moonphase-test' | chpasswd \
    && echo 'deploy ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/deploy \
    && chmod 0440 /etc/sudoers.d/deploy

# Password auth is the point: the test exercises the bootstrap flow that logs
# in with a password once and installs a key.
RUN sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config \
    && ssh-keygen -A

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 22
CMD ["/usr/local/bin/entrypoint.sh"]
