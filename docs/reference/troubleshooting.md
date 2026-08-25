# Troubleshooting

The failures that are hard to diagnose from what they print.


**"Invalid token" on every request.** Supabase now signs with ES256 via JWKS.
Check `SUPABASE_URL` points at the auth service the client is using — the API
fetches `/auth/v1/.well-known/jwks.json` from it.

**CORS errors in the browser.** `MOONPHASE_CORS_ORIGINS` must list the exact
origin the frontend is served from, and the API must be restarted after
changing it. It cannot be set to `*`: credentialed requests are always on, so
a wildcard origin would be silently reflected back for every caller instead
of enforcing anything — the API refuses to start rather than allow that.

**Electron shows a blank window / connection refused.** Vite must bind IPv4;
`server.host` is pinned to `127.0.0.1` for this reason, since `localhost`
resolves to `::1` on some systems while Electron loads `127.0.0.1`.

**Electron fails to start with "failed to install correctly."** Its postinstall
can silently produce an incomplete `dist/` on very new Node versions. Extract
the cached zip by hand:

```bash
cd node_modules/.pnpm/electron@*/node_modules/electron
rm -rf dist && mkdir dist
unzip -q ~/.cache/electron/*/electron-v*-linux-x64.zip -d dist
printf 'electron' > path.txt
```

**Server stuck in `error` after a Docker install.** Group membership only
applies to new sessions. Press **Test** to reconnect and re-probe.

**Push notifications never arrive.** Almost always the secure-context rule: service
workers and Web Push only work over HTTPS, and `localhost` is the only exemption. Check
`MOONPHASE_VAPID_PUBLIC_KEY` and `MOONPHASE_VAPID_PRIVATE_KEY` are set and the API was
restarted afterwards. On iOS the app must be **added to the home screen** — Safari will
not deliver push to a normal tab.

**A session shows as `working` long after it stopped.** The monitor decides activity by
watching whether the pane changes. Check `MOONPHASE_MONITOR_INTERVAL` is not `0`, and
that the backend can still reach the server — an unreachable machine keeps its last known
state rather than inventing a new one, and the sidebar shows when it was last confirmed.

**Keystrokes are swallowed in a session you own.** You are probably in someone else's
session, or in a read-only view of your own before the session list resolved. The bar at
the top says whose it is. The server enforces this regardless of what the interface
shows.

**Cost is blank on the usage screen.** No rate is known for that model. It is deliberately
not `$0.00` — see [usage and limits](../guides/usage-and-limits.md#cost-and-when-it-is-blank).

**A preview loads but its API calls fail.** You are in a browser rather than the desktop
app, so ports are forwarded and renumbered. See
[previews](../guides/previews.md#why-forwarding-a-port-is-not-enough).

**"No such session" when saving a save point.** Save points only work in sessions you
own, because they commit as the session's git identity.
