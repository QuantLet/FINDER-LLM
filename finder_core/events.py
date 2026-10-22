from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "openai_api_key",
    "token",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_text(value: object, secrets: Iterable[str] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _KEY_PATTERN.sub("[REDACTED]", text)
    return text


def redact_tree(value: Any, secrets: Iterable[str] = ()) -> Any:


    secret_values = tuple(item for item in secrets if item)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_NAMES:
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = redact_tree(item, secret_values)
        return cleaned
    if isinstance(value, list):
        return [redact_tree(item, secret_values) for item in value]
    if isinstance(value, tuple):
        return [redact_tree(item, secret_values) for item in value]
    if isinstance(value, str):
        return redact_text(value, secret_values)
    return value


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:

    content = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n").encode("utf-8")
    _write_bytes_atomic(Path(path), content)


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class TraceRecorder:


    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._records: list[dict[str, Any]] = []
        if self.path is not None and self.path.is_file():
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError("Call audit has an invalid JSON line.") from exc
                    if not isinstance(record, dict) or not isinstance(record.get("call_id"), int):
                        raise RuntimeError("Call audit record is invalid.")
                    self._records.append(record)
        self._next_id = max((int(item["call_id"]) for item in self._records), default=0) + 1
        self._lock = threading.Lock()

    def reserve_id(self) -> int:
        with self._lock:
            call_id = self._next_id
            self._next_id += 1
            return call_id

    def append(self, record: dict[str, Any]) -> None:
        with self._lock:
            clean = redact_tree(record)
            if any(int(item["call_id"]) == int(clean["call_id"]) for item in self._records):
                raise RuntimeError(f"Duplicate call_id {clean['call_id']} in call audit.")
            records = [*self._records, clean]
            if self.path is not None:
                content = "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in records).encode("utf-8")
                _write_bytes_atomic(self.path, content)
            self._records = records

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (dict(record) for record in self._records),
                key=lambda item: int(item["call_id"]),
            )


class AgentEventLogger:


    def __init__(
        self,
        path: Path,
        *,
        default_window_no: int = 1,
        default_attempt: int = 1,
        default_phase: str = "paper_workflow",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.default_window_no = default_window_no
        self.default_attempt = default_attempt
        self.default_phase = default_phase
        self._sequence = self._last_sequence()

    def _last_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        maximum = 0
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                        sequence = int(record.get("sequence", 0))
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        raise RuntimeError("Progress event audit has an invalid JSON line.") from exc
                    maximum = max(maximum, sequence)
        except OSError:
            return 0
        return maximum

    def emit(
        self,
        event_type: str,
        *,
        run_id: str,
        window_id: str,
        agent: str | None = None,
        agent_id: str | None = None,
        activity_id: str | None = None,
        invocation_id: str | None = None,
        stage: str | None = None,
        stage_group: str | None = None,
        model: str | None = None,
        status: str | None = None,
        summary: Any = None,
        error: Any = None,
        tool_name: str | None = None,
        tool_request_id: str | None = None,
        tool_receipt_id: str | None = None,
        selected_count: int | None = None,
        completed_count: int | None = None,
        total_count: int | None = None,
        snapshot_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        window_no: int | None = None,
        attempt: int | None = None,
        phase: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            next_sequence = self._sequence + 1
            resolved_agent = agent_id or agent
            record = redact_tree(
                {
                    "event_id": f"E{next_sequence:06d}",
                    "sequence": next_sequence,
                    "timestamp_utc": utc_now(),
                    "run_id": run_id,
                    "window_id": window_id,
                    "window_no": (
                        self.default_window_no
                        if window_no is None
                        else window_no
                    ),
                    "attempt": (
                        self.default_attempt if attempt is None else attempt
                    ),
                    "phase": self.default_phase if phase is None else phase,
                    "event_type": event_type,
                    "agent": resolved_agent,
                    "agent_id": resolved_agent,
                    "activity_id": activity_id,
                    "invocation_id": invocation_id,
                    "stage": stage,
                    "stage_group": stage_group,
                    "model": model,
                    "status": status,
                    "summary": summary,
                    "error": error,
                    "tool_name": tool_name,
                    "tool_request_id": tool_request_id,
                    "tool_receipt_id": tool_receipt_id,
                    "selected_count": selected_count,
                    "completed_count": completed_count,
                    "total_count": total_count,
                    "snapshot_hash": snapshot_hash,
                    "metadata": dict(metadata or {}),
                }
            )
            existing = self.path.read_bytes() if self.path.is_file() else b""
            encoded = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            _write_bytes_atomic(self.path, existing + encoded)
            self._sequence = next_sequence
        return record
