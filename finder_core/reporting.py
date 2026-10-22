from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .events import utc_now, write_json_atomic, _write_bytes_atomic


def capture_call(client: Any, agent_id: str, route: Any, recorder_path: Path | None, **kwargs: Any) -> dict[str, Any]:
    if recorder_path is None:
        return client.chat_json(**kwargs)
    folder = Path(recorder_path).parent / "inputs"
    folder.mkdir(parents=True, exist_ok=True)
    stage = kwargs["stage"]
    index = len(list(folder.glob(f"{stage}_*.json"))) + 1
    path = folder / f"{stage}_{index:02d}.json"
    payload = json.loads(next(message["content"] for message in reversed(kwargs["messages"]) if message["role"] == "user"))
    window = payload.get("window") or payload["frozen_context"]["window"]
    images = [{"path": str(Path(p).resolve()), "sha256": digest} for p, digest in zip(kwargs.get("image_paths", ()), kwargs.get("image_hashes", ()))]
    for i, raw in enumerate(kwargs.get("image_bytes", ()), 1):
        image_path = folder / f"{stage}_image_{i}.png"
        if not image_path.exists():
            _write_bytes_atomic(image_path, raw)
        images.append({"path": str(image_path.resolve())})
    record = {"agent_id": agent_id, "model": route.model, "provider": route.provider, "stage": stage, "target_date": window["forecast_target_date"], "information_cutoff": window["information_cutoff"], "started_at": utc_now(), "messages": kwargs["messages"], "schema": kwargs["schema"], "images": images}
    record["news_window"] = window.get("news_window", {})
    record["market"] = window.get("market", {})
    write_json_atomic(path, record)
    try:
        result = client.chat_json(**kwargs)
        record.update(response_json=result, client_status="returned")
        return result
    except Exception as exc:
        record.update(client_status="failed", error_type=type(exc).__name__)
        raise
    finally:
        record["ended_at"] = utc_now()
        write_json_atomic(path, record)
