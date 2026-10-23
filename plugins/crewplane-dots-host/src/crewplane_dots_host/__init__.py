"""A local, explicit transport. The active host invokes models."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import signal
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any


class Queue:
    """Transactional fencing; expired work fails closed, never auto-dispatches twice."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        # Core TERM grace is 250ms; never block signal delivery for seconds.
        self.db = sqlite3.connect(root / "queue.sqlite3", timeout=0.05)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, state TEXT NOT NULL, request TEXT NOT NULL,
                token TEXT, lease REAL, receipt TEXT)""")
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def submit(
        self,
        prompt: str,
        model: str | None,
        reasoning: str | None,
        timeout: float = 600,
        job_id: str | None = None,
    ) -> str:
        self._duration(timeout)
        job_id = job_id or str(uuid.uuid4())
        request = {
            "version": 1,
            "id": job_id,
            "prompt": prompt,
            "working_directory": str(Path.cwd().resolve()),
            "requested": {"model": model, "reasoning": reasoning},
            "created_at": time.time(),
            "expires_at": time.time() + timeout,
        }
        with self.db:
            self.db.execute(
                "INSERT INTO jobs (id,state,request) VALUES (?,?,?)",
                (job_id, "pending", json.dumps(request)),
            )
        return job_id

    def inspect(self, job_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM jobs" + (" WHERE id=?" if job_id else ""),
            (job_id,) if job_id else (),
        ).fetchall()
        return [
            {
                **dict(row),
                "state": "cancelled"
                if self._marker(row["id"]).exists()
                else row["state"],
                "request": json.loads(row["request"]),
                "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            }
            for row in rows
        ]

    def claim(self, job_id: str, seconds: float) -> dict[str, Any]:
        self._duration(seconds)
        self.reconcile()
        token = str(uuid.uuid4())
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._assert_not_cancelled(job_id)
            result = self.db.execute(
                "UPDATE jobs SET state='claimed',token=?,lease=? WHERE id=? AND state='pending' AND json_extract(request, '$.expires_at')>?",
                (token, time.time() + seconds, job_id, time.time()),
            )
            if result.rowcount != 1:
                raise ValueError("Job is absent or already claimed/terminal")
        return self.inspect(job_id)[0]

    @staticmethod
    def _duration(seconds: float) -> None:
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Duration must be finite and positive")

    def renew(self, job_id: str, token: str, seconds: float) -> None:
        self._duration(seconds)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._assert_not_cancelled(job_id)
            result = self.db.execute(
                "UPDATE jobs SET lease=? WHERE id=? AND token=? AND state='claimed' AND lease>? AND json_extract(request, '$.expires_at')>?",
                (time.time() + seconds, job_id, token, time.time(), time.time()),
            )
            if result.rowcount != 1:
                raise ValueError("Stale or invalid lease")

    def complete(self, job_id: str, token: str, receipt: dict[str, Any]) -> None:
        # A host attestation is evidence, not a provider billing/API attestation.
        if not isinstance(receipt, dict) or set(receipt) != {
            "output",
            "host_agent_id",
            "confirmed",
            "evidence",
        }:
            raise ValueError(
                "Receipt requires output, host_agent_id, confirmed, evidence"
            )
        if not all(
            isinstance(receipt[k], str) and receipt[k].strip()
            for k in ("output", "host_agent_id", "evidence")
        ):
            raise ValueError("Receipt text fields must be nonempty strings")
        confirmed = receipt["confirmed"]
        if not isinstance(confirmed, dict) or set(confirmed) != {"model", "reasoning"}:
            raise ValueError(
                "Confirmed model/reasoning must be explicit, null if unknown"
            )
        if any(v is not None and not isinstance(v, str) for v in confirmed.values()):
            raise ValueError("Invalid confirmed model/reasoning")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._assert_not_cancelled(job_id)
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown job")
            encoded = json.dumps(receipt, sort_keys=True)
            if (
                row["state"] == "completed"
                and row["token"] == token
                and row["receipt"] == encoded
            ):
                return  # Exact replay is idempotent; divergent duplicate is rejected.
            if (
                row["state"] != "claimed"
                or row["token"] != token
                or row["lease"] <= time.time()
            ):
                raise ValueError("Stale, cancelled, or invalid completion")
            requested = json.loads(row["request"])["requested"]
            if any(
                value is not None and value != confirmed[key]
                for key, value in requested.items()
            ):
                raise ValueError(
                    "Requested configuration not confirmed; no fallback permitted"
                )
            result = self.db.execute(
                "UPDATE jobs SET state='completed',receipt=? WHERE id=? AND state='claimed' AND token=? AND lease>? AND json_extract(request, '$.expires_at')>?",
                (encoded, job_id, token, time.time(), time.time()),
            )
            if result.rowcount != 1:
                raise ValueError("Completion lost lease")
            self._assert_not_cancelled(job_id)
        self._assert_not_cancelled(job_id)

    def mark_cancelled(self, job_id: str) -> None:
        self._marker(job_id).touch(exist_ok=True)

    def _marker(self, job_id: str) -> Path:
        return self.root / (hashlib.sha256(job_id.encode()).hexdigest() + ".cancelled")

    def _assert_not_cancelled(self, job_id: str) -> None:
        if self._marker(job_id).exists():
            raise ValueError("Job cancelled")

    def cancel(self, job_id: str) -> None:
        rows = self.inspect(job_id)
        if rows and rows[0]["state"] == "completed":
            return
        self.mark_cancelled(job_id)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE id=? AND state IN ('pending','claimed')",
                (job_id,),
            )

    def reconcile(self) -> int:
        with self.db:
            for row in self.db.execute(
                "SELECT id FROM jobs WHERE state IN ('pending','claimed','completed')"
            ).fetchall():
                if self._marker(row["id"]).exists():
                    self.db.execute(
                        "UPDATE jobs SET state='cancelled' WHERE id=?", (row["id"],)
                    )
            result = self.db.execute(
                "UPDATE jobs SET state='expired' WHERE (state='claimed' AND lease<=?) OR (state IN ('pending','claimed') AND json_extract(request, '$.expires_at')<=?)",
                (time.time(), time.time()),
            )
        return result.rowcount


def invoke(
    queue: Queue, args: argparse.Namespace, deadline: float | None = None
) -> int:
    Queue._duration(args.timeout)
    if deadline is None:
        deadline = time.monotonic() + args.timeout
    job_id = str(uuid.uuid4())
    submitted = False
    completed = False

    def cancelled(signum: int, frame: Any) -> None:
        del frame
        queue.mark_cancelled(job_id)
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    try:
        prompt = _read_prompt(deadline)
        while (remaining := deadline - time.monotonic()) > 0:
            queue._assert_not_cancelled(job_id)
            try:
                if not submitted:
                    queue.submit(prompt, args.model, args.reasoning, remaining, job_id)
                    submitted = True
                    print(f"dots-host job {job_id}", file=sys.stderr, flush=True)
                queue.reconcile()
                job = queue.inspect(job_id)[0]
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode & 0xFF not in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }:
                    raise
                time.sleep(0.1)
                continue
            if job["state"] == "completed":
                queue._assert_not_cancelled(job_id)
                print(job["receipt"]["output"], flush=True)
                completed = True
                return 0
            if job["state"] in {"cancelled", "expired"}:
                raise ValueError(
                    f"Dots host job {job['state']}; inspect before retrying"
                )
            time.sleep(0.1)
        raise ValueError(
            "Dots host timed out; job cancelled, running host must stop/reconcile"
        )
    finally:
        if not completed:
            queue.mark_cancelled(job_id)


def _read_prompt(deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Dots host timed out while reading stdin")

    def timed_out(signum: int, frame: Any) -> None:
        del signum, frame
        raise TimeoutError("Dots host timed out while reading stdin")

    previous_handler = signal.signal(signal.SIGALRM, timed_out)
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining)
        return sys.stdin.read()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _execute_command(args: argparse.Namespace, deadline: float) -> int:
    with closing(Queue(args.queue)) as queue:
        match args.command:
            case "invoke":
                return invoke(queue, args, deadline)
            case "list":
                print(json.dumps(queue.inspect(), indent=2))
            case "claim":
                print(json.dumps(queue.claim(args.id, args.lease_seconds), indent=2))
            case "renew":
                queue.renew(args.id, args.token, args.lease_seconds)
            case "complete":
                queue.complete(
                    args.id, args.token, json.loads(args.receipt.read_text())
                )
            case "cancel":
                queue.cancel(args.id)
            case "reconcile":
                print(queue.reconcile())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--queue", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("invoke", allow_abbrev=False)
    run.add_argument("--model")
    run.add_argument("--reasoning")
    run.add_argument("--timeout", type=float, default=600)
    commands.add_parser("list", allow_abbrev=False)
    commands.add_parser("reconcile", allow_abbrev=False)
    for name in ("claim", "renew", "complete", "cancel"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("id")
        if name in {"renew", "complete"}:
            command.add_argument("--token", required=True)
        if name in {"claim", "renew"}:
            command.add_argument("--lease-seconds", type=float, default=300)
        if name == "complete":
            command.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        timeout = 0.0
        if args.command == "invoke":
            Queue._duration(args.timeout)
            timeout = args.timeout
        elif args.command == "cancel":
            timeout = 5.0
        deadline = time.monotonic() + timeout
        while True:
            try:
                return _execute_command(args, deadline)
            except sqlite3.OperationalError as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or error.sqlite_errorcode & 0xFF not in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }:
                    raise
                time.sleep(min(0.1, remaining))
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 1
