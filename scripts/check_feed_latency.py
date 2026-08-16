#!/usr/bin/env python3
"""Measure how quickly a transcript line reaches a connected feed client.

Polling put a hard floor of one interval — three seconds — on this, which is
what made the phone feel a step behind the terminal. Streaming should be well
under a second, and this is how that claim stays honest.

Needs a running stack and a seeded demo project:

    ./scripts/dev.sh
    apps/api/.venv/bin/python scripts/seed_demo.py     # prints credentials
    apps/api/.venv/bin/python scripts/check_feed_latency.py <email> <password>
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import time
import uuid

import httpx
import websockets

API = "http://127.0.0.1:8471"
SUPABASE = "http://127.0.0.1:54721"
TRANSCRIPT_DIR = "/home/dev/.claude/projects/-workspace"

# Anything above this and the stream is no better than the polling it replaced.
BUDGET_SECONDS = 1.5


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def anon_key() -> str:
    out = run("supabase", "status", "-o", "env").stdout
    return next(line.split('"')[1] for line in out.splitlines() if line.startswith("ANON_KEY="))


def transcript_line(uid: str, text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uid,
            "timestamp": "2026-08-17T10:00:00Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def append(container: str, text: str, *, truncate: bool = False) -> None:
    """Write a transcript line the way the harness would: one appended line."""
    redirect = ">" if truncate else ">>"
    run(
        "docker", "exec", "-u", "dev", container, "sh", "-c",
        f"mkdir -p {TRANSCRIPT_DIR} && printf '%s\\n' {shlex.quote(text)} "
        f"{redirect} {TRANSCRIPT_DIR}/live.jsonl",
    )


async def main(email: str, password: str) -> int:
    key = anon_key()

    async with httpx.AsyncClient(timeout=60) as http:
        auth_response = await http.post(
            f"{SUPABASE}/auth/v1/token?grant_type=password",
            headers={"apikey": key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        auth_response.raise_for_status()
        token = auth_response.json()["access_token"]

        projects = (
            await http.get(f"{API}/api/projects", headers={"Authorization": f"Bearer {token}"})
        ).json()

    running = [p for p in projects if p["status"] == "running"]
    if not running:
        print("no running project; run scripts/seed_demo.py first")
        return 1

    project = running[0]
    container = project["container_name"]
    print(f"  project {project['name']} ({container})")

    append(container, transcript_line("seed", "seeded history"), truncate=True)

    url = f"{API.replace('http', 'ws')}/ws/projects/{project['id']}/feed?token={token}"
    async with websockets.connect(url, max_size=None) as socket:
        page = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
        if page.get("type") != "page":
            print(f"  expected a page first, got {page}")
            return 1
        print(f"  initial page: {len(page['events'])} event(s)")

        latencies: list[float] = []
        for index in range(3):
            marker = f"streamed-{index}-{uuid.uuid4().hex[:6]}"
            sent = time.monotonic()
            append(container, transcript_line(f"m{index}", marker))

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                message = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
                if message.get("type") == "events" and any(
                    e["text"] == marker for e in message["events"]
                ):
                    latencies.append(time.monotonic() - sent)
                    break
            else:
                print(f"  never received {marker}")
                return 1

    for index, seconds in enumerate(latencies):
        print(f"  line {index}: arrived in {seconds * 1000:.0f} ms")

    worst = max(latencies)
    print(f"\n  worst {worst * 1000:.0f} ms (the polling floor was 3000 ms)")
    if worst > BUDGET_SECONDS:
        print("  FAIL: no better than polling")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2])))
