"""JSON hashes and the resolved decision history used by the supervisor."""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


class MemoryStoreError(RuntimeError):
    pass


class MemoryValidationError(MemoryStoreError):
    pass


def _validate_json_tree(value: Any, name: str = "value") -> None:
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise MemoryValidationError(f"{name} contains a non-finite number.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_tree(item, f"{name}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise MemoryValidationError(f"{name} contains a non-string key.")
            _validate_json_tree(item, f"{name}.{key}")
        return
    raise MemoryValidationError(f"{name} contains a non-JSON value.")


def canonical_json(value: Any) -> str:
    _validate_json_tree(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_memory_reference_context(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if snapshot.get("mode") != "agent_decision_history":
        raise MemoryValidationError("The paper workflow uses resolved agent decision history.")
    return {"status": snapshot["status"], "owner_agent_id": snapshot["owner_agent_id"], "recent_decisions": deepcopy(snapshot["recent_decisions"])}
