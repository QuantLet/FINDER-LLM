from __future__ import annotations

import fcntl
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .events import write_json_atomic


MAX_STAGE_EXECUTION_ATTEMPTS = 3


class StorageError(RuntimeError):
    pass


class StageAttemptLimitError(StorageError):
    pass


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class StageStore:


    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.attempt_root = self.root / ".attempts"
        self.attempt_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _filename(stage_id: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", stage_id).strip("._")
        if not value:
            raise StorageError("stage_id must contain a safe character.")
        return f"{value}.json"

    def path_for(self, stage_id: str) -> Path:
        return self.root / self._filename(stage_id)

    def attempt_path_for(self, stage_id: str) -> Path:
        return self.attempt_root / self._filename(stage_id)

    def reserve_attempt(self, stage_id: str, input_fingerprint: str, max_attempts: int = MAX_STAGE_EXECUTION_ATTEMPTS) -> int:
        if type(max_attempts) is not int or max_attempts <= 0:
            raise StorageError("max_attempts must be a positive integer.")
        path = self.attempt_path_for(stage_id)
        lock_path = path.with_name(f".{path.name}.lock")
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            envelope = read_json(path) if path.is_file() else {}
            if envelope:
                if set(envelope) != {"stage_id", "input_fingerprint", "attempts", "max_attempts"}:
                    raise StorageError(f"Attempt record schema mismatch for {stage_id}.")
                if envelope.get("stage_id") != stage_id or envelope.get("input_fingerprint") != input_fingerprint or envelope.get("max_attempts") != max_attempts:
                    raise StorageError(f"Attempt record context changed for {stage_id}.")
                attempts = envelope.get("attempts")
                if type(attempts) is not int or attempts < 0:
                    raise StorageError(f"Attempt record is invalid for {stage_id}.")
            else:
                attempts = 0
            if attempts >= max_attempts:
                raise StageAttemptLimitError(f"Stage {stage_id} exhausted its {max_attempts} durable execution attempts.")
            attempts += 1
            write_json_atomic(path, {"stage_id": stage_id, "input_fingerprint": input_fingerprint, "attempts": attempts, "max_attempts": max_attempts})
            return attempts

    def load(self, stage_id: str, input_fingerprint: str) -> dict[str, Any] | None:
        path = self.path_for(stage_id)
        if not path.is_file():
            return None
        envelope = read_json(path)
        if envelope.get("stage_id") != stage_id:
            raise StorageError(f"Cached stage identity mismatch for {stage_id}.")
        if envelope.get("input_fingerprint") != input_fingerprint:
            raise StorageError(f"Cached stage input changed for {stage_id}.")
        output = envelope.get("output")
        if not isinstance(output, dict):
            raise StorageError(f"Cached stage output is invalid for {stage_id}.")
        if envelope.get("output_fingerprint") != canonical_hash(output):
            raise StorageError(f"Cached stage output hash mismatch for {stage_id}.")
        return output

    def save_once(self, stage_id: str, input_fingerprint: str, output: Mapping[str, Any]) -> Path:
        path = self.path_for(stage_id)
        payload = deepcopy(dict(output))
        envelope = {
            "stage_id": stage_id,
            "input_fingerprint": input_fingerprint,
            "output_fingerprint": canonical_hash(payload),
            "output": payload,
        }
        if path.is_file():
            existing = read_json(path)
            if existing != envelope:
                raise StorageError(f"Refusing to overwrite immutable stage {stage_id}.")
            return path
        write_json_atomic(path, envelope)
        return path


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"Cannot read {path.name}.") from exc
    if not isinstance(value, dict):
        raise StorageError(f"{path.name} must contain one JSON object.")
    return value


def write_json_once(path: Path, payload: Mapping[str, Any]) -> Path:


    clean = deepcopy(dict(payload))
    if path.is_file():
        if read_json(path) != clean:
            raise StorageError(f"Refusing to overwrite immutable {path.name}.")
        return path
    write_json_atomic(path, clean)
    return path
