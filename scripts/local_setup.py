"""Configure ONLY a running local Supabase stack; write ignored local credentials.

Run with .venv/bin/python scripts/local_setup.py after supabase start/reset.
No remote hosts, hardcoded passwords, or committed tokens are used.
"""

import asyncio
import json
import os
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[1]


def require_local(url):
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Refusing non-local Supabase configuration")


def private_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


async def setup():
    # Capture status: it includes secrets and must never be printed.
    status = subprocess.run(  # noqa: ASYNC221
        ["supabase", "status", "-o", "json"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if status.returncode:
        raise RuntimeError("Local Supabase status failed; run supabase start first")
    cfg = json.loads(status.stdout)
    url, db_url = cfg["API_URL"], cfg["DB_URL"]
    require_local(url)
    require_local(db_url)
    folder = ROOT / ".local"
    folder.mkdir(exist_ok=True, mode=0o700)
    credentials_file = folder / "auth-passwords.json"
    passwords = json.loads(credentials_file.read_text()) if credentials_file.exists() else {}
    for letter in "ABC":
        passwords.setdefault(letter, secrets.token_urlsafe(32))
    private_write(credentials_file, json.dumps(passwords))
    tokens, users = {}, {}
    async with httpx.AsyncClient(base_url=url, timeout=30) as auth:
        admin = {
            "apikey": cfg["SERVICE_ROLE_KEY"],
            "Authorization": "Bearer " + cfg["SERVICE_ROLE_KEY"],
        }
        listed = await auth.get("/auth/v1/admin/users", headers=admin)
        if listed.status_code != 200:
            raise RuntimeError("Local Auth admin lookup failed")
        existing = {u["email"]: u for u in listed.json()["users"]}
        for letter in "ABC":
            email = f"sync-player-{letter.lower()}@example.test"
            if email not in existing:
                response = await auth.post(
                    "/auth/v1/admin/users",
                    headers=admin,
                    json={"email": email, "password": passwords[letter], "email_confirm": True},
                )
                if response.status_code not in {200, 201}:
                    raise RuntimeError(f"Local Auth creation failed for fictional player {letter}")
            response = await auth.post(
                "/auth/v1/token?grant_type=password",
                headers={"apikey": cfg["ANON_KEY"]},
                json={"email": email, "password": passwords[letter]},
            )
            if response.status_code != 200:
                raise RuntimeError(f"Local Auth sign-in failed for fictional player {letter}")
            tokens[letter] = response.json()["access_token"]
            users[letter] = UUID(response.json()["user"]["id"])
    profiles = {
        letter: UUID(f"10000000-0000-4000-8000-{i:012d}") for i, letter in enumerate("ABC", 1)
    }
    db = await asyncpg.connect(db_url)
    try:
        async with db.transaction():
            for letter in "ABC":
                await db.execute(
                    "INSERT INTO public.demo_identities(user_id,profile_id) VALUES($1,$2) "
                    "ON CONFLICT DO NOTHING",
                    users[letter],
                    profiles[letter],
                )
                bound = await db.fetchval(
                    "SELECT profile_id FROM public.demo_identities WHERE user_id=$1", users[letter]
                )
                if bound != profiles[letter]:
                    raise RuntimeError(
                        "Local identity binding conflict; inspect fictional seed state"
                    )
    finally:
        await db.close()
    issuer = url.rstrip("/") + "/auth/v1"
    backend = {
        "DATABASE_URL": db_url,
        "SUPABASE_JWT_ISSUER": issuer,
        # Current local Supabase signs access tokens with ES256. JWKS takes
        # precedence over the legacy HS256 secret in app.auth.
        "SUPABASE_JWT_SECRET": cfg["JWT_SECRET"],
        "SUPABASE_JWKS_URL": issuer + "/.well-known/jwks.json",
        "ALLOWED_ORIGINS": "http://localhost:8081,http://localhost:3000",
    }
    verification = {
        "TEST_DATABASE_URL": db_url,
        "VERIFY_API_URL": "http://127.0.0.1:8000",
        "VERIFY_SUPABASE_URL": url,
        "VERIFY_PUBLISHABLE_KEY": cfg["ANON_KEY"],
    }
    for letter in "ABC":
        verification[f"VERIFY_TOKEN_{letter}"] = tokens[letter]
        verification[f"VERIFY_PROFILE_{letter}"] = str(profiles[letter])
    # Refuse to replace a configuration for any other target.
    if (ROOT / ".env").exists():
        from dotenv import dotenv_values

        prior = dotenv_values(ROOT / ".env").get("DATABASE_URL")
        if prior:
            require_local(prior)
            if prior != db_url:
                raise RuntimeError(
                    "Existing .env points to another local database; refusing overwrite"
                )
    for name, data in [(".env", backend), (".env.verify", verification)]:
        private_write(
            ROOT / name, "\n".join(f"{key}={value}" for key, value in data.items()) + "\n"
        )
    print("PASS: local Auth identities A/B/C configured; .env and .env.verify saved privately.")


if __name__ == "__main__":
    try:
        asyncio.run(setup())
    except Exception as error:  # noqa: BLE001
        # Network/SQL exception messages can include credentials; expose type only.
        print(
            f"Local setup failed ({type(error).__name__}); inspect the local stack/configuration."
        )
        raise SystemExit(1) from None
