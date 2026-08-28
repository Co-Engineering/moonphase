# Docker access

Project containers run with no path to Docker by default — not even `sudo`
helps, since the restriction is enforced by the kernel against the
container's namespace, not by anything a user inside it controls. Sometimes
a project genuinely needs Docker: testing a `docker-compose` deployment
stack is the common case. This is how to turn that on, for the projects that
actually need it.

## Why not just `--privileged`

The obvious fix — start the container with `--privileged`, or mount the
host's `docker.sock` in — works, and is also close to handing the container
root on the server itself. A `docker-compose` stack an agent is testing
should not come with a side effect of "and now it can reach every other
project on this machine."

Moonphase uses [Sysbox](https://github.com/nestybox/sysbox) instead: a
container runtime that lets an *unprivileged* container run Docker and other
nested containers safely, by virtualizing user namespaces per container
rather than granting broad capabilities. A container running under Sysbox
can run its own Docker daemon; it still cannot reach the host's, or the host
itself.

## Two steps, both off by default

**1. Install Sysbox on the server.** This is a host-level dependency — a
package installed on the managed server itself, registered as a Docker
runtime alongside Docker. Turn it on when adding a server (**Install
Sysbox**, next to **Install Docker if missing**), or add it to a server you
already have from its detail page (**Install Sysbox**, next to
**Re-bootstrap**).

Requires:

- Ubuntu or Debian (amd64 or arm64)
- A kernel new enough for ID-mapped mounts (5.19+), or, on Ubuntu only, the
  older `shiftfs` module as a fallback
- Docker already installed on the same server

An incompatible or already-covered server says so — installing Sysbox never
fails deep into a broken state, and never affects whether the server itself
is considered online.

**2. Turn on Docker access for a project.** *(Coming in a follow-up — this
half is not available yet.)* Once it lands: a checkbox at project creation,
available only on a server that has Sysbox installed, starts that project's
container under `sysbox-runc` instead of the default runtime.

## What it does not do

Turning Docker access on for a project does **not** put Docker inside the
container image. Moonphase never bakes it in — once your project is running
under Sysbox, install Docker yourself the same way you'd install anything
else:

```
sudo apt-get install -y docker.io
```

Sysbox is what makes that command actually work as intended, rather than
silently failing to produce a usable daemon. It is not a trick that installs
Docker for you.

See [Security model](../concepts/security.md) for how this fits with
Moonphase's other isolation boundaries.
