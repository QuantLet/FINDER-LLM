from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .memory import canonical_json, canonical_sha256, file_sha256


BANK_SCHEMA_VERSION = "few-shot-bank"
CASE_SCHEMA_VERSION = "few-shot-case"
SCALER_SCHEMA_VERSION = "few-shot-scaler"
SNAPSHOT_SCHEMA_VERSION = "few-shot-snapshot"
MODEL_REFERENCE_SCHEMA_VERSION = "few-shot-model-reference"
MODEL_REFERENCE_MAX_CHARACTERS = 12000
SELECTION_POLICY = "class-recency"
SCALER_ID = "none"
FEW_SHOT_MODE = "frozen_reference"
LABEL_THRESHOLD_METHOD = "rolling-lower-tail-quantile-shift-one"
FEW_SHOT_STATUSES = {"BALANCED", "CLASS_LIMITED", "SINGLE_CLASS_LIMITED", "NO_FEW_SHOT"}
_MANIFEST_KEYS = {"schema_version", "bank_id", "created_at_utc", "mode", "asset", "candidate_start_date", "candidate_end_date", "window_size", "indicator_count", "indicator_fields", "input_schema_fingerprint", "forecast_horizon_days", "crash_percentile", "selection_policy", "maximum_examples", "scaler_id", "scaler_sha256", "bank_sha256", "case_count", "class_counts", "source_files", "label_artifact_fingerprint", "label_threshold_method", "verified_label_rows", "exclusion_counts", "case_payload", "manifest_hash"}


class FewShotError(RuntimeError):
    pass


class FewShotValidationError(FewShotError):
    pass


def _iso_date(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise FewShotValidationError(f"{name} must use YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FewShotValidationError(f"{name} must use YYYY-MM-DD.") from exc
    if parsed.isoformat() != value:
        raise FewShotValidationError(f"{name} must use canonical YYYY-MM-DD.")
    return value


def _utc_timestamp(value: Any) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if not isinstance(value, str):
        raise FewShotValidationError("created_at_utc must be an ISO UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FewShotValidationError("created_at_utc must be an ISO UTC timestamp.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise FewShotValidationError("created_at_utc must include the UTC offset.")
    return parsed.astimezone(timezone.utc).isoformat()


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise FewShotValidationError(f"{name} must be a SHA-256 hex digest.")
    try:
        int(value, 16)
    except ValueError as exc:
        raise FewShotValidationError(f"{name} must be a SHA-256 hex digest.") from exc
    return value.lower()


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FewShotValidationError(f"{name} must be numeric.") from exc
    if not math.isfinite(number):
        raise FewShotValidationError(f"{name} must be finite.")
    return number


def _q(value: Any) -> float:
    number = _finite(value, "crash_percentile")
    if not 0.0 < number < 1.0:
        raise FewShotValidationError("crash_percentile must be strictly between 0 and 1.")
    return number


def _ordered_dates(values: Sequence[str], *, expected_count: int = 126, name: str = "window_observation_dates") -> list[str]:
    dates = [_iso_date(value, name) for value in values]
    if len(dates) != expected_count or dates != sorted(dates) or len(set(dates)) != len(dates):
        raise FewShotValidationError(f"{name} must contain {expected_count} unique ordered dates.")
    return dates


def _case_id(case: Mapping[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in case.items() if key != "case_id"})


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FewShotValidationError(f"Cannot read valid {label} JSON.") from exc
    if not isinstance(value, dict):
        raise FewShotValidationError(f"{label} must be a JSON object.")
    return value


def _load_cases(path: Path) -> list[dict[str, Any]]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise FewShotValidationError("Cannot read few-shot bank.") from exc
    if not content or not content.endswith(b"\n"):
        raise FewShotValidationError("Few-shot bank must be non-empty and end with a newline.")
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise FewShotValidationError("Few-shot bank is not valid UTF-8.") from exc
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FewShotValidationError(f"Few-shot bank line {line_number} is invalid.") from exc
        if not isinstance(case, dict):
            raise FewShotValidationError(f"Few-shot bank line {line_number} must be an object.")
        cases.append(case)
    return cases


def validate_case(case: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "case_id", "asset", "information_cutoff", "forecast_target_date", "forecast_horizon_days", "crash_percentile", "input_schema_fingerprint", "window_observation_dates", "input_hash", "indicator_latest", "indicator_mean", "indicator_std", "observed_anomaly_label", "frozen_tail_threshold", "source_fingerprints"}
    expected_schema = CASE_SCHEMA_VERSION
    if set(case) != required or case.get("schema_version") != expected_schema:
        raise FewShotValidationError("Few-shot case has the wrong schema.")
    clean = json.loads(canonical_json(dict(case)))
    for name in ("information_cutoff", "forecast_target_date"):
        _iso_date(clean[name], name)
    clean["window_observation_dates"] = _ordered_dates(clean["window_observation_dates"], expected_count=int(manifest["window_size"]))
    if clean["information_cutoff"] != clean["window_observation_dates"][-1] or not clean["information_cutoff"] < clean["forecast_target_date"]:
        raise FewShotValidationError("Few-shot case dates are inconsistent.")
    if clean["asset"] != manifest["asset"] or clean["forecast_horizon_days"] != manifest["forecast_horizon_days"] or clean["input_schema_fingerprint"] != manifest["input_schema_fingerprint"]:
        raise FewShotValidationError("Few-shot case is incompatible with its manifest.")
    if not math.isclose(float(clean["crash_percentile"]), float(manifest["crash_percentile"]), rel_tol=0.0, abs_tol=1e-12):
        raise FewShotValidationError("Few-shot case crash percentile differs from its manifest.")
    fields = list(manifest["indicator_fields"])
    for name in ("indicator_latest", "indicator_mean", "indicator_std"):
        if not isinstance(clean[name], dict) or set(clean[name]) != set(fields):
            raise FewShotValidationError(f"{name} does not match the 63-field schema.")
        clean[name] = {field: _finite(clean[name][field], f"{name}.{field}") for field in fields}
    if clean["observed_anomaly_label"] not in {0, 1} or type(clean["observed_anomaly_label"]) is not int:
        raise FewShotValidationError("Few-shot label must be binary.")
    clean["frozen_tail_threshold"] = _finite(clean["frozen_tail_threshold"], "frozen_tail_threshold")
    _hash(clean["input_hash"], "input_hash")
    if not isinstance(clean["source_fingerprints"], dict) or clean["source_fingerprints"] != {item["role"]: item["sha256"] for item in manifest["source_files"]}:
        raise FewShotValidationError("Few-shot source fingerprints do not match the manifest.")
    return clean


def build_few_shot_reference_context(snapshot: Mapping[str, Any]) -> dict[str, Any]:

    base = {key: value for key, value in snapshot.items() if key not in {"few_shot_snapshot_hash", "model_reference_context"}}
    examples = base.get("examples", [])
    if not isinstance(examples, list) or any(not isinstance(item, Mapping) for item in examples):
        raise FewShotValidationError("Few-shot snapshot examples are invalid.")
    fields = sorted(examples[0].get("indicator_latest", {})) if examples else []
    if examples and (len(fields) != 63 or any(sorted(item.get("indicator_latest", {})) != fields for item in examples)):
        raise FewShotValidationError("Few-shot model references require the same 63 latest-value fields.")
    projected_examples: list[dict[str, Any]] = []
    for item in examples:
        projected_examples.append(
            {
                "case_id": item.get("case_id"),
                "information_cutoff": item.get("information_cutoff"),
                "forecast_target_date": item.get("forecast_target_date"),
                "observed_anomaly_label": item.get("observed_anomaly_label"),
                "frozen_tail_threshold": item.get("frozen_tail_threshold"),
                "latest_values": [_finite(item["indicator_latest"][field], f"indicator_latest.{field}") for field in fields],
            }
        )
    context: dict[str, Any] = {
        "schema_version": MODEL_REFERENCE_SCHEMA_VERSION,
        "status": base.get("status"),
        "selection_policy": base.get("selection_policy"),
        "source_snapshot_payload_hash": canonical_sha256(base),
        "indicator_fields": fields,
        "examples": projected_examples,
        "reference_contract": "63_latest_values_plus_real_binary_label",
    }
    context["reference_context_hash"] = canonical_sha256(context)
    if len(canonical_json(context)) > MODEL_REFERENCE_MAX_CHARACTERS:
        raise FewShotValidationError("Few-shot model reference exceeds its fixed character budget.")
    return context


class FewShotBank:
    def __init__(self, bank_path: Path | str) -> None:
        value = Path(bank_path)
        self.bank_path = value / "few_shot_bank.jsonl" if value.suffix == "" else value
        if self.bank_path.name != "few_shot_bank.jsonl":
            raise FewShotValidationError("Few-shot bank filename must be few_shot_bank.jsonl.")
        self.root = self.bank_path.parent
        self.scaler_path = self.root / "few_shot_scaler.json"
        self.manifest_path = self.root / "few_shot_bank_manifest.json"
        self._loaded = None

    def load(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not all(path.is_file() for path in (self.bank_path, self.scaler_path, self.manifest_path)):
            raise FewShotValidationError("Frozen few-shot bank, scaler, or manifest is missing.")
        manifest = _read_json(self.manifest_path, "few-shot manifest")
        if set(manifest) != _MANIFEST_KEYS or manifest.get("schema_version") != BANK_SCHEMA_VERSION or manifest.get("mode") != FEW_SHOT_MODE or manifest.get("selection_policy") != SELECTION_POLICY or manifest.get("maximum_examples") != 4:
            raise FewShotValidationError("Few-shot manifest contract is invalid.")
        if not isinstance(manifest.get("bank_id"), str) or not manifest["bank_id"] or not isinstance(manifest.get("asset"), str) or not manifest["asset"]:
            raise FewShotValidationError("Few-shot manifest identity is invalid.")
        created_at = _utc_timestamp(manifest.get("created_at_utc"))
        start = _iso_date(manifest.get("candidate_start_date"), "candidate_start_date")
        end = _iso_date(manifest.get("candidate_end_date"), "candidate_end_date")
        if created_at != manifest["created_at_utc"] or start > end:
            raise FewShotValidationError("Few-shot manifest time boundary is invalid.")
        if manifest.get("window_size") != 126 or manifest.get("indicator_count") != 63 or manifest.get("forecast_horizon_days") != 1:
            raise FewShotValidationError("Few-shot manifest must retain the 126 x 63 one-day contract.")
        fields = manifest.get("indicator_fields")
        if not isinstance(fields, list) or len(fields) != 63 or len(set(fields)) != 63 or any(not isinstance(field, str) or not field for field in fields):
            raise FewShotValidationError("Few-shot manifest indicator fields are invalid.")
        if manifest.get("input_schema_fingerprint") != canonical_sha256({"window_size": 126, "indicator_fields": fields}):
            raise FewShotValidationError("Few-shot manifest input schema fingerprint is invalid.")
        _q(manifest.get("crash_percentile"))
        if not isinstance(manifest.get("source_files"), list) or any(not isinstance(item, dict) or set(item) != {"role", "size_bytes", "sha256"} for item in manifest["source_files"]):
            raise FewShotValidationError("Few-shot source manifest is invalid.")
        source_roles: set[str] = set()
        for item in manifest["source_files"]:
            if not isinstance(item["role"], str) or not item["role"] or item["role"] in source_roles or type(item["size_bytes"]) is not int or item["size_bytes"] < 0:
                raise FewShotValidationError("Few-shot source manifest identity or size is invalid.")
            source_roles.add(item["role"])
            _hash(item["sha256"], f"source_files.{item['role']}.sha256")
        if not {"indicators", "labels", "label_returns"}.issubset(source_roles):
            raise FewShotValidationError("Few-shot source manifest lacks required source roles.")
        _hash(manifest.get("label_artifact_fingerprint"), "label_artifact_fingerprint")
        if manifest.get("label_threshold_method") != LABEL_THRESHOLD_METHOD or type(manifest.get("verified_label_rows")) is not int or manifest["verified_label_rows"] <= 0:
            raise FewShotValidationError("Few-shot label artifact verification metadata is invalid.")
        if type(manifest.get("case_count")) is not int or manifest["case_count"] <= 0:
            raise FewShotValidationError("Few-shot manifest case count is invalid.")
        class_counts = manifest.get("class_counts")
        if not isinstance(class_counts, dict) or set(class_counts) != {"ANOMALY", "NON_ANOMALY"} or any(type(value) is not int or value < 0 for value in class_counts.values()) or sum(class_counts.values()) != manifest["case_count"]:
            raise FewShotValidationError("Few-shot manifest class counts are invalid.")
        expected_exclusions = {"outside_candidate_period", "not_in_valid_dates", "incomplete_window", "target_unavailable", "label_unavailable"}
        if not isinstance(manifest.get("exclusion_counts"), dict) or set(manifest["exclusion_counts"]) != expected_exclusions or any(type(value) is not int or value < 0 for value in manifest["exclusion_counts"].values()) or manifest.get("case_payload") != "126_dates_plus_63_latest_mean_std_label_threshold_and_source_hashes":
            raise FewShotValidationError("Few-shot manifest audit metadata is invalid.")
        saved_manifest_hash = manifest.get("manifest_hash")
        if saved_manifest_hash != canonical_sha256({key: value for key, value in manifest.items() if key != "manifest_hash"}):
            raise FewShotValidationError("Few-shot manifest hash does not match.")
        scaler = _read_json(self.scaler_path, "few-shot scaler")
        expected_scaler = {"schema_version": SCALER_SCHEMA_VERSION, "scaler_id": SCALER_ID, "transformation": "none", "parameters": {}, "fitted": False}
        if scaler != expected_scaler or manifest.get("scaler_id") != SCALER_ID or manifest.get("scaler_sha256") != file_sha256(self.scaler_path):
            raise FewShotValidationError("Few-shot none scaler contract is invalid.")
        if manifest.get("bank_sha256") != file_sha256(self.bank_path):
            raise FewShotValidationError("Few-shot bank hash does not match.")
        cases = [validate_case(case, manifest) for case in _load_cases(self.bank_path)]
        if len(cases) != manifest.get("case_count") or len({case["case_id"] for case in cases}) != len(cases):
            raise FewShotValidationError("Few-shot bank count or case IDs are invalid.")
        if [case["information_cutoff"] for case in cases] != sorted(case["information_cutoff"] for case in cases):
            raise FewShotValidationError("Few-shot bank cases must be ordered by cutoff.")
        if manifest["schema_version"] == BANK_SCHEMA_VERSION and [case["case_id"] for case in cases] != [f"FS{index:04d}" for index in range(1, len(cases) + 1)]:
            raise FewShotValidationError("Few-shot cases must use ordered FS indices.")
        observed_class_counts = {"ANOMALY": sum(case["observed_anomaly_label"] == 1 for case in cases), "NON_ANOMALY": sum(case["observed_anomaly_label"] == 0 for case in cases)}
        if observed_class_counts != manifest.get("class_counts") or any(not start <= case["information_cutoff"] <= end for case in cases):
            raise FewShotValidationError("Few-shot manifest class counts do not match.")
        self._loaded = (manifest, cases)
        return manifest, cases

    def freeze_snapshot(self, *, asset: str, information_cutoff: str, forecast_target_date: str, forecast_horizon_days: int, crash_percentile: float, input_schema_fingerprint: str, indicator_fields: Sequence[str], window_observation_dates: Sequence[str], mode: str = FEW_SHOT_MODE) -> dict[str, Any]:
        if mode != FEW_SHOT_MODE:
            raise FewShotValidationError("Few-shot mode must be frozen_reference.")
        manifest, cases = self._loaded if self._loaded is not None else self.load()
        cutoff = _iso_date(information_cutoff, "information_cutoff")
        target = _iso_date(forecast_target_date, "forecast_target_date")
        current_dates = _ordered_dates(list(window_observation_dates))
        if cutoff != current_dates[-1] or cutoff >= target:
            raise FewShotValidationError("Current cutoff, target, and observation dates are inconsistent.")
        if not isinstance(asset, str) or not asset or type(forecast_horizon_days) is not int or asset != manifest["asset"] or forecast_horizon_days != manifest["forecast_horizon_days"] or input_schema_fingerprint != manifest["input_schema_fingerprint"] or list(indicator_fields) != manifest["indicator_fields"]:
            raise FewShotValidationError("Current input contract does not match the frozen few-shot bank.")
        q = _q(crash_percentile)
        if not math.isclose(q, float(manifest["crash_percentile"]), rel_tol=0.0, abs_tol=1e-12):
            raise FewShotValidationError("Current crash percentile does not match the frozen few-shot bank.")
        current_set = set(current_dates)
        eligible: list[dict[str, Any]] = []
        excluded = {"not_past": 0, "outcome_not_available": 0, "window_overlap": 0, "horizon_embargo": 0}
        for case in cases:
            if case["forecast_target_date"] >= target:
                excluded["not_past"] += 1
            elif case["forecast_target_date"] > cutoff:
                excluded["outcome_not_available"] += 1
            elif current_set.intersection(case["window_observation_dates"]):
                excluded["window_overlap"] += 1
            elif case["forecast_target_date"] >= current_dates[0]:
                excluded["horizon_embargo"] += 1
            else:
                eligible.append(case)
        anomalies = sorted((case for case in eligible if case["observed_anomaly_label"] == 1), key=lambda case: (-date.fromisoformat(case["forecast_target_date"]).toordinal(), case["case_id"]))
        non_anomalies = sorted((case for case in eligible if case["observed_anomaly_label"] == 0), key=lambda case: (-date.fromisoformat(case["forecast_target_date"]).toordinal(), case["case_id"]))
        if not eligible:
            status = "NO_FEW_SHOT"
            selected: list[dict[str, Any]] = []
        elif anomalies and non_anomalies:
            selected = anomalies[:2] + non_anomalies[:2]
            status = "BALANCED" if len(anomalies) >= 2 and len(non_anomalies) >= 2 else "CLASS_LIMITED"
            if len(selected) < 4:
                selected_ids = {case["case_id"] for case in selected}
                remaining = [case for case in eligible if case["case_id"] not in selected_ids]
                remaining.sort(key=lambda case: (-date.fromisoformat(case["forecast_target_date"]).toordinal(), case["case_id"]))
                selected.extend(remaining[: 4 - len(selected)])
        else:
            status = "SINGLE_CLASS_LIMITED"
            selected = (anomalies or non_anomalies)[:4]
        selected.sort(key=lambda case: (-date.fromisoformat(case["forecast_target_date"]).toordinal(), case["case_id"]))
        if status not in FEW_SHOT_STATUSES:
            raise FewShotValidationError("Few-shot status is invalid.")
        index_by_id = {case["case_id"]: f"FS{position:04d}" for position, case in enumerate(cases, 1)}
        selected = [{**case, "source_case_hash": _case_id(case), "case_id": index_by_id[case["case_id"]]} for case in selected]
        snapshot: dict[str, Any] = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "status": status,
            "mode": mode,
            "bank_id": manifest["bank_id"],
            "bank_manifest_hash": manifest["manifest_hash"],
            "bank_hash": manifest["bank_sha256"],
            "scaler_id": SCALER_ID,
            "scaler_hash": manifest["scaler_sha256"],
            "selection_policy": SELECTION_POLICY,
            "current": {"asset": asset, "information_cutoff": cutoff, "forecast_target_date": target, "forecast_horizon_days": forecast_horizon_days, "crash_percentile": q, "input_schema_fingerprint": input_schema_fingerprint, "window_observation_dates_hash": canonical_sha256(current_dates)},
            "eligible_counts": {"total": len(eligible), "ANOMALY": len(anomalies), "NON_ANOMALY": len(non_anomalies)},
            "excluded_counts": excluded,
            "selected_case_ids": [case["case_id"] for case in selected],
            "examples": selected,
        }
        snapshot["model_reference_context"] = build_few_shot_reference_context(snapshot)
        snapshot["few_shot_snapshot_hash"] = canonical_sha256(snapshot)
        return snapshot

    freeze = freeze_snapshot


__all__ = ["FewShotBank", "FewShotError", "FewShotValidationError", "build_few_shot_reference_context"]
