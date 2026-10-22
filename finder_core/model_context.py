from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Mapping

from .verification import build_machine_comparisons


DECISION_MODEL_PAYLOAD_MAX_CHARACTERS = 150000


class ModelContextError(RuntimeError):
    pass


def canonical_model_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False, default=str)


def without_anomaly_scores(value: Any) -> Any:

    if isinstance(value, Mapping):
        return {key: without_anomaly_scores(item) for key, item in value.items() if str(key).lower() not in {"anomaly_score", "return_direction", "prior_return_direction", "confidence", "decision", "certainty_basis", "timing_basis", "news_source_refs", "sources", "selection_source_refs"}}
    if isinstance(value, (list, tuple)):
        return [without_anomaly_scores(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\b(?:anomaly[_ -]?score|risk score)\s*(?:=|:|\bis\b|\bof\b)?\s*(?:[01](?:\.\d+)?|\.\d+)(?!\w|\.\d)", "anomaly_score [withheld]", value, flags=re.IGNORECASE)
    return value


def build_evidence_model_context(reports: Mapping[str, Any]) -> dict[str, Any]:

    result = [without_anomaly_scores(reports[domain]) for domain in ("QUANTITATIVE", "CHART", "NEWS")]
    for report in result:
        for name in ("supporting_evidence", "counter_evidence"):
            for claim in report[name]:
                claim.pop("source_refs", None)
    return {"reports": result}


def build_memory_decision_context(reference_context: Mapping[str, Any]) -> dict[str, Any]:
    source = reference_context
    all_cases = [*source.get("recent_decisions", []), *source.get("comparable_resolved_cases", [])]
    ordered = sorted({item["decision_id"]: item for item in all_cases}.values(), key=lambda item: (item["information_cutoff"], item["decision_id"]))
    index = {item["decision_id"]: f"MEM{position:02d}" for position, item in enumerate(ordered, 1)}

    def project(item: Mapping[str, Any]) -> dict[str, Any]:
        result = {key: copy.deepcopy(item[key]) for key in ("information_cutoff", "forecast_target_date", "bounded_rationale", "overlap", "outcome") if key in item}
        result["decision_id"] = index[item["decision_id"]]
        result["outcome_status"] = "RESOLVED" if item.get("outcome") is not None else "PREDICTION_ONLY"
        if source.get("owner_agent_id"):
            if item["agent_id"] != source["owner_agent_id"]: raise ModelContextError("Memory contains another agent decision.")
            for key in ("agent_id", "prior_anomaly_score", "prior_evidence_balance"):
                result[key] = copy.deepcopy(item[key])
            result.pop("bounded_rationale", None)
            return result
        result["bounded_rationale"] = str(result.get("bounded_rationale", ""))[:120]
        return without_anomaly_scores(result)

    result = {"status": source.get("status"), "recent_decisions": [project(item) for item in source.get("recent_decisions", [])], "reference_index": {alias: original for original, alias in index.items()}}
    if "comparable_resolved_cases" in source:
        result["comparable_resolved_cases"] = [project(item) for item in source["comparable_resolved_cases"]]
    return result


def build_few_shot_decision_context(reference_context: Mapping[str, Any], *, current_numeric_reference: Mapping[str, Any] | None = None) -> dict[str, Any]:
    examples = []
    reference_index = {}
    for position, item in enumerate(reference_context.get("examples", []), 1):
        alias = item["case_id"] if str(item["case_id"]).startswith("FS") else f"FS{position:02d}"
        reference_index[alias] = item["case_id"]
        examples.append({"case_id": alias, "information_cutoff": item["information_cutoff"], "forecast_target_date": item["forecast_target_date"], "observed_anomaly_label": item["observed_anomaly_label"], "frozen_tail_threshold": float(format(item["frozen_tail_threshold"], ".10g")), "latest_values": {field: float(format(number, ".10g")) for field, number in zip(reference_context["indicator_fields"], item["latest_values"])}})
    fields = list(reference_context.get("indicator_fields", []))
    result = {"status": reference_context.get("status"), "indicator_fields": fields, "examples": examples, "reference_index": reference_index}
    if current_numeric_reference is not None:
        values = current_numeric_reference["latest_values"]
        fields = fields or list(values)
        result["indicator_fields"] = fields
        result["current_observation"] = {"information_cutoff": current_numeric_reference["information_cutoff"], "forecast_target_date": current_numeric_reference["forecast_target_date"], "latest_values": {field: values[field] for field in fields}}
        for name in ("target_event_spec", "current_window_context", "context_window_size", "global_market_context", "previous_indicator_series"):
            if name in current_numeric_reference:
                result["current_observation"][name] = without_anomaly_scores(copy.deepcopy(current_numeric_reference[name]))
        result["current_observation"]["machine_checked_comparisons"] = build_machine_comparisons(current_numeric_reference)
        if "target_event_spec" in current_numeric_reference:
            spec = current_numeric_reference["target_event_spec"]
            close, cutoff = float(values["close"]), float(spec["target_tail_threshold"])
            result["current_observation"]["target_return_basis"] = {"base_session": spec["market_data_cutoff_date"], "base_close": close, "target_session": spec["forecast_target_date"], "log_return_boundary": cutoff, "simple_return_boundary": math.expm1(cutoff), "target_close_boundary": close * math.exp(cutoff), "interpretation": "The target return starts anew at base_close and includes the overnight move. Prior daily returns do not accumulate into it."}
    return result


def model_reference_case_ids(memory_context: Mapping[str, Any], few_shot_context: Mapping[str, Any]) -> list[str]:
    memory_ids = [item["decision_id"] for key in ("recent_decisions", "comparable_resolved_cases") for item in memory_context.get(key, [])]
    return list(dict.fromkeys([*memory_ids, *[item["case_id"] for item in few_shot_context.get("examples", [])]]))


def assert_decision_model_payload(payload: Mapping[str, Any]) -> int:
    characters = len(canonical_model_json(payload))
    if characters > DECISION_MODEL_PAYLOAD_MAX_CHARACTERS:
        raise ModelContextError(f"Decision input exceeds {DECISION_MODEL_PAYLOAD_MAX_CHARACTERS} characters.")
    return characters
