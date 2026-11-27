"""Root-side job runner for the panel's "server" tab (systemd unit telec-ops).

Picks queued jobs from ops_jobs one at a time, rebuilds the commands from the
whitelist in app/ops/jobs.py, runs them (as the telec user unless the step needs
root), streams output into the database and honours cancel requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import signal
import time
from pathlib import Path

from app.config import load_settings
from app.data.store import Store
from app.ops.jobs import KINDS, RUNNER, InvalidParams, build_steps, restarts_runner

log = logging.getLogger("telec.ops")
APP_DIR = str(Path(__file__).resolve().parents[2])
RUN_USER = os.environ.get("OPS_RUN_USER", "telec")
MAX_OUTPUT = 300_000          # characters kept per job (the tail)
FLUSH_S = 1.5


class Output:
    def __init__(self):
        self.text = ""
        self.dirty = False

    def add(self, s: str) -> None:
        self.text = (self.text + s)[-MAX_OUTPUT:]
        self.dirty = True


class Runner:
    def __init__(self, store: Store, alerter=None):
        self.store = store
        self.alerter = alerter
        self.stop = asyncio.Event()
        try:
            pw = pwd.getpwnam(RUN_USER)
            self.uid, self.gid, self.home = pw.pw_uid, pw.pw_gid, pw.pw_dir
        except KeyError:
            self.uid = self.gid = None
            self.home = APP_DIR

    async def _flush(self, job_id: int, out: Output) -> bool:
        """Save output; return True if a cancel was requested."""
        async with self.store.pool.acquire() as con:
            await con.execute("UPDATE app_settings SET value=$1 WHERE key='ops_heartbeat'", str(time.time()))
            if out.dirty:
                out.dirty = False
                return bool(await con.fetchval(
                    "UPDATE ops_jobs SET output=$2, updated_at=now() WHERE id=$1 RETURNING cancel_requested", job_id, out.text))
            return bool(await con.fetchval("SELECT cancel_requested FROM ops_jobs WHERE id=$1", job_id))

    async def _run_step(self, job_id: int, step, out: Output, deadline: float) -> int:
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
               "PYTHONUNBUFFERED": "1", "HOME": "/root" if step.as_root else self.home}
        kw = {}
        if not step.as_root and self.uid is not None and os.geteuid() == 0:
            kw = {"user": self.uid, "group": self.gid, "extra_groups": []}
        out.add(f"\n$ {' '.join(step.argv)}\n")
        proc = await asyncio.create_subprocess_exec(
            *step.argv, cwd=APP_DIR, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True, **kw)

        async def pump():
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                out.add(chunk.decode("utf-8", "replace"))

        reader = asyncio.create_task(pump())
        reason = None
        while not reader.done():
            await asyncio.wait({reader}, timeout=FLUSH_S)
            cancel = await self._flush(job_id, out)
            if cancel or self.stop.is_set():
                reason = "لغو شد"
            elif time.time() > deadline:
                reason = "زمان مجاز تمام شد"
            if reason:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10)
                        break
                    except asyncio.TimeoutError:
                        continue
                break
        if not reader.done():
            try:
                await asyncio.wait_for(reader, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                reader.cancel()
        code = await proc.wait()
        if reason:
            out.add(f"\n[{reason}]\n")
            raise asyncio.CancelledError(reason)
        return code

    async def run_job(self, job: dict) -> None:
        job_id, key = job["id"], job["kind"]
        out = Output()
        status, code, restart = "failed", None, False
        try:
            kind = KINDS[key]
            params = kind.clean(json.loads(job["params"]) if isinstance(job["params"], str) else job["params"])
            restart = restarts_runner(key, params)
            deadline = time.time() + kind.timeout_s
            for step in build_steps(key, params, APP_DIR):
                code = await self._run_step(job_id, step, out, deadline)
                if code != 0:
                    out.add(f"\n[خطا: کد خروج {code}]\n")
                    break
            else:
                status = "done"
        except asyncio.CancelledError:
            status = "cancelled"
        except (KeyError, InvalidParams) as exc:
            out.add(f"[کار نامعتبر: {exc}]\n")
        except Exception as exc:
            log.exception("job %s failed", job_id)
            out.add(f"\n[خطای اجرا: {exc}]\n")
        async with self.store.pool.acquire() as con:
            await con.execute("UPDATE ops_jobs SET status=$2, exit_code=$3, output=$4, finished_at=now(), updated_at=now() WHERE id=$1",
                              job_id, status, code, out.text)
        log.info("job %s (%s) %s", job_id, key, status)
        if restart and status == "done":
            log.info("restarting the runner itself")
            await asyncio.create_subprocess_exec("systemctl", "restart", "--no-block", RUNNER)

    async def loop(self) -> None:
        async with self.store.pool.acquire() as con:
            await con.execute("""UPDATE ops_jobs SET status='failed', finished_at=now(),
                                 output=coalesce(output,'') || E'\n[سرویس عملیات ری‌استارت شد و این کار نیمه‌کاره ماند]'
                                 WHERE status='running'""")
        while not self.stop.is_set():
            async with self.store.pool.acquire() as con:
                await con.execute("""INSERT INTO app_settings (key, value) VALUES ('ops_heartbeat', $1)
                                     ON CONFLICT (key) DO UPDATE SET value=$1""", str(time.time()))
                job = await con.fetchrow(
                    """UPDATE ops_jobs SET status='running', started_at=now(), updated_at=now()
                       WHERE id=(SELECT id FROM ops_jobs WHERE status='queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
                       RETURNING id, kind, params, requested_ip,
                                 (SELECT username FROM users WHERE id=requested_by) AS username""")
                if job is None:
                    # a cancel on a queued job just closes it
                    await con.execute("UPDATE ops_jobs SET status='cancelled', finished_at=now() WHERE status='queued' AND cancel_requested")
            if job is None:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                continue
            job = dict(job)
            if self.alerter is not None:
                await self.alerter.send(f"ops{job['id']}", f"عملیات سرور #{job['id']} ({job['kind']}) شروع شد — "
                                        f"کاربر {job['username']} از IP {job['requested_ip']}", force=True)
            await self.run_job(job)


async def main() -> None:
    cfg = load_settings()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store(cfg.database_url)
    await store.connect()
    await store.apply_schema()
    from app.alerts import AdminAlerter

    r = Runner(store, AdminAlerter(cfg.admin_bot_token, cfg.admin_chat_id))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, r.stop.set)
    log.info("ops runner started in %s (jobs run as %s)", APP_DIR, RUN_USER)
    try:
        await r.loop()
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
