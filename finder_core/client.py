"""One Responses API client for text and chart inputs."""

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
from pathlib import Path

from .config import EXPECTED_CHART_COUNT
from .events import utc_now
from .provider_retry import create_response, error_details


class ModelError(RuntimeError):
    pass


def _value(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _usage(usage, name):
    return int(_value(usage, name) or 0)


def _has_refusal(response):
    return any(_value(content, "type") == "refusal" or str(_value(content, "refusal") or "").strip() for item in (_value(response, "output") or []) for content in (_value(item, "content") or []))


class ModelClient:
    def __init__(self, *, agent_id, model_name, modality, recorder, timeout_seconds, max_output_tokens, max_retries, reasoning_effort="low", api_key=None, base_url=None, sdk_client=None):
        self.agent_id = agent_id
        self.model_name = model_name
        self.modality = modality
        self.endpoint_name = "openai_responses_" + modality
        self.recorder = recorder
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self._api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        if sdk_client is not None:
            self._client = sdk_client
            return
        if not self._api_key.strip():
            raise ModelError("Add your OpenAI API key before starting a run.")
        from openai import OpenAI
        self._client = OpenAI(api_key=self._api_key, base_url=base_url or "https://api.openai.com/v1", timeout=timeout_seconds, max_retries=max_retries)

    def _input(self, messages, image_paths, image_bytes, image_hashes):
        paths = [Path(path) for path in image_paths]
        raw_images = [(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png") for path in paths]
        raw_images += [(bytes(content), "image/png") for content in image_bytes]
        if not messages or not any(message.get("role") == "user" for message in messages):
            raise ModelError("A request needs at least one user message.")
        if self.modality == "text" and (raw_images or image_hashes):
            raise ModelError("The text route must not receive images.")
        if self.modality == "vision" and not 1 <= len(raw_images) <= EXPECTED_CHART_COUNT * 2:
            raise ModelError("A chart request needs between 1 and 6 images.")
        if len(raw_images) != len(image_hashes):
            raise ModelError("Each chart needs one frozen SHA-256 digest.")
        parts = []
        for (content, mime), digest in zip(raw_images, image_hashes):
            if not mime.startswith("image/") or not content or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(content).hexdigest() != digest:
                raise ModelError("Chart bytes do not match the frozen SHA-256 digest.")
            parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}", "detail": "high"})
        inputs = [{"role": str(message.get("role", "user")), "content": str(message.get("content", ""))} for message in messages]
        if parts:
            last_user = max(i for i, message in enumerate(inputs) if message["role"] == "user")
            inputs[last_user]["content"] = [{"type": "input_text", "text": inputs[last_user]["content"]}, *parts]
        return inputs, [mime for _, mime in raw_images]

    def chat_json(self, *, stage, messages, schema, seed, response_contract=None, image_paths=(), image_bytes=(), image_hashes=()):
        inputs, mime_types = self._input(messages, image_paths, image_bytes, image_hashes)
        call_id = self.recorder.reserve_id()
        started_at, wall_start = utc_now(), time.perf_counter()
        contract = response_contract or stage
        name = ("finder_" + re.sub(r"[^A-Za-z0-9_-]+", "_", contract).strip("_"))[:64] or "finder_output"
        request = {"message_count": len(messages), "content_characters": sum(len(str(item.get("content", ""))) for item in messages), "image_count": len(mime_types), "api": "responses", "structured_output": True, "response_contract": contract, "reasoning_effort": self.reasoning_effort, "max_output_tokens": self.max_output_tokens}
        if mime_types:
            request.update(image_sha256=list(image_hashes), image_detail="high", image_mime_types=mime_types)
        response, output, error = None, None, None
        raw_output, incomplete_reason, error_code = "", "", ""
        try:
            response = create_response(self._client.responses.create, log_path=self.recorder.path.with_name("transport.jsonl") if self.recorder.path else None, stage=stage, agent_id=self.agent_id, secrets=(self._api_key,), model=self.model_name, input=inputs, text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}}, reasoning={"effort": self.reasoning_effort}, max_output_tokens=self.max_output_tokens, store=False)
            status = str(_value(response, "status") or "unknown")
            incomplete_reason = str(_value(_value(response, "incomplete_details"), "reason") or "")
            error_code = str(_value(_value(response, "error"), "code") or "")
            raw_output = str(_value(response, "output_text") or "")
            if error_code or (status != "completed" and not (status == "incomplete" and incomplete_reason == "max_output_tokens")):
                raise ModelError(f"The response did not complete: {status}; reason: {incomplete_reason}; code: {error_code}.")
            if _has_refusal(response):
                raise ModelError("The model refused this request.")
            if not raw_output.strip():
                raise ModelError("The model returned no output text.")
            output = json.loads(raw_output)
            if not isinstance(output, dict):
                raise ModelError("The model output must be one JSON object.")
        except Exception as exc:
            error = json.dumps(error_details(exc, secrets=(self._api_key,)), ensure_ascii=False)
        usage = _value(response, "usage")
        prompt_tokens, completion_tokens = _usage(usage, "input_tokens"), _usage(usage, "output_tokens")
        self.recorder.append({"call_id": call_id, "stage": stage, "role": self.agent_id, "agent_id": self.agent_id, "model": self.model_name, "provider": "openai", "endpoint": self.endpoint_name, "modality": self.modality, "started_at_utc": started_at, "ended_at_utc": utc_now(), "request": request, "latency": {"wall_seconds": round(time.perf_counter() - wall_start, 6)}, "tokens": {"prompt": prompt_tokens, "completion": completion_tokens, "reasoning": _usage(_value(usage, "output_tokens_details"), "reasoning_tokens"), "total": _usage(usage, "total_tokens") or prompt_tokens + completion_tokens}, "response_id": _value(response, "id"), "response_model": str(_value(response, "model") or ""), "response_status": _value(response, "status"), "incomplete_reason": incomplete_reason if incomplete_reason in {"", "content_filter", "max_output_tokens"} else "other", "response_error_present": bool(error_code), "refusal_detected": _has_refusal(response), "output_characters": len(raw_output), "output_sha256": hashlib.sha256(raw_output.encode()).hexdigest() if response is not None else "", "error": error})
        if error is not None:
            raise ModelError(f"Model call failed for {stage}: {error}")
        return output
