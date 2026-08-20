# Previews

Seeing the thing you made, without it being subtly broken.

## Nothing is declared

Moonphase does not ask which ports your project uses. It looks. Whatever the container is
listening on appears within a few seconds — a dev server started a minute ago, or one
restarted onto a different port, simply shows up.

Each port is then asked what it serves, with an actual HTTP request:

| Kind      | Meaning                                    |
| --------- | ------------------------------------------ |
| `page`    | Serves HTML — this is what "open the app" means |
| `api`     | Answers JSON                               |
| `unknown` | Did not answer an HTTP request at all      |

The page's own `<title>` becomes its name, which beats a port number by a distance:

```text
Your app       port 5173
Todo           port 5174
Data service   port 8000
```

!!! info "Why a page that names itself wins"
    When two ports both serve HTML, the one that gave itself a title is almost always the
    app and the untitled one is almost always a placeholder. Ranking by port number
    instead picks whichever sorts lower, which is a coin flip — and landing on a
    placeholder looks exactly like the app is broken.

    A directory listing is HTML too, and is never what someone meant to open.

## Why forwarding a port is not enough

The obvious approach is to forward the container's port 5173 to a free port on your
machine. It works right up until the app talks to itself.

A React frontend on 5173 calling its API at `http://localhost:8000` is making a request
**from your browser**, which means *your* machine's port 8000 — not the container's. The
port got renumbered on the way out, and now the app fails in a way that looks like your
code is wrong.

You cannot fix that by rewriting the app, either. Agents write absolute URLs; expecting
every generated project to use relative paths is a bet you will lose.

## What Moonphase does instead

**Open** launches a window whose entire network lives inside the container.

Moonphase speaks SOCKS, and terminates each connection inside the container
itself over the SSH connection it already holds. The preview window is pointed at
that proxy, with loopback exempted from the browser's bypass rules so `localhost`
resolves *there* rather than here.

So `http://localhost:5173` loads the frontend, and its call to
`http://localhost:8000` reaches the container's port 8000. Nothing is renumbered,
and nothing has to be written any particular way.

The app opens that proxy on its **own** loopback and carries each connection to
your instance over a WebSocket, authenticated as you and checked against your
access to the project. That is what lets this work against an instance running
somewhere else: the proxy has to be somewhere the browser can reach, and the
browser is on your desk.

!!! note "Desktop only"
    This needs control over the browser's proxy configuration, which a web page cannot
    have. In a browser you get forwarded ports and a warning that an app calling its own
    API may not work.

## Public links

**Get a public link** puts one port on the network so you can show someone.

!!! warning "A public link is public"
    Anyone who has it can reach the service, with no password. It is meant for showing a
    colleague something for ten minutes, not for hosting.

    Stop sharing when you are done. The panel lists everything currently public so you
    cannot forget one.

Where those links are reachable from is set by `MOONPHASE_PREVIEW_BIND` and
`MOONPHASE_PREVIEW_HOST` — see [configuration](../reference/configuration.md).

## No inbound ports on your server

Everything is tunnelled back through the SSH connection Moonphase already holds. Your
servers never need a port opened for previews, and the containers stay on a private
network.

Nor does your Moonphase instance open one. The proxy used to listen on the
machine running the API — which was reachable only by a browser on that same
machine, and so was safe and useless the moment the app was installed anywhere
else. Nothing listens there now; the stream is carried over the same authenticated
connection as everything else.
