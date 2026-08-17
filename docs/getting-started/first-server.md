# Your first server

A server is any machine you can reach over SSH. Moonphase connects to it, makes sure
Docker is there, and from then on runs every project on it inside its own container.

## Add it

Press **+** beside *Servers* in the sidebar, then choose how Moonphase should
authenticate. The three modes trade convenience against how much of your key material the
backend ends up holding.

=== "Password (once)"

    **Recommended.** Moonphase logs in with the password, generates its own ed25519 key,
    installs it, verifies that key-only login works, and then destroys the password.

    The password is never written to the database. If key installation fails, the whole
    thing is rolled back rather than left half-done.

=== "Moonphase-managed key"

    Moonphase generates a keypair and shows you the public half to install yourself.
    Nothing of yours is ever sent to the backend.

    Slower to set up, and the right choice if you would rather not type a server password
    into anything.

=== "Paste my private key"

    Fastest. Also the only mode where Moonphase holds a credential that probably opens
    more than this one machine — an existing key is rarely scoped to a single host.

    Stored encrypted with `MOONPHASE_SECRET_KEY`, but consider whether a dedicated key
    would be better.

## What happens next

Moonphase connects, pins the host key, and probes for Docker.

```text
connecting → pinning host key → probing docker → online
```

If Docker is missing and the user has passwordless sudo, Moonphase offers to install it.
Once the card reads **online** it shows the Docker version and the pinned host key
fingerprint.

!!! info "Host keys are pinned on first use"
    The first fingerprint Moonphase sees is stored, and a later mismatch is a hard
    failure rather than a prompt — the point of pinning is that nobody gets to click
    through it. Set `MOONPHASE_SSH_TRUST_ON_FIRST_USE=false` to require the fingerprint
    up front instead.

## Requirements on the server

Very little, on purpose:

- Inbound SSH, reachable from the backend and nowhere else in particular
- Docker, or a user who can install it
- Enough disk for the images and your project volumes

Moonphase never opens inbound ports on your server for previews. Everything is
[tunnelled back through the SSH connection it already has](../guides/previews.md).

## If it goes wrong

**Stuck in `error` right after a Docker install.** Group membership only applies to new
sessions, so the connection that installed Docker cannot use it. Press **Test** to
reconnect and re-probe.

**Host key mismatch.** Either the machine was rebuilt, or something is wrong. Moonphase
will not connect until you remove the server and add it again, which is deliberate.

More in [troubleshooting](../reference/troubleshooting.md).

## Next

[Create your first project →](first-project.md){ .md-button .md-button--primary }
