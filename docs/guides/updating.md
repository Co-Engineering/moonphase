# Updating

**Settings → Instance → Version** says which build you are running and whether a
newer release exists.

The update button appears only when there is one. A screen that offers "Update"
to somebody already on the newest release teaches people to ignore it on the day
it matters.

## Updating by hand

Works everywhere, needs nothing installed, and is what the screen gives you to
copy:

```bash
cd moonphase
git pull
docker compose pull
docker compose up -d
```

Migrations travel inside the image alongside the code that expects them, so
those commands bring the schema with them.

!!! note "Why `git pull` is in there"
    `docker-compose.yml` is a file on your server, not something inside the
    image, so pulling a new image cannot change it. Most releases do not touch
    it and the two `docker compose` commands are enough — but when one adds a
    service, an image pull alone leaves it out, and the release quietly does
    less than it says.

    v0.6.0 was such a release: the service that repairs the permissions on the
    auth volume lives in that file, so an upgrade without this step brought the
    new code and none of the fix.

    Running the installer again does the same thing and keeps every secret. Your data, your servers and your
credentials are untouched — the containers are replaced, the volumes are not.

Sessions keep running throughout. They live in `tmux` inside project containers
on your *managed* servers, which this does not touch at all.

## Updating from the app

One click, once an updater is running beside Moonphase. It is opt-in, and worth
understanding before you turn it on.

### Why it is a separate container

Applying an update means talking to the host's Docker daemon. A container that
can do that can start a privileged container, mount the host filesystem, and own
the machine.

The API is the internet-facing component. Giving *it* that access would put the
whole host inside the blast radius of any flaw in the part of Moonphase most
exposed to the world. So it does not get it — a container that does nothing else
does, and only if you ask for it.

### What the updater can do

Exactly one thing. It publishes no port, so nothing outside the Docker network
can reach it, and it takes no commands: the API writes a random string into a
shared volume, and the updater's only question is whether that string changed.
Nothing a caller can say becomes something the host runs.

When it changes, it runs `docker compose pull` and `docker compose up -d` in the
project directory, which it has mounted read-only.

### Turning it on

```bash
cd moonphase
docker compose -f docker-compose.yml -f docker-compose.update.yml up -d
```

To keep it on for plain `docker compose` commands, add it to `COMPOSE_FILE` in
`.env`:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.public.yml:docker-compose.update.yml
```

The button appears in **Settings → Instance → Version** the next time you open
it.

### Turning it off

Remove the file from `COMPOSE_FILE`, then:

```bash
docker compose up -d --remove-orphans
```

The button disappears and the command comes back in its place.

## What you will see while it runs

The update replaces the API container, so the page loses contact for a few
seconds. That is expected, and the panel says so rather than showing an error.

The updater writes what it did into the shared volume before it restarts
anything, so the new API can report the outcome — including a failure, with the
reason. A pull that fails leaves everything running on the old version.

## Which build am I on?

| What it says | What it means |
| ------------ | ------------- |
| `v0.2.0` | A release. It can be compared against the latest one |
| `development build` | Built from `main` or from source. Not a release, so it is neither ahead of nor behind one |
| A commit, in grey | The exact build, which is what to quote in a bug report |

!!! question "Why does it not offer to update a development build?"
    Because `edge` is rebuilt on every commit and is usually *ahead* of the
    newest release. Offering "update to v0.2.0" to somebody running the tip
    would be offering a downgrade.

    Pin a release in `.env` to get update notices:

    ```bash
    MOONPHASE_VERSION=v0.2.0
    ```

!!! warning "Nothing rolls back automatically"
    An update that starts cleanly and misbehaves afterwards is yours to
    unpick — set `MOONPHASE_VERSION` to the release you were on and bring the
    stack up again. Migrations are additive and forward-only, so the schema
    tolerates running an older image against it; that is a deliberate property
    rather than luck, but it is not the same as a tested downgrade path.

    [Back up first](../getting-started/docker.md#backing-up) if the instance
    matters.
