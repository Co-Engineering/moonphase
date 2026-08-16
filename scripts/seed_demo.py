#!/usr/bin/env python3
"""Create a demo user, server and project against a local stack.

Used to populate the UI for visual checks. Prints the credentials as JSON on
stdout so a capture script can sign in as that user.

    python scripts/seed_demo.py            # create, print credentials
    python scripts/seed_demo.py --teardown # remove containers it created
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid

import httpx

API = "http://127.0.0.1:8471"
SUPABASE = "http://127.0.0.1:54721"
SSH_PORT = 22422
FIXTURE_NAME = "moonphase-demo-server"
FIXTURE_IMAGE = "moonphase/fake-server:latest"
PASSWORD = "demo-password-12345"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def teardown() -> None:
    docker("rm", "-f", FIXTURE_NAME, check=False)
    for name in docker(
        "ps", "-aq", "--filter", "label=moonphase=1", check=False
    ).stdout.split():
        docker("rm", "-f", name, check=False)
    print("demo resources removed", file=sys.stderr)


def anon_key() -> str:
    out = subprocess.run(
        ["supabase", "status", "-o", "env"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return next(line.split('"')[1] for line in out.splitlines() if line.startswith("ANON_KEY="))


def start_fixture() -> None:
    docker("rm", "-f", FIXTURE_NAME, check=False)
    docker(
        "run", "-d", "--name", FIXTURE_NAME,
        "-p", f"127.0.0.1:{SSH_PORT}:22",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        FIXTURE_IMAGE,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        logs = docker("logs", FIXTURE_NAME, check=False)
        if "Server listening on 0.0.0.0" in (logs.stdout + logs.stderr):
            return
        time.sleep(0.4)
    raise SystemExit("fixture sshd never started")


def main() -> None:
    if "--teardown" in sys.argv:
        teardown()
        return

    key = anon_key()
    start_fixture()
    print(f"fixture up on {SSH_PORT}", file=sys.stderr)

    email = f"demo-{uuid.uuid4().hex[:6]}@example.test"
    with httpx.Client(timeout=300) as http:
        signup = http.post(
            f"{SUPABASE}/auth/v1/signup",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"email": email, "password": PASSWORD},
        )
        signup.raise_for_status()
        auth = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        # Projects now require a connected harness, which is the point: an
        # unconfigured one would come up unable to do anything.
        http.post(
            f"{API}/api/profile/harness/api-key",
            headers=auth,
            json={"api_key": "sk-ant-demo-not-real", "harness": "claude_code"},
        ).raise_for_status()
        print("harness connected (demo key)", file=sys.stderr)

        server = http.post(
            f"{API}/api/servers",
            headers=auth,
            json={
                "name": "srv-hetzner",
                "host": "127.0.0.1",
                "port": SSH_PORT,
                "ssh_user": "deploy",
                "auth_mode": "password_bootstrap",
                "password": "moonphase-test",
                "auto_install_docker": False,
            },
        )
        server.raise_for_status()
        server_id = server.json()["server"]["id"]
        print(f"server: {server.json()['status']}", file=sys.stderr)

        for project_name in ("moonphase-api", "landing-page"):
            project = http.post(
                f"{API}/api/projects",
                headers=auth,
                json={
                    "server_id": server_id,
                    "name": project_name,
                    "harness": "claude_code",
                    "environment": "debian",
                },
            )
            project.raise_for_status()
            pid = project.json()["id"]
            http.post(
                f"{API}/api/projects/{pid}/sessions/start",
                headers=auth,
                json={"restart": False},
            ).raise_for_status()
            print(f"project {project_name}: {project.json()['status']}", file=sys.stderr)

    json.dump({"email": email, "password": PASSWORD}, sys.stdout)
    print()


if __name__ == "__main__":
    main()
