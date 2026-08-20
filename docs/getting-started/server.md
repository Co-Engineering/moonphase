# Installing on a server

One command on your own machine. It asks where the server is and how to reach
it, then does the rest over SSH.

```console
$ curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install-server.sh -o install-server.sh
$ sh install-server.sh
```

It asks for the address, a username, and a key or password. When it finishes it
tells you where to open it. The first account you make there is yours, and the
domain and HTTPS are set on the same screen.

!!! info "Why not pipe it straight into `sh`?"
    Because it has questions, and piped from `curl` the script *is* the shell's
    input — an answer would be read from the middle of the script rather than
    from you. It reads the terminal directly to avoid that, but downloading it
    first is the honest way round, and it means you can read it before you run
    it.

    Already know the answers? Then it needs no terminal at all:

    ```console
    $ MOONPHASE_HOST=203.0.113.10 MOONPHASE_SSH_USER=root \
        MOONPHASE_SSH_KEY=~/.ssh/id_ed25519 sh install-server.sh
    ```

## What you need

| On your machine | On the server |
| --------------- | ------------- |
| `ssh` | Nothing |
| The address, a username, and a key or password | Linux, and an account that is root or can `sudo` |

Docker is installed if the machine has none. So are `curl`, `openssl` and `git`
if the image was minimal enough to omit them — apt, dnf, yum, pacman and apk are
all handled.

!!! question "Password or key?"
    Either. Leave the key blank and `ssh` asks for the password, once — the
    script never sees it and has nowhere to keep it. There is deliberately no
    way to pass a password as an argument, because that puts it in your shell
    history and in `ps` output for everyone else on the machine.

## What it does

1. Connects, and stops with a readable reason if it cannot
2. Checks the machine is Linux, and that the account can install things
3. Installs anything missing, Docker included
4. Fetches [the server installer](docker.md) and runs it
5. Generates every secret — you are never asked to edit a file
6. Takes ports 80 and 443 if they are free, so a domain and HTTPS work later
7. Tells you where to open it

## Running it again

Safe, and it is how you upgrade. Secrets already in place are kept —
`MOONPHASE_SECRET_KEY` in particular, since regenerating it would make every
stored SSH credential unreadable — and an install already holding ports 80 and
443 keeps them.

```console
$ sh install-server.sh
```

## If it cannot connect

The reasons, in the order they happen:

| What you see | Usually |
| ------------ | ------- |
| `Permission denied (publickey)` | The server does not know that key. Try a password, or add the key to `~/.ssh/authorized_keys` there |
| `Connection timed out` | Wrong address, or port 22 is closed in the provider's firewall |
| `Connection refused` | The machine is up and SSH is not running on that port |
| Installs, but the address does not open | Port 80 is closed. On a cloud VM that is a security group, not the machine |

That last one is the common one, and it is worth being specific: on AWS it is
the instance's security group, on Azure a network security group, on Google
Cloud a firewall rule, and on Hetzner or DigitalOcean a firewall attached to the
server. The machine itself is usually not the thing blocking you.

## Then what

- [Point a domain at it](../guides/dns.md) — one DNS record, and HTTPS follows
  on its own
- [Install the app](app.md) on your computer and phone
- [Add your first server](first-server.md) to run projects on

## Doing it by hand

Nothing here is magic — it is one SSH connection and the ordinary installer. To
run that yourself, on the server:

```console
$ curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install.sh | sh
```

[Installing with Docker](docker.md) covers what that puts on the machine, how to
manage it afterwards, and how to build from source instead.
