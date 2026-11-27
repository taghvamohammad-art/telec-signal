"""Panel users.

  .venv/bin/python scripts/manage_users.py add USERNAME [--admin]   # asks for the password
  .venv/bin/python scripts/manage_users.py passwd USERNAME
  .venv/bin/python scripts/manage_users.py disable USERNAME
  .venv/bin/python scripts/manage_users.py enable USERNAME
  .venv/bin/python scripts/manage_users.py list
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.data.store import Store  # noqa: E402
from app.web.auth import hash_password  # noqa: E402


def ask_password() -> str:
    while True:
        p1 = getpass.getpass("رمز (حداقل ۸ کاراکتر): ")
        if len(p1) < 8:
            print("رمز کوتاه است.")
            continue
        if p1 != getpass.getpass("تکرار رمز: "):
            print("دو رمز یکی نیستند.")
            continue
        return p1


async def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"add", "passwd", "disable", "enable", "list"}:
        print(__doc__)
        return 1
    cmd = argv[0]
    store = Store(load_settings().database_url)
    await store.connect(attempts=1)
    await store.apply_schema()
    try:
        async with store.pool.acquire() as con:
            if cmd == "list":
                for r in await con.fetch("SELECT username, is_admin, active, created_at FROM users ORDER BY id"):
                    print(f"{r['username']:<20} {'admin' if r['is_admin'] else 'user':<6} {'active' if r['active'] else 'disabled':<9} {r['created_at']:%Y-%m-%d}")
                return 0
            if len(argv) < 2:
                print(__doc__)
                return 1
            name = argv[1].strip()
            if cmd == "add":
                if await con.fetchval("SELECT 1 FROM users WHERE username=$1", name):
                    print("این نام کاربری وجود دارد.")
                    return 1
                await con.execute("INSERT INTO users (username, password_hash, is_admin) VALUES ($1,$2,$3)",
                                  name, hash_password(ask_password()), "--admin" in argv)
                print(f"کاربر {name} ساخته شد.")
            elif cmd == "passwd":
                res = await con.execute("UPDATE users SET password_hash=$2 WHERE username=$1", name, hash_password(ask_password()))
                await con.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=$1)", name)
                print("رمز عوض شد." if res.endswith("1") else "کاربر پیدا نشد.")
            else:
                res = await con.execute("UPDATE users SET active=$2 WHERE username=$1", name, cmd == "enable")
                if cmd == "disable":
                    await con.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=$1)", name)
                print("انجام شد." if res.endswith("1") else "کاربر پیدا نشد.")
        return 0
    finally:
        await store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
