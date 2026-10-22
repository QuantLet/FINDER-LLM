from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from typing import Any, Mapping, Sequence, TypedDict

from jsonschema import Draft202012Validator


MIN_NEWS_REQUESTS = 5
MAX_NEWS_REQUESTS = 20
MAX_NEWS_EVENTS = 3
BRIEF_TEXT_LIMIT = 240
REPORT_TEXT_LIMIT = 1200
NEWS_SUMMARY_TEXT_LIMIT = 900
MAX_REPORT_CLAIMS = 3
ID_TEXT_LIMIT = 128
EVIDENCE_DOMAINS = ("QUANTITATIVE", "CHART", "NEWS")


class SchemaError(RuntimeError):
    def safe_diagnostic(self) -> str:
        return str(self)


class EvidenceReport(TypedDict):
    anomaly_score: float
    evidence_domain: str
    evidence_status: str
    assessment: str
    supporting_evidence: list[dict[str, Any]]
    counter_evidence: list[dict[str, Any]]
    report: str


class FinalDecisionEnvelope(TypedDict):
    anomaly_score: float
    memory_comparison: dict[str, Any]
    assessment: dict[str, Any]
    report: dict[str, Any]


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": list(properties), "properties": dict(properties)}


def _text(maximum: int = BRIEF_TEXT_LIMIT) -> dict[str, Any]:
    return {"type": "string", "description": f"Use concise English, about {max(15, maximum // 6)} words."}


def _array(items: Mapping[str, Any], maximum: int = 12, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": dict(items)}


def _enum(values: Sequence[str]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


SCORE_SCHEMA = {"type": "number", "minimum": 0, "maximum": 1}
ANOMALY_SCORE_SCHEMA = {**SCORE_SCHEMA, "description": "Return the anomaly score to four decimal places."}


def bind_response_schema(schema: Mapping[str, Any], *, references: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:

    result = deepcopy(dict(schema))

    def visit(node: dict[str, Any]) -> None:
        for name, field in node.get("properties", {}).items():
            if name in (references or {}):
                allowed = list(dict.fromkeys(references[name]))
                if field.get("type") == "array":
                    if allowed:
                        field["items"] = _enum(allowed)
                    else:
                        field["minItems"] = field["maxItems"] = 0
                elif allowed:
                    field.update(_enum(allowed))
            visit(field)
        if isinstance(node.get("items"), dict):
            visit(node["items"])

    visit(result)
    return result


def validate_response(value: Any, schema: Mapping[str, Any]) -> dict[str, Any]:

    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise SchemaError("Response must contain finite JSON values.") from exc
    error = next((error for error in Draft202012Validator(schema).iter_errors(value) if error.validator != "additionalProperties"), None)
    if error is not None:
        known_fields = set()

        def fields(node: Mapping[str, Any]) -> None:
            known_fields.update(node.get("properties", {}))
            for child in node.get("properties", {}).values():
                fields(child)
            if isinstance(node.get("items"), dict):
                fields(node["items"])

        fields(schema)
        path = ".".join(str(part) if isinstance(part, int) or part in known_fields else "unknown_field" for part in error.absolute_path)
        raise SchemaError(f"Response schema violation at {path or 'root'}; constraint={error.validator}.")
    if isinstance(value, dict) and "anomaly_score" in schema.get("properties", {}):
        score = Decimal(str(value["anomaly_score"]))
        if score != score.quantize(Decimal("0.0001")):
            raise SchemaError("Response schema violation at anomaly_score; use at most four decimal places.")
    return deepcopy(value)


def news_reader_schema(available_indices: Sequence[int], calendar_sources=None) -> dict[str, Any]:
    from .future_events import future_event_schema
    index = {"type": "integer", **({"enum": list(available_indices)} if 0 < len(available_indices) < 900 else {"minimum": min(available_indices), "maximum": max(available_indices)} if available_indices else {})}
    selection = _object({"index": index, "selected_title": {"type": "string", "description": "Copy the complete selected candidate title exactly, preserving punctuation and Unicode."}, "reason": _text(180)})
    if calendar_sources is not None:
        choices = {event["event_id"]: _object({"index": {"type": "integer", "enum": event["candidate_indices"]}, "reason": _text(180)}) for event in calendar_sources if event["candidate_indices"]}
        return _object({"calendar_selections": _object(choices), "selections": {"type": "array", "items": selection, "minItems": min(max(0, 5 - len(choices)), len(available_indices)), "maxItems": min(MAX_NEWS_REQUESTS - len(choices), len(available_indices))}, "anomaly_score": ANOMALY_SCORE_SCHEMA})
    return _object({"future_events": future_event_schema(), "selections": {**_array(selection), "minItems": min(MIN_NEWS_REQUESTS, len(available_indices)), "maxItems": min(MAX_NEWS_REQUESTS, len(available_indices))}, "anomaly_score": ANOMALY_SCORE_SCHEMA})


def validate_news_reader(value: Any, *, available_indices: Sequence[int]) -> dict[str, Any]:
    schema = news_reader_schema(available_indices)
    result = validate_response(value, schema)
    selected = {}
    for item in result["selections"]:
        if item["index"] not in available_indices:
            raise SchemaError("Selected news index is unavailable.")
        selected.setdefault(item["index"], item)
    if not min(MIN_NEWS_REQUESTS, len(available_indices)) <= len(selected) <= min(MAX_NEWS_REQUESTS, len(available_indices)):
        raise SchemaError("News selections must meet the distinct article count limits.")
    result["selections"] = list(selected.values())
    return result


def news_assistant_schema(selected_indices: Sequence[int]) -> dict[str, Any]:
    from .future_events import certainty_update_schema
    article_refs = [f"N{index:03d}" for index in selected_indices]
    event = _object({"article_refs": {**_array(_enum(article_refs) if article_refs else _text(ID_TEXT_LIMIT)), "minItems": 1, "maxItems": 1}, "event_name": _text(180), "event_date": _text(40), "date_evidence": _text(180), "event_state": _enum(("ANNOUNCED", "SCHEDULED", "ACTIVE", "ESCALATING", "MITIGATED", "RESOLVED", "UNKNOWN")), "latest_change": _text(360), "upcoming_catalyst": _text(240), "asset_channel": _text(240), "scope": _enum(("ASSET_SPECIFIC", "GLOBAL"))})
    events = {**_array(event), "maxItems": 10 if article_refs else 0}
    return _object({"certainty_updates": certainty_update_schema(), "summaries": _object({f"N{index:03d}": _text(NEWS_SUMMARY_TEXT_LIMIT) for index in selected_indices}), "event_records": events})


def validate_news_assistant(value: Any, *, selected_indices: Sequence[int], maximum_certainty_updates: int = 10) -> dict[str, Any]:
    schema = news_assistant_schema(selected_indices)
    schema["properties"]["certainty_updates"]["maxItems"] = maximum_certainty_updates
    result = validate_response(value, schema)
    if any(error.validator == "additionalProperties" for error in Draft202012Validator(schema).iter_errors(value)):
        raise SchemaError("News Assistant output contains an unexpected field.")
    return {"certainty_updates": result["certainty_updates"], "summaries": [{"index": index, "summary": result["summaries"][f"N{index:03d}"]} for index in selected_indices], "event_records": result["event_records"]}


def evidence_report_schema(domain: str) -> dict[str, Any]:
    if domain not in EVIDENCE_DOMAINS:
        raise ValueError("Unknown evidence domain.")
    claim = _object({"observation": _text(360), "strength": _enum(("LOW", "MEDIUM", "HIGH")), "source_refs": _array(_text(ID_TEXT_LIMIT), 1, 1)})
    properties = {"evidence_status": _enum(("SUFFICIENT", "PARTIAL", "INSUFFICIENT")), "assessment": _enum(("SUPPORTS_ANOMALY", "SUPPORTS_NON_ANOMALY", "MIXED", "INSUFFICIENT")), "supporting_evidence": _array(claim, MAX_REPORT_CLAIMS), "counter_evidence": _array(claim, MAX_REPORT_CLAIMS), "report": _text(1500), "anomaly_score": ANOMALY_SCORE_SCHEMA}
    if domain == "NEWS":
        event = _object({"event_ids": {**_array(_text(ID_TEXT_LIMIT)), "minItems": 1}, "article_refs": {**_array(_text(ID_TEXT_LIMIT)), "minItems": 1}, "scope": _enum(("ASSET_SPECIFIC", "GLOBAL")), "event_summary": _text(360), "target_session_channel": _text(360), "risk_direction": _enum(("INCREASES_DOWNSIDE_RISK", "REDUCES_DOWNSIDE_RISK", "UNCLEAR"))})
        from .future_events import future_event_schema
        properties["future_events"] = future_event_schema()
        properties["event_assessments"] = {**_array(event), "maxItems": MAX_NEWS_EVENTS}
    return _object(properties)


def validate_evidence_report(value: Any, *, expected_domain: str, valid_source_refs: Sequence[str], valid_event_ids: Sequence[str] = (), maximum_future_events: int = 10) -> dict[str, Any]:
    schema = bind_response_schema(evidence_report_schema(expected_domain), references={"source_refs": valid_source_refs, "article_refs": valid_source_refs, "event_ids": valid_event_ids})
    if expected_domain == "NEWS":schema["properties"]["future_events"]["maxItems"] = maximum_future_events
    if expected_domain == "NEWS" and not valid_event_ids:
        schema["properties"]["event_assessments"]["maxItems"] = 0
    result = validate_response(value, schema)
    result["evidence_domain"] = expected_domain
    return result


SUPERVISOR_SCHEMA = _object({
    "report": _object({"evidence": _text(600), "counter_evidence": _text(600), "rationale": _text(REPORT_TEXT_LIMIT)}),
    "assessment": _object({"evidence_balance": _enum(("ANOMALY", "NON_ANOMALY", "BALANCED")), "summary": _text(600)}),
    "memory_comparison": _object({"reference_case_ids": _array(_text(ID_TEXT_LIMIT), 4), "reason": _text(600)}),
    "anomaly_score": ANOMALY_SCORE_SCHEMA,
})


def supervisor_schema(valid_memory_case_ids: Sequence[str] = ()) -> dict[str, Any]:
    return bind_response_schema(SUPERVISOR_SCHEMA, references={"reference_case_ids": valid_memory_case_ids})


def validate_supervisor(value: Any, *, valid_memory_case_ids: Sequence[str] = ()) -> dict[str, Any]:
    schema = supervisor_schema(valid_memory_case_ids)
    result = validate_response(value, schema)
    if any(error.validator == "additionalProperties" for error in Draft202012Validator(schema).iter_errors(value)):
        raise SchemaError("Supervisor output contains an unexpected field.")
    return result
