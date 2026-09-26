"""Explicitly opt-in live verification; creates demo data, never migrates/resets.

Uses httpx and websockets already provided by uvicorn[standard]. See README.
"""

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
from urllib.parse import urlencode, urlparse

import asyncpg
import httpx
from websockets.asyncio.client import connect


class Listener:
    def __init__(self, base_url, key, token, room_id, *, filtered=True, topic=None):
        self.url = base_url.replace("https://", "wss://").replace("http://", "ws://")
        self.url += "/realtime/v1/websocket?" + urlencode({"apikey": key, "vsn": "1.0.0"})
        self.token = token
        self.filtered = filtered
        self.topic = topic or f"realtime:room:{room_id}"
        self.room_id = room_id
        self.events = []
        self.changed = asyncio.Event()
        self.socket = None
        self.reader = None
        self.heartbeat = None

    def handlers(self):
        handlers = []
        for table, events, column in [
            ("rooms", ["UPDATE"], "id"),
            ("room_members", ["INSERT", "UPDATE"], "room_id"),
            ("game_sessions", ["INSERT", "UPDATE"], "room_id"),
            ("rounds", ["INSERT", "UPDATE"], "room_id"),
            ("timeline_events", ["INSERT"], "room_id"),
        ]:
            for event in events:
                handler = {"event": event, "schema": "public", "table": table}
                if self.filtered:
                    handler["filter"] = f"{column}=eq.{self.room_id}"
                handlers.append(handler)
        return handlers

    def capture(self, event):
        if event["event"] == "postgres_changes":
            self.events.append(event["payload"]["data"])
            self.changed.set()
        elif event["event"] in {"phx_error", "phx_close"}:
            raise RuntimeError("Realtime channel closed unexpectedly")

    async def join(self):
        self.events = []
        self.changed = asyncio.Event()
        self.socket = await connect(self.url)
        await self.socket.send(
            json.dumps(
                {
                    "topic": self.topic,
                    "event": "phx_join",
                    "ref": "1",
                    "payload": {
                        "config": {
                            "broadcast": {"ack": False, "self": False},
                            "presence": {"key": ""},
                            "postgres_changes": self.handlers(),
                        },
                        "access_token": self.token,
                    },
                }
            )
        )
        joined = False
        subscribed = False
        try:
            async with asyncio.timeout(15):
                # phx_reply can arrive before Postgres Changes is actually listening.
                # Mutations in that gap are not replayed, so wait for both.
                while not (joined and subscribed):
                    reply = json.loads(await self.socket.recv())
                    if reply["event"] == "postgres_changes":
                        self.capture(reply)
                        continue
                    payload = reply.get("payload") or {}
                    if reply["event"] == "phx_reply" and reply.get("ref") == "1":
                        if payload.get("status") != "ok":
                            raise RuntimeError("Realtime subscription rejected")
                        joined = True
                    elif reply["event"] == "system" and payload.get("status") == "ok":
                        if "Subscribed to PostgreSQL" in str(payload.get("message") or ""):
                            subscribed = True
        except BaseException:
            await self.socket.close()
            raise
        self.reader = asyncio.create_task(self.read())
        self.heartbeat = asyncio.create_task(self.beat())

    async def resubscribe(self):
        await self.close()
        await self.join()

    async def __aenter__(self):
        await self.join()
        return self

    async def read(self):
        async for raw in self.socket:
            self.capture(json.loads(raw))

    async def beat(self):
        counter = 1
        while True:
            await asyncio.sleep(20)
            counter += 1
            await self.socket.send(
                json.dumps(
                    {"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": str(counter)}
                )
            )

    async def wait(self, table, predicate):
        async with asyncio.timeout(15):
            while True:
                self.changed.clear()
                for event in self.events:
                    if event["table"] == table and predicate(event["record"]):
                        return event["record"]
                if self.reader.done():
                    self.reader.result()
                    raise RuntimeError("Realtime connection ended")
                await self.changed.wait()

    async def close(self):
        for task in [self.reader, self.heartbeat]:
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.reader = None
        self.heartbeat = None
        if self.socket is not None:
            await self.socket.close()
            self.socket = None

    async def __aexit__(self, *args):
        await self.close()


def require_local(url):
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Refusing non-local verification target")


def id_list(value):
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
        return [item for item in value[1:-1].split(",") if item]
    return []


def concerns_room(event, room_id):
    row = event.get("record") or {}
    if event["table"] == "rooms":
        return str(row.get("id")) == str(room_id)
    return str(row.get("room_id")) == str(room_id)


def newer(current, row):
    if current is None:
        return True
    return int(row.get("revision") or 0) > int(current.get("revision") or 0)


def install_snapshot(snapshot):
    sessions = {}
    for key in ("active_session", "last_session"):
        session = snapshot.get(key)
        if session:
            sessions[str(session["id"])] = session
    return {
        "room": snapshot["room"],
        "members": {(str(m["room_id"]), str(m["profile_id"])): m for m in snapshot["members"]},
        "sessions": sessions,
        "rounds": {str(r["id"]): r for r in snapshot["rounds"]},
        "timeline": {str(event["id"]): event for event in snapshot["timeline"]},
    }


def apply_event(state, event):
    table = event["table"]
    row = event["record"]
    if table == "timeline_events":
        state["timeline"].setdefault(str(row["id"]), row)
    elif table == "rooms":
        if newer(state["room"], row):
            state["room"] = row
    elif table == "room_members":
        key = (str(row["room_id"]), str(row["profile_id"]))
        if newer(state["members"].get(key), row):
            state["members"][key] = row
    elif table == "game_sessions":
        key = str(row["id"])
        if newer(state["sessions"].get(key), row):
            state["sessions"][key] = row
    elif table == "rounds":
        key = str(row["id"])
        if newer(state["rounds"].get(key), row):
            state["rounds"][key] = row


def reconcile(snapshot, events):
    state = install_snapshot(snapshot)
    for event in events:
        apply_event(state, event)
    timeline_count = len(state["timeline"])
    for event in events:
        apply_event(state, event)
    assert len(state["timeline"]) == timeline_count, "Timeline events were duplicated"
    return state


def assert_no_secrets(value):
    if isinstance(value, dict):
        assert not {"answer", "reveal_copy", "source_item_ids", "value"} & set(value)
        for child in value.values():
            assert_no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_secrets(child)


def assert_public(events):
    for event in events:
        assert event["table"] not in {"round_responses", "round_secrets"}
        assert_no_secrets(event["record"])
        if event["table"] == "rounds":
            row = event["record"]
            if row["phase"] in {"pending", "answering"}:
                assert row["reveal"] is None
            assert row["phase"] != "pending"


async def main():
    required = ["VERIFY_API_URL", "VERIFY_SUPABASE_URL", "VERIFY_PUBLISHABLE_KEY"]
    required += [f"VERIFY_{kind}_{letter}" for kind in ["TOKEN", "PROFILE"] for letter in "ABC"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise SystemExit("Missing explicit configuration: " + ", ".join(missing))
    base = os.environ["VERIFY_SUPABASE_URL"].rstrip("/")
    require_local(base)
    require_local(os.environ["VERIFY_API_URL"])
    key = os.environ["VERIFY_PUBLISHABLE_KEY"]
    tokens = {letter: os.environ[f"VERIFY_TOKEN_{letter}"] for letter in "ABC"}
    profiles = {letter: os.environ[f"VERIFY_PROFILE_{letter}"] for letter in "ABC"}
    assert len(set(profiles.values())) == 3, "Use three distinct profiles"
    async with httpx.AsyncClient(
        base_url=os.environ["VERIFY_API_URL"].rstrip("/"), timeout=20
    ) as api:

        async def command(letter, path, body=None):
            response = await api.post(
                "/v1" + path, json=body or {}, headers={"Authorization": "Bearer " + tokens[letter]}
            )
            # Do not print tokens or raw response bodies on failures.
            if response.is_error:
                raise RuntimeError(f"{path} returned HTTP {response.status_code}")
            return response.json() if response.status_code != 204 else None

        for letter in "ABC":
            await command(letter, "/demo-sessions", {"profile_id": profiles[letter]})
        room = await command("A", "/rooms", {"name": "Realtime verification"})
        room_id = room["id"]
        async with (
            Listener(base, key, tokens["A"], room_id) as a,
            Listener(base, key, tokens["B"], room_id) as b,
            Listener(base, key, tokens["C"], room_id) as c,
            Listener(
                base, key, tokens["C"], room_id, filtered=False, topic="realtime:broad-outsider"
            ) as outsider,
        ):
            await command("B", "/rooms/join", {"code": room["join_code"]})
            for listener in [a, b]:
                member = await listener.wait(
                    "room_members",
                    lambda row, pid=profiles["B"]: (
                        str(row["profile_id"]) == pid and row["left_at"] is None
                    ),
                )
                assert str(member["room_id"]) == room_id
            assert not any(concerns_room(event, room_id) for event in c.events + outsider.events)

            for letter in "AB":
                response = await api.get(
                    f"/v1/rooms/{room_id}", headers={"Authorization": "Bearer " + tokens[letter]}
                )
                response.raise_for_status()
            denied_room = await api.get(
                f"/v1/rooms/{room_id}", headers={"Authorization": "Bearer " + tokens["C"]}
            )
            if denied_room.status_code != 403:
                raise RuntimeError(f"Outsider hydrate returned HTTP {denied_room.status_code}")
            result = await command("A", f"/rooms/{room_id}/sessions", {"vibe": "chaos"})
            session_id = result["session"]["id"]
            for ordinal in range(1, 4):
                current = result["current_round"]
                rid = current["id"]
                for listener in [a, b]:
                    observed = await listener.wait(
                        "rounds",
                        lambda row, rid=rid: row["id"] == rid and row["phase"] == "answering",
                    )
                    assert observed["prompt"] == current["prompt"]
                    assert observed["reveal"] is None
                if ordinal == 1:
                    await b.close()
                await command("A", f"/rounds/{rid}/responses", {"value": profiles["B"]})
                if ordinal == 1:
                    await b.resubscribe()
                    hydrated = await api.get(
                        f"/v1/rooms/{room_id}",
                        headers={"Authorization": "Bearer " + tokens["B"]},
                    )
                    hydrated.raise_for_status()
                    snapshot = hydrated.json()
                    state = reconcile(snapshot, b.events)
                    recovered = state["rounds"][rid]
                    assert profiles["A"] in id_list(recovered["submitted_profile_ids"])
                    assert recovered["phase"] == "answering" and recovered["reveal"] is None
                    assert profiles["A"] in id_list(
                        snapshot["current_round"]["submitted_profile_ids"]
                    )
                else:
                    for listener in [a, b]:
                        await listener.wait(
                            "rounds",
                            lambda row, rid=rid: (
                                row["id"] == rid
                                and id_list(row["submitted_profile_ids"]) == [profiles["A"]]
                            ),
                        )
                await a.wait(
                    "rounds",
                    lambda row, rid=rid: (
                        row["id"] == rid and profiles["A"] in id_list(row["submitted_profile_ids"])
                    ),
                )
                await command("B", f"/rounds/{rid}/responses", {"value": profiles["A"]})
                for listener in [a, b]:
                    complete = await listener.wait(
                        "rounds",
                        lambda row, rid=rid, profiles=profiles: (
                            row["id"] == rid
                            and set(id_list(row["submitted_profile_ids"]))
                            == {profiles["A"], profiles["B"]}
                        ),
                    )
                    assert complete["reveal"] is None
                    assert len(id_list(complete["submitted_profile_ids"])) == int(
                        complete["required_response_count"]
                    )
                reveal = await command("A", f"/rounds/{rid}/reveal")
                for listener in [a, b]:
                    observed = await listener.wait(
                        "rounds",
                        lambda row, rid=rid: row["id"] == rid and row["phase"] == "revealed",
                    )
                    assert observed["reveal"] == reveal["reveal"]
                    assert observed["reveal"]["correct_profile_id"]
                result = await command("A", f"/rounds/{rid}/advance")
                for listener in [a, b]:
                    await listener.wait(
                        "rounds",
                        lambda row, rid=rid: row["id"] == rid and row["phase"] == "complete",
                    )
                    await listener.wait(
                        "game_sessions",
                        lambda row, result=result, session_id=session_id: (
                            row["id"] == session_id
                            and row["revision"] == result["session"]["revision"]
                        ),
                    )
                    if ordinal < 3:
                        nxt = result["current_round"]["id"]
                        await listener.wait(
                            "rounds",
                            lambda row, nxt=nxt: row["id"] == nxt and row["phase"] == "answering",
                        )
                        await listener.wait(
                            "game_sessions",
                            lambda row, ordinal=ordinal, session_id=session_id: (
                                row["id"] == session_id
                                and int(row["current_round_ordinal"]) == ordinal + 1
                            ),
                        )
                    else:
                        await listener.wait(
                            "game_sessions",
                            lambda row, session_id=session_id: (
                                row["id"] == session_id and row["status"] == "complete"
                            ),
                        )
            event = await command("A", f"/rooms/{room_id}/messages", {"body": "Realtime verified"})
            for listener in [a, b]:
                observed = await listener.wait(
                    "timeline_events", lambda row: row["id"] == event["id"]
                )
                assert observed["payload"]["body"] == "Realtime verified"
            async with httpx.AsyncClient(timeout=15) as rest:
                for letter in "ABC":
                    headers = {"apikey": key, "Authorization": "Bearer " + tokens[letter]}
                    denied = await rest.get(
                        base + "/rest/v1/round_responses?select=*", headers=headers
                    )
                    assert denied.status_code in {401, 403}, "Private responses must deny SELECT"
                    secrets = await rest.get(
                        base + "/rest/v1/round_secrets?select=*", headers=headers
                    )
                    assert secrets.status_code in {401, 403, 404}, (
                        "Secrets must not be client-readable"
                    )
                response = await rest.get(
                    base + f"/rest/v1/rounds?room_id=eq.{room_id}&select=*",
                    headers={"apikey": key, "Authorization": "Bearer " + tokens["C"]},
                )
                assert response.status_code == 200 and response.json() == []
            await asyncio.sleep(2)
            leaked = [
                event for event in c.events + outsider.events if concerns_room(event, room_id)
            ]
            assert not leaked, "Unauthorized client received room data"
            assert not outsider.events, "Broad outsider subscription received rows"
            assert_public(a.events + b.events)
            print(
                "PASS: roster, three rounds, progress, reveal, advance, chat, "
                f"reconnect/hydrate, outsider isolation. Room {room_id}"
            )
            print("Fixture rows remain for inspection. No cleanup/reset was performed.")


def host_of(url):
    parsed = urlparse(url)
    return parsed.hostname, parsed.port


def local_supabase_containers():
    running = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    services = [
        line
        for line in running.stdout.splitlines()
        if line.startswith("supabase_") and "Backend" in line
    ]
    return running.returncode == 0, len(services)


def names_for(names, values):
    return [names.get(str(value), str(value)) for value in id_list(values)]


def choice_paths(value, choice, path="$"):
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(choice_paths(child, choice, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(choice_paths(child, choice, f"{path}[{index}]"))
    elif str(value) == str(choice):
        found.append(path)
    return found


def choice_is_private(record, choice):
    assert_no_secrets(record)
    paths = choice_paths(record, choice)
    return paths == [] or all(path.startswith("$.options") for path in paths)


def print_round(label, row, names):
    print(f"[{label}]")
    print(f"session_id={row.get('session_id')}")
    print(f"round_id={row['id']}")
    print(f"ordinal={row.get('ordinal')}")
    print(f"prompt={row['prompt']!r}")
    print(f"phase={row['phase']}")
    print(f"required_responses={row['required_response_count']}")
    print(f"submitted={names_for(names, row.get('submitted_profile_ids'))}")
    print(f"reveal={row.get('reveal')}")


def print_summary(checks):
    print()
    print("LOCAL SUPABASE REALTIME DEMO")
    print("--------------------------------")
    for label in [
        "A/B same round",
        "Submission progress sync",
        "Private answer hidden",
        "Simultaneous reveal",
        "Round advancement",
        "Chat synchronization",
        "C outsider isolation",
        "Reconnect/hydration",
    ]:
        print(f"{label:<31}{checks.get(label, 'FAIL')}")


async def database_summary():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        raise SystemExit("Set TEST_DATABASE_URL to the local Supabase database")
    require_local(dsn)
    host, port = host_of(dsn)
    docker_ok, service_count = local_supabase_containers()
    print("LOCAL DATABASE")
    print(f"supabase_containers={service_count} docker_ok={docker_ok}")
    print(f"database_host={host} port={port}")
    db = await asyncpg.connect(dsn)
    try:
        profiles = await db.fetch(
            "SELECT id, display_name FROM public.profiles ORDER BY display_name"
        )
        print("profiles:")
        for row in profiles:
            print(f"  {row['display_name']} id={row['id']}")
        bindings = await db.fetch(
            "SELECT u.email, p.display_name FROM public.demo_identities d "
            "JOIN auth.users u ON u.id=d.user_id JOIN public.profiles p ON p.id=d.profile_id "
            "WHERE u.email LIKE 'sync-player-%@example.test' ORDER BY u.email"
        )
        print("local_auth_bindings:")
        for row in bindings:
            print(f"  {row['email']} -> {row['display_name']}")
        rooms = await db.fetch(
            "SELECT id, name FROM public.rooms "
            "WHERE id IN ("
            "'20000000-0000-4000-8000-000000000001',"
            "'20000000-0000-4000-8000-000000000002') ORDER BY name"
        )
        print("seeded_rooms:")
        for room in rooms:
            print(f"  {room['name']} id={room['id']}")
            members = await db.fetch(
                "SELECT p.display_name, m.role::text AS role FROM public.room_members m "
                "JOIN public.profiles p ON p.id=m.profile_id "
                "WHERE m.room_id=$1 ORDER BY m.role DESC, p.display_name",
                room["id"],
            )
            for member in members:
                print(f"    {member['role']}: {member['display_name']}")
            messages = await db.fetch(
                "SELECT payload->>'body' AS body FROM public.timeline_events "
                "WHERE room_id=$1 AND event_type='message' ORDER BY created_at, id",
                room["id"],
            )
            for message in messages:
                print(f"    chat: {message['body']}")
        context_count = await db.fetchval("SELECT count(*) FROM public.group_context_items")
        print(f"group_context_items={context_count}")
        print("round secrets are created when a session starts and are not in this public summary")
    finally:
        await db.close()
    print()


async def demo():
    required = [
        "VERIFY_API_URL",
        "VERIFY_SUPABASE_URL",
        "VERIFY_PUBLISHABLE_KEY",
        "TEST_DATABASE_URL",
    ]
    required += [f"VERIFY_{kind}_{letter}" for kind in ["TOKEN", "PROFILE"] for letter in "ABC"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise SystemExit("Missing explicit configuration: " + ", ".join(missing))
    base = os.environ["VERIFY_SUPABASE_URL"].rstrip("/")
    api_url = os.environ["VERIFY_API_URL"]
    require_local(base)
    require_local(api_url)
    require_local(os.environ["TEST_DATABASE_URL"])
    supabase_host, supabase_port = host_of(base)
    api_host, api_port = host_of(api_url)
    print("TARGET CHECK")
    print(f"api={api_host}:{api_port} supabase={supabase_host}:{supabase_port} database=127.0.0.1")
    print()
    checks = {}
    try:
        await database_summary()
        key = os.environ["VERIFY_PUBLISHABLE_KEY"]
        tokens = {letter: os.environ[f"VERIFY_TOKEN_{letter}"] for letter in "ABC"}
        profiles = {letter: os.environ[f"VERIFY_PROFILE_{letter}"] for letter in "ABC"}
        db = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        try:
            rows = await db.fetch("SELECT id::text AS id, display_name FROM public.profiles")
        finally:
            await db.close()
        names = {row["id"]: row["display_name"] for row in rows}
        labels = {"A": "authorized player", "B": "authorized player", "C": "unauthorized outsider"}
        async with httpx.AsyncClient(base_url=api_url.rstrip("/"), timeout=20) as api:

            async def command(letter, path, body=None):
                response = await api.post(
                    "/v1" + path,
                    json=body or {},
                    headers={"Authorization": "Bearer " + tokens[letter]},
                )
                if response.is_error:
                    raise RuntimeError(f"{path} returned HTTP {response.status_code}")
                return response.json() if response.status_code != 204 else None

            async def hydrate(letter, room_id):
                response = await api.get(
                    f"/v1/rooms/{room_id}",
                    headers={"Authorization": "Bearer " + tokens[letter]},
                )
                if response.is_error:
                    raise RuntimeError(f"hydrate returned HTTP {response.status_code}")
                return response.json()

            for letter in "ABC":
                await command(letter, "/demo-sessions", {"profile_id": profiles[letter]})
            room = await command("A", "/rooms", {"name": "Visible realtime demo"})
            room_id = room["id"]
            print("REALTIME CLIENTS")
            print(f"demo_room={room['name']} id={room_id}")
            async with (
                Listener(base, key, tokens["A"], room_id) as listener_a,
                Listener(base, key, tokens["B"], room_id) as listener_b,
                Listener(base, key, tokens["C"], room_id) as listener_c,
                Listener(
                    base,
                    key,
                    tokens["C"],
                    room_id,
                    filtered=False,
                    topic="realtime:broad-outsider",
                ) as outsider,
            ):
                for letter, listener in [("A", listener_a), ("B", listener_b), ("C", listener_c)]:
                    print(
                        f"[{letter}] {labels[letter]} subscribed "
                        f"channel=room:{room_id} events={len(listener.events)}"
                    )
                print("[C] also subscribed with no room filter")
                await command("B", "/rooms/join", {"code": room["join_code"]})
                for letter, listener in [("A", listener_a), ("B", listener_b)]:
                    member = await listener.wait(
                        "room_members",
                        lambda row, pid=profiles["B"]: (
                            str(row["profile_id"]) == pid and row["left_at"] is None
                        ),
                    )
                    print(
                        f"[{letter} REALTIME] MEMBER JOIN "
                        f"{names[str(member['profile_id'])]} role={member['role']}"
                    )
                print()
                result = await command("A", f"/rooms/{room_id}/sessions", {"vibe": "chaos"})
                current = result["current_round"]
                rid = current["id"]
                observed = {}
                for letter, listener in [("A", listener_a), ("B", listener_b)]:
                    observed[letter] = await listener.wait(
                        "rounds",
                        lambda row, rid=rid: row["id"] == rid and row["phase"] == "answering",
                    )
                    print(f"[{letter} REALTIME] ROUND START")
                    print_round(f"{letter} REALTIME", observed[letter], names)
                same = (
                    observed["A"]["id"] == observed["B"]["id"]
                    and observed["A"]["prompt"] == observed["B"]["prompt"]
                    and observed["A"]["phase"] == observed["B"]["phase"] == "answering"
                )
                print(
                    f"[SAME ROUND] A and B received the same answering round: {'PASS' if same else 'FAIL'}"
                )
                checks["A/B same round"] = "PASS" if same else "FAIL"
                print()
                print("[PRE-REVEAL]")
                print(f"public reveal={observed['A']['reveal']}")
                print("correct answer unavailable to clients")
                print()
                print(f"[TEST DRIVER] A submits: {names[profiles['B']]!r}")
                await command("A", f"/rounds/{rid}/responses", {"value": profiles["B"]})
                progress = await listener_b.wait(
                    "rounds",
                    lambda row, rid=rid: (
                        row["id"] == rid
                        and id_list(row["submitted_profile_ids"]) == [profiles["A"]]
                    ),
                )
                remaining = int(progress["required_response_count"]) - len(
                    id_list(progress["submitted_profile_ids"])
                )
                print("[B REALTIME]")
                print(f"submitted_count={len(id_list(progress['submitted_profile_ids']))}")
                print(f"remaining={remaining}")
                print(f"submitted_players={names_for(names, progress['submitted_profile_ids'])}")
                print(f"phase={progress['phase']} reveal={progress['reveal']}")
                hidden = choice_is_private(progress, profiles["B"]) and progress["reveal"] is None
                print(f"[PRIVACY CHECK] B cannot see A answer: {'PASS' if hidden else 'FAIL'}")
                print()
                print(f"[TEST DRIVER] B submits: {names[profiles['A']]!r}")
                await command("B", f"/rounds/{rid}/responses", {"value": profiles["A"]})
                complete = {}
                for letter, listener in [("A", listener_a), ("B", listener_b)]:
                    complete[letter] = await listener.wait(
                        "rounds",
                        lambda row, rid=rid, profiles=profiles: (
                            row["id"] == rid
                            and set(id_list(row["submitted_profile_ids"]))
                            == {profiles["A"], profiles["B"]}
                            and row["reveal"] is None
                        ),
                    )
                    done = len(id_list(complete[letter]["submitted_profile_ids"]))
                    print(
                        f"[{letter} REALTIME] submitted_count={done} "
                        f"remaining={int(complete[letter]['required_response_count']) - done} "
                        f"reveal={complete[letter]['reveal']}"
                    )
                progress_ok = all(row["reveal"] is None for row in complete.values())
                checks["Submission progress sync"] = "PASS" if progress_ok else "FAIL"
                checks["Private answer hidden"] = "PASS" if hidden else "FAIL"
                print()
                print("[PRE-REVEAL]")
                print("public reveal=null / unavailable")
                print("correct answer unavailable to clients")
                reveal = await command("A", f"/rounds/{rid}/reveal")
                revealed = {}
                for letter, listener in [("A", listener_a), ("B", listener_b)]:
                    revealed[letter] = await listener.wait(
                        "rounds",
                        lambda row, rid=rid: row["id"] == rid and row["phase"] == "revealed",
                    )
                    public = revealed[letter]["reveal"]
                    print(f"[{letter} REALTIME] REVEAL")
                    print(f"round_id={revealed[letter]['id']} phase={revealed[letter]['phase']}")
                    print(f"correct_player={names.get(public['correct_profile_id'])}")
                    print(f"message={public['message']!r}")
                    for item in public["results"]:
                        print(
                            f"  {names.get(item['profile_id'])}: "
                            f"correct={item['correct']} points={item['points']}"
                        )
                reveal_ok = revealed["A"]["reveal"] == revealed["B"]["reveal"] == reveal["reveal"]
                print(
                    f"[SAME REVEAL] A and B received the same reveal: {'PASS' if reveal_ok else 'FAIL'}"
                )
                checks["Simultaneous reveal"] = "PASS" if reveal_ok else "FAIL"
                print()
                advanced = await command("A", f"/rounds/{rid}/advance")
                nxt = advanced["current_round"]
                for letter, listener in [("A", listener_a), ("B", listener_b)]:
                    await listener.wait(
                        "rounds",
                        lambda row, rid=rid: row["id"] == rid and row["phase"] == "complete",
                    )
                    opened = await listener.wait(
                        "rounds",
                        lambda row, nxt=nxt["id"]: row["id"] == nxt and row["phase"] == "answering",
                    )
                    print(f"[{letter} REALTIME] ADVANCE")
                    print("old round:")
                    print(f"  round_id={rid}")
                    print("  phase=complete")
                    print("new round:")
                    print(f"  round_id={opened['id']}")
                    print(f"  ordinal={opened['ordinal']}")
                    print("  phase=answering")
                    print(f"  prompt={opened['prompt']!r}")
                advance_ok = opened["prompt"] == nxt["prompt"] and int(opened["ordinal"]) == 2
                checks["Round advancement"] = "PASS" if advance_ok else "FAIL"
                print()
                chat = "Bring the backup Uno deck."
                print(f"[TEST DRIVER] A sends chat: {chat!r}")
                event = await command("A", f"/rooms/{room_id}/messages", {"body": chat})
                for letter, listener in [("A", listener_a), ("B", listener_b)]:
                    received = await listener.wait(
                        "timeline_events", lambda row, event=event: row["id"] == event["id"]
                    )
                    print(
                        f"[{letter} REALTIME] CHAT event_type={received['event_type']} "
                        f"body={received['payload']['body']!r}"
                    )
                checks["Chat synchronization"] = "PASS"
                print()
                before = await hydrate("B", room_id)
                before_round = before["current_round"]
                print("[B] state before disconnect")
                print(f"round_id={before_round['id']}")
                print(f"ordinal={before_round['ordinal']}")
                print(f"phase={before_round['phase']}")
                print(f"prompt={before_round['prompt']!r}")
                print(f"submitted_count={len(before_round['submitted_profile_ids'])}")
                await listener_b.close()
                print("[B] disconnected")
                print(f"[TEST DRIVER] while B is offline, A submits: {names[profiles['B']]!r}")
                await command(
                    "A", f"/rounds/{before_round['id']}/responses", {"value": profiles["B"]}
                )
                authoritative = await hydrate("A", room_id)
                changed = authoritative["current_round"]
                print("[OFFLINE CHANGE]")
                print(f"round_id={changed['id']}")
                print(f"phase={changed['phase']} reveal={changed['reveal']}")
                print(f"submitted_count={len(changed['submitted_profile_ids'])}")
                print(f"submitted_players={names_for(names, changed['submitted_profile_ids'])}")
                await listener_b.resubscribe()
                hydrated = await hydrate("B", room_id)
                recovered = reconcile(hydrated, listener_b.events)["rounds"][changed["id"]]
                print("[B] hydrated state after reconnect")
                print(f"round_id={recovered['id']}")
                print(f"ordinal={recovered['ordinal']}")
                print(f"phase={recovered['phase']}")
                print(f"prompt={recovered['prompt']!r}")
                print(f"submitted_count={len(id_list(recovered['submitted_profile_ids']))}")
                print(f"submitted_players={names_for(names, recovered['submitted_profile_ids'])}")
                print(f"reveal={recovered['reveal']}")
                caught_up = (
                    recovered["id"] == changed["id"]
                    and recovered["prompt"] == changed["prompt"]
                    and recovered["phase"] == changed["phase"]
                    and set(id_list(recovered["submitted_profile_ids"]))
                    == set(changed["submitted_profile_ids"])
                    and recovered["reveal"] is None
                )
                print(
                    f"[RECONNECT] B matches authoritative state: {'PASS' if caught_up else 'FAIL'}"
                )
                checks["Reconnect/hydration"] = "PASS" if caught_up else "FAIL"
                await asyncio.sleep(2)
                leaked = [
                    event
                    for event in listener_c.events + outsider.events
                    if concerns_room(event, room_id)
                ]
                print()
                print(f"[C] unauthorized protected-room events received: {len(leaked)}")
                isolated = not leaked and not outsider.events
                print(f"[OUTSIDER ISOLATION] {'PASS' if isolated else 'FAIL'}")
                checks["C outsider isolation"] = "PASS" if isolated else "FAIL"
                assert_public(listener_a.events + listener_b.events)
    finally:
        print_summary(checks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Explicitly authorize writes to configured test environment",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Print a human-readable local Realtime lifecycle; local targets only",
    )
    args = parser.parse_args()
    if args.demo == args.run:
        parser.error("Choose one of --run or --demo for an authorized local environment")
    asyncio.run(demo() if args.demo else main())
