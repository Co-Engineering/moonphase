#!/usr/bin/env python3
"""Full-stack smoke test.

Drives Moonphase the way a client does — sign up through GoTrue, then hit the
real HTTP API and WebSocket with a bearer token — against a throwaway sshd
container standing in for a managed server.

This covers what the unit tests cannot: routing, JWT verification, RLS applied
through `user_session`, and the PTY bridge over an actual WebSocket.

    supabase start && uvicorn moonphase.main:app &
    python scripts/smoke.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid

import httpx
import websockets

API = "http://127.0.0.1:8787"
SUPABASE = "http://127.0.0.1:54321"
SSH_PORT = 22322
FIXTURE_IMAGE = "moonphase/fake-server:latest"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def step(message: str) -> None:
    print(f"  {PASS} {message}")


def die(message: str, detail: object = "") -> None:
    print(f"  {FAIL} {message}")
    if detail:
        print(f"      {detail}")
    sys.exit(1)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def start_fixture() -> str:
    name = f"moonphase-smoke-{uuid.uuid4().hex[:8]}"
    docker("rm", "-f", name, check=False)
    docker(
        "run", "-d", "--name", name,
        "-p", f"127.0.0.1:{SSH_PORT}:22",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        FIXTURE_IMAGE,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        logs = docker("logs", name, check=False)
        if "Server listening on 0.0.0.0" in (logs.stdout + logs.stderr):
            return name
        time.sleep(0.4)
    docker("rm", "-f", name, check=False)
    die("fixture sshd never started")
    raise SystemExit(1)


def read_anon_key() -> str:
    """Read the local anon key. Blocking, so it runs before the event loop."""
    out = subprocess.run(
        ["supabase", "status", "-o", "env"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    key = next(
        (line.split('"')[1] for line in out.splitlines() if line.startswith("ANON_KEY=")),
        None,
    )
    if not key:
        die("could not read ANON_KEY from `supabase status`")
    return key or ""


async def main(anon_key: str) -> None:
    print("\nMoonphase smoke test\n")

    fixture = start_fixture()
    step(f"fixture server up on 127.0.0.1:{SSH_PORT}")

    project_id = None
    try:
        async with httpx.AsyncClient(timeout=180) as http:
            health = await http.get(f"{API}/api/health")
            if health.status_code != 200:
                die("API health check failed", health.text)
            step(f"api healthy — {health.json()}")

            # --- auth --------------------------------------------------------
            email = f"smoke-{uuid.uuid4().hex[:8]}@example.test"
            signup = await http.post(
                f"{SUPABASE}/auth/v1/signup",
                headers={"apikey": anon_key, "Content-Type": "application/json"},
                json={"email": email, "password": "smoke-test-password-123"},
            )
            if signup.status_code >= 300:
                die("signup failed", signup.text)
            token = signup.json().get("access_token")
            if not token:
                die("signup returned no access token", signup.text)
            auth = {"Authorization": f"Bearer {token}"}
            step(f"signed up as {email}")

            # --- unauthenticated access must be refused ----------------------
            naked = await http.get(f"{API}/api/servers")
            if naked.status_code not in (401, 403):
                die(f"unauthenticated request returned {naked.status_code}, expected 401")
            step("unauthenticated request rejected")

            orgs = (await http.get(f"{API}/api/organizations", headers=auth)).json()
            if len(orgs) != 1 or not orgs[0]["is_personal"]:
                die("expected exactly one personal org", orgs)
            step(f"personal org created automatically — {orgs[0]['slug']}")

            harnesses = (await http.get(f"{API}/api/harnesses", headers=auth)).json()
            step(f"harnesses: {[h['display_name'] for h in harnesses]}")

            # --- add a server via password bootstrap -------------------------
            created = await http.post(
                f"{API}/api/servers",
                headers=auth,
                json={
                    "name": "smoke-server",
                    "host": "127.0.0.1",
                    "port": SSH_PORT,
                    "ssh_user": "deploy",
                    "auth_mode": "password_bootstrap",
                    "password": "moonphase-test",
                    "auto_install_docker": False,
                },
            )
            if created.status_code >= 300:
                die("add server failed", created.text)
            body = created.json()
            if body["status"] != "online":
                die(f"server not online: {body['status']}", body.get("detail"))
            server = body["server"]
            step(
                f"server bootstrapped — docker {server['docker_version']}, "
                f"host key {server['host_key_fingerprint'][:24]}…"
            )
            if not server["managed_public_key"]:
                die("no managed key was recorded")
            step("moonphase key installed and recorded")

            # --- create a project --------------------------------------------
            created_project = await http.post(
                f"{API}/api/projects",
                headers=auth,
                json={
                    "server_id": server["id"],
                    "name": "Smoke Project",
                    "harness": "claude_code",
                    "harness_auth_mode": "api_key",
                    "api_key": "sk-ant-smoke-not-real",
                },
            )
            if created_project.status_code >= 300:
                die("create project failed", created_project.text)
            project = created_project.json()
            if project["status"] != "running":
                die(f"project not running: {project['status']}", project.get("status_detail"))
            project_id = project["id"]
            step(f"project provisioned — container {project['container_name']}")

            # --- start the harness session -----------------------------------
            session = await http.post(
                f"{API}/api/projects/{project_id}/sessions/start",
                headers=auth,
                json={"restart": False},
            )
            if session.status_code >= 300:
                die("session start failed", session.text)
            step(f"tmux session started — state {session.json()['state']}")

            # --- attach the WebSocket terminal --------------------------------
            ws_url = (
                f"{API.replace('http', 'ws')}/ws/projects/{project_id}/terminal"
                f"?token={token}&cols=120&rows=32"
            )
            received = bytearray()
            attached = False
            async with websockets.connect(ws_url, max_size=None) as socket:
                deadline = time.time() + 30
                while time.time() < deadline and len(received) < 200:
                    try:
                        frame = await asyncio.wait_for(socket.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        break
                    if isinstance(frame, bytes):
                        received.extend(frame)
                    else:
                        message = json.loads(frame)
                        if message.get("type") == "attached":
                            attached = True
                        elif message.get("type") == "error":
                            die("terminal error", message.get("message"))

                if not attached:
                    die("never received the `attached` control frame")
                step("websocket attached to the live PTY")

                await socket.send(json.dumps({"type": "resize", "cols": 100, "rows": 40}))
                await socket.send(b"\x0c")  # ctrl-L, force a redraw
                try:
                    frame = await asyncio.wait_for(socket.recv(), timeout=10)
                    if isinstance(frame, bytes):
                        received.extend(frame)
                except asyncio.TimeoutError:
                    pass

            if len(received) < 50:
                die(f"PTY produced only {len(received)} bytes")
            step(f"received {len(received)} bytes of terminal output")

            # --- detaching must not stop the session --------------------------
            await asyncio.sleep(1.5)
            snapshot = await http.get(
                f"{API}/api/projects/{project_id}/sessions/snapshot", headers=auth
            )
            text = snapshot.json()["text"]
            if not text.strip():
                die("session pane is empty after detach — the session did not survive")
            step("session still alive after the websocket closed")

            marker = "claude" in text.lower() or "welcome" in text.lower()
            step(
                "harness visible in the pane"
                if marker
                else "pane has content (harness banner not matched)"
            )

            # --- another user must not see any of it --------------------------
            other_email = f"smoke-other-{uuid.uuid4().hex[:8]}@example.test"
            other = await http.post(
                f"{SUPABASE}/auth/v1/signup",
                headers={"apikey": anon_key, "Content-Type": "application/json"},
                json={"email": other_email, "password": "smoke-test-password-123"},
            )
            other_auth = {"Authorization": f"Bearer {other.json()['access_token']}"}
            their_servers = (await http.get(f"{API}/api/servers", headers=other_auth)).json()
            their_projects = (await http.get(f"{API}/api/projects", headers=other_auth)).json()
            if their_servers or their_projects:
                die("tenant isolation broken", (their_servers, their_projects))
            step("second user sees no servers or projects")

            forbidden = await http.get(f"{API}/api/projects/{project_id}", headers=other_auth)
            if forbidden.status_code != 404:
                die(f"direct fetch by id returned {forbidden.status_code}, expected 404")
            step("direct fetch by id is refused for the other tenant")

            ws_forbidden = (
                f"{API.replace('http', 'ws')}/ws/projects/{project_id}/terminal"
                f"?token={other.json()['access_token']}"
            )
            try:
                async with websockets.connect(ws_forbidden) as socket:
                    frame = await asyncio.wait_for(socket.recv(), timeout=10)
                    message = json.loads(frame) if isinstance(frame, str) else {}
                    if message.get("type") != "error":
                        die("other tenant attached to the terminal", message)
            except websockets.exceptions.WebSocketException:
                pass
            step("other tenant cannot attach to the terminal")

            # --- cleanup ------------------------------------------------------
            await http.delete(
                f"{API}/api/projects/{project_id}?delete_volumes=true", headers=auth
            )
            await http.delete(f"{API}/api/servers/{server['id']}", headers=auth)
            step("project and server removed")

        print(f"\n  {PASS} smoke test passed\n")

    finally:
        docker("rm", "-f", fixture, check=False)
        for name in docker(
            "ps", "-aq", "--filter", "label=moonphase=1", check=False
        ).stdout.split():
            docker("rm", "-f", name, check=False)


if __name__ == "__main__":
    asyncio.run(main(read_anon_key()))
