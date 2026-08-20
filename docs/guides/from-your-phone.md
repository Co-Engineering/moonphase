# Working from your phone

The point of moving a session onto a server is that you can stop watching it. The point
of the notification is that you find out when that stops being safe.

This page is about what a phone is *for* here. Getting the app onto one is
[installing the app](../getting-started/app.md#on-a-phone) — two taps, no store
download.

Two things have to be true before any of the below works, and both are covered
there: the app is on your **home screen**, and your instance is on **HTTPS**.
iOS will not deliver a push to a Safari tab, and no browser will deliver one over
plain HTTP.

## Turn on notifications

**Settings → Notifications → Enable.**

You get a push whenever one of your sessions starts waiting for you.

Notifications go to the person who owns the session and nobody else. Someone watching a
colleague's agent cannot answer its questions — it runs on the colleague's account — so
waking them would be noise, and the useful signal drowns quickly.

## Answer without opening anything

Tapping the notification lands you on the home screen, where the waiting question is
already parsed into buttons:

> **fresh-demo** · oliver-test
>
> Do you want to proceed?
>
> `1` Yes    `2` No, tell Claude what to do differently
>
> *Show the last few lines*

Tap an option and it goes straight into the same `tmux` session your desktop is attached
to. If the prompt could not be parsed into options you get a text box instead.

!!! tip "Look before you approve"
    **Show the last few lines** expands the terminal output that led to the question.
    Approving a permission prompt without seeing what it is about is how you approve the
    wrong thing, and a phone makes that easier, not harder.

## What the badge means

The home-screen icon carries a count of how many notifications are still unread. It is
derived from the notification shade rather than counted in the service worker, because a
service worker is stopped and restarted at the browser's discretion and any total it kept
would be wrong by morning.

## The feed, not the terminal

On a narrow screen Moonphase defaults to the **Feed** rather than the terminal. That is
not only about readability: attaching a terminal would drag the shared `tmux` window down
to phone width, and your desktop would find its session had shrunk.

The feed writes back into the same session, so you lose nothing but the ability to do
something unusual.
