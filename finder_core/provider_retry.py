from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import time
from pathlib import Path
from typing import Any

from .events import redact_text, utc_now

MAX_TRANSPORT_ATTEMPTS = 6
MAX_TRANSPORT_SECONDS = 15 * 60


def error_details(exc: Exception, *, secrets=()) -> dict[str, Any]:
    return {"error_type": type(exc).__name__, "message": redact_text(str(exc), (*secrets, os.getenv("OPENAI_API_KEY", ""))), "status_code": getattr(exc, "status_code", None), "request_id": getattr(exc, "request_id", None), "code": getattr(exc, "code", None), "param": getattr(exc, "param", None)}


def retryable(exc: Exception) -> bool:
    from openai import APIConnectionError, APIStatusError
    if getattr(exc, "code", None) in {"insufficient_quota", "billing_hard_limit_reached"}:
        return False
    return isinstance(exc, APIConnectionError) or (isinstance(exc, APIStatusError) and (exc.status_code in {408, 409, 429} or exc.status_code >= 500))


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def create_response(create: Any, *, log_path: Path | None, stage: str, agent_id: str, secrets=(), **body: Any) -> Any:
    started = time.monotonic()
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    for attempt in range(1, MAX_TRANSPORT_ATTEMPTS + 1):
        event = {"at_utc": utc_now(), "stage": stage, "agent_id": agent_id, "model": body["model"], "attempt": attempt, "request_sha256": fingerprint}
        if log_path is not None:
            append_event(log_path, {**event, "status": "request_started"})
        try:
            response = create(**body)
        except Exception as exc:
            elapsed = time.monotonic() - started
            recover = retryable(exc) and attempt < MAX_TRANSPORT_ATTEMPTS and elapsed < MAX_TRANSPORT_SECONDS
            delay = min(300.0, 5.0 * 2 ** min(attempt - 1, 6)) + random.uniform(0, 3)
            headers = getattr(getattr(exc, "response", None), "headers", {})
            try:
                delay = max(delay, float(headers.get("retry-after", 0)))
            except (TypeError, ValueError):
                pass
            delay = min(delay, max(0, MAX_TRANSPORT_SECONDS - elapsed))
            if log_path is not None:
                append_event(log_path, {**event, "at_utc": utc_now(), "status": "retry_wait" if recover else "failed", "elapsed_seconds": round(elapsed, 3), "wait_seconds": delay if recover else 0, **error_details(exc, secrets=secrets)})
            if not recover:
                raise

            remaining = delay
            while remaining > 0:
                step = min(30, remaining)
                time.sleep(step)
                remaining -= step
            continue
        if log_path is not None:
            usage = getattr(response, "usage", None)
            append_event(log_path, {**event, "at_utc": utc_now(), "status": "returned", "response_status": getattr(response, "status", None), "request_id": getattr(response, "_request_id", None), "usage": usage.model_dump() if usage is not None else None})
        return response
    raise RuntimeError("Provider recovery budget exhausted.")
