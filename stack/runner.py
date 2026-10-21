"""Run recipe commands on the head Spark over ssh, with logs, one job at a time."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable

HOME = os.environ.get("HOME", "/root")
SSH_KEY = os.environ.get("STACK_SSH_KEY", f"{HOME}/.ssh/id_ed25519")
SSH_USER = os.environ.get("STACK_SSH_USER", "")
KNOWN_HOSTS = os.environ.get("STACK_SSH_KNOWN_HOSTS", f"{HOME}/.ssh/known_hosts")


def ssh_argv(target: str) -> list[str]:
    """ssh options that work when the container runs as root: the host's key and user, given explicitly."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-i", SSH_KEY, "-o", "IdentitiesOnly=yes"]
    # Only fill in a user when the target does not name one: -o User= would override valentine@10.10.10.13.
    if SSH_USER and "@" not in target:
        argv += ["-o", f"User={SSH_USER}"]
    return argv + [target]


@dataclass
class Job:
    key: str
    action: str
    started: float
    log: str
    pid: int = 0
    rc: int | None = None
    done: bool = False


class Host:
    """The head Spark, reached over ssh from inside the controller container."""

    def __init__(self, target: str, state_dir: Path, window: int = 900):
        self.target = target
        self.state_dir = state_dir
        self.window = window
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- ssh
    def run(self, command: str, timeout: int = 60, stdin: str | None = None,
            target: str | None = None) -> tuple[int, str]:
        argv = ssh_argv(target or self.target) + [f"timeout {timeout} bash -lc {shquote(command)}"]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 20,
                               input=stdin)
            return p.returncode, (p.stdout + p.stderr).strip()
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {timeout}s"

    def spawn(self, key: str, action: str, command: str, timeout: int = 7200) -> Job:
        log_dir = self.state_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / f"{key}.{action}.{int(time.time())}.log"
        argv = ssh_argv(self.target) + [f"timeout {timeout} bash -lc {shquote(command)}"]
        fh = log.open("w")
        proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        job = Job(key=key, action=action, started=time.time(), log=str(log), pid=proc.pid)
        with self._lock:
            self.jobs[key] = job

        def reap() -> None:
            rc = proc.wait()
            fh.close()
            job.rc, job.done = rc, True

        threading.Thread(target=reap, daemon=True).start()
        return job

    def job(self, key: str) -> Job | None:
        job = self.jobs.get(key)
        if job and not job.done and job.pid:
            # A job whose ssh died under us (container restart, killed ssh) should stop looking alive.
            try:
                os.kill(job.pid, 0)
            except OSError:
                job.done, job.rc = True, job.rc if job.rc is not None else -1
        return job

    def cancel(self, key: str) -> None:
        """Stop a running job's ssh (its remote command gets SIGHUP); the recipe's stop.sh cleans up the rest."""
        job = self.job(key)
        if job and not job.done and job.pid:
            try:
                os.kill(job.pid, 15)
            except OSError:
                pass

    def tail(self, log_path: str, lines: int = 60) -> list[str]:
        try:
            return Path(log_path).read_text(errors="replace").splitlines()[-lines:]
        except OSError:
            return []


def shquote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"
