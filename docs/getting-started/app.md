# Installing the app

One command on a computer, two taps on a phone.

!!! info "This is the app, not the server"
    The app connects to a Moonphase you are already running. It asks for the
    address the first time you open it, and installing it changes nothing on any
    server.

    No server yet? [Install one first](docker.md) — that is also one command.

## On a computer

=== "Linux"

    ```console
    $ curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install-app.sh | sh
    ```

    Installs to `~/.local/share/moonphase` with a launcher at
    `~/.local/bin/moonphase` and an entry in your applications menu. No root,
    nothing outside your home directory, and about 350 MB.

    Launch it from the menu, or run `moonphase`.

    !!! note "Why it unpacks instead of leaving an AppImage"
        An AppImage mounts itself through FUSE 2, and distributions that have
        moved on to FUSE 3 — Arch and Fedora among them — answer with
        `dlopen(): error loading libfuse.so.2` and nothing else. Installing a
        compatibility package to open an app is a second command, and this is
        meant to be one. Unpacked, it needs no FUSE and starts faster.

        The `.AppImage` on [the releases
        page](https://github.com/oliversvane/moonphase/releases) is the same
        build if you would rather have the single file — that one does need
        `libfuse2`.

    !!! note "If nothing appears in the menu"
        Some desktops only rescan on login. `moonphase` from a terminal works
        either way, and the entry will be there next time you log in.

=== "macOS"

    ```console
    $ curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install-app.sh | sh
    ```

    The same command as Linux — it works out which one it is on. Moonphase lands
    in `~/Applications`; open it from Spotlight, or with `open -a Moonphase`.

    !!! warning "It clears the quarantine flag, and says so"
        These builds are not signed by Apple, so macOS quarantines the download
        and the first open fails with *"Moonphase is damaged and can't be
        opened"* — which is not what has happened. The installer clears that
        flag, and prints a line saying it did. See
        [about the unsigned builds](#about-the-unsigned-builds).

=== "Windows"

    ```powershell
    irm https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install-app.ps1 | iex
    ```

    In PowerShell — no administrator rights needed. It installs for your user
    and puts Moonphase in the Start menu.

    !!! note "If PowerShell refuses to run it"
        Execution policy blocks piped scripts on some machines. This allows it
        for the current window only, and nothing beyond it:

        ```powershell
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
        ```

!!! tip "Read it before you pipe it"
    Piping a script from the internet into a shell is worth being suspicious of.
    Both are short and do exactly what this page says:
    [install-app.sh](https://github.com/oliversvane/moonphase/blob/main/scripts/install-app.sh)
    ·
    [install-app.ps1](https://github.com/oliversvane/moonphase/blob/main/scripts/install-app.ps1)

    Or take the file yourself from
    [the releases page](https://github.com/oliversvane/moonphase/releases) —
    `.AppImage` and `.deb` for Linux, `.dmg` for macOS, `.exe` for Windows, in
    x64 and arm64.

### First launch

It asks for the address of your Moonphase — the same one you use in a browser,
like `https://moonphase.example.com`. It is remembered, and you can change it
later from **Host** at the bottom of the sidebar.

## On a phone

There is no App Store or Play Store download. Moonphase installs from its own
address, which means the phone app is always the same version as the server it
talks to.

=== "iPhone and iPad"

    1. Open your Moonphase address in **Safari**. It has to be Safari — Chrome
       and Firefox on iOS cannot install to the home screen.
    2. Tap **Share** (the square with an arrow).
    3. Scroll down and tap **Add to Home Screen**.
    4. Tap **Add**.

    It now opens full screen with no browser chrome, and appears in the app
    switcher like anything else.

    !!! warning "Notifications need this step"
        iOS delivers push only to a home-screen app, never to a Safari tab. If
        you skip this, **Settings → Notifications → Enable** will not work, and
        being told when an agent needs you is most of the point.

=== "Android"

    1. Open your Moonphase address in **Chrome**.
    2. Tap the **⋮** menu.
    3. Tap **Install app** — or **Add to Home screen** on older versions.
    4. Tap **Install**.

    Chrome often offers this by itself as a banner at the bottom of the screen a
    moment after the page loads.

!!! danger "Phones need HTTPS"
    Installing to the home screen and receiving notifications both require a
    secure context. A phone pointed at `http://192.168.1.20:8471` can browse the
    app but cannot install it or be notified by it, and browsers say very little
    about why.

    [Give the instance a domain](../guides/dns.md) and HTTPS is automatic. A
    Tailscale HTTPS address or a Cloudflare tunnel work too.

## Or just use a browser

Nothing has to be installed at all. Every feature except notifications works in
an ordinary tab, on any device.

Chrome and Edge on a desktop will also offer to install the page as its own
window — the icon at the right of the address bar. That is the same app as the
download, without the download.

## Updating

=== "Computer"

    Run the install command again. It replaces what it installed last time and
    keeps the address you configured.

=== "Phone"

    Nothing to do. The app is served by your instance, so
    [upgrading the server](docker.md#upgrading) upgrades every phone with it.

## Uninstalling

=== "Linux"

    ```console
    $ rm -rf ~/.local/share/moonphase
    $ rm ~/.local/bin/moonphase
    $ rm ~/.local/share/applications/moonphase.desktop
    $ rm ~/.local/share/icons/hicolor/512x512/apps/moonphase.png
    ```

=== "macOS"

    ```console
    $ rm -rf ~/Applications/Moonphase.app
    ```

=== "Windows"

    **Settings → Apps → Installed apps → Moonphase → Uninstall.**

=== "Phone"

    Remove the icon from the home screen, the same as any app.

Nothing on your servers is touched, and no session stops. The app is a window,
not the thing running your agents.

## About the unsigned builds

Nothing here is signed by Apple or by a Windows certificate authority, so both
systems treat the download as untrusted until told otherwise:

| | What you would see | What the installer does |
| --- | --- | --- |
| **macOS** | *"Moonphase is damaged and can't be opened"* | Clears the quarantine flag, and prints that it did |
| **Windows** | *"Windows protected your PC"* (SmartScreen) | Runs the installer directly, so the panel never appears |
| **Linux** | Nothing — Linux does not do this | — |

Neither weakens anything system-wide, and neither is unique to Moonphase: it is
what every unsigned open-source desktop app runs into. If that trade is not one
you want to make, use the browser or a phone instead — same app, no download.

Signing costs money every year, per platform. It is on
[the roadmap](../roadmap.md) rather than done.

## Which build you get

The install commands fetch **edge**, rebuilt from `main` on every commit. It is
what this project runs on itself.

To pin a version instead, set a channel:

```console
$ curl -fsSL https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install-app.sh \
    | MOONPHASE_CHANNEL=v0.1.0 sh
```

```powershell
$env:MOONPHASE_CHANNEL = 'v0.1.0'; irm https://raw.githubusercontent.com/oliversvane/moonphase/main/scripts/install-app.ps1 | iex
```

!!! warning "The variable goes before `sh`, not before `curl`"
    `MOONPHASE_CHANNEL=… curl … | sh` looks right and is not: the assignment
    applies to `curl`, which does not read it, and the shell running the script
    never sees it.
