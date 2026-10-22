from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from .few_shot import BANK_SCHEMA_VERSION, CASE_SCHEMA_VERSION, FEW_SHOT_MODE, SCALER_ID, SCALER_SCHEMA_VERSION, SELECTION_POLICY, FewShotError, FewShotValidationError, _finite, _iso_date, _q, _utc_timestamp
from .label_artifact import LabelArtifactError, verify_label_artifact
from .memory import canonical_json, canonical_sha256, file_sha256


def _read_csv(path: Path, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except OSError as exc:
        raise FewShotError(f"Cannot read {label} CSV.") from exc
    if not fields or not rows:
        raise FewShotValidationError(f"{label} CSV is empty.")
    return fields, rows


def _date_field(fields: Sequence[str], label: str) -> str:
    matches = [name for name in fields if name.lower() == "date"]
    if len(matches) != 1:
        raise FewShotValidationError(f"{label} CSV must contain exactly one date column.")
    return matches[0]


def _write_atomic(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_frozen_bundle(entries: Sequence[tuple[Path, bytes]]) -> None:
    paths = [path for path, _ in entries]
    root = paths[0].parent
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".few_shot_bank.lock"
    with lock_path.open("a+b") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if any(path.exists() for path in paths):
            raise FewShotValidationError("Frozen few-shot bank or sidecar already exists.")
        written: list[Path] = []
        try:
            for path, content in entries:
                _write_atomic(path, content)
                written.append(path)
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise


def _source_record(path: Path, role: str) -> dict[str, Any]:
    return {"role": role, "size_bytes": path.stat().st_size, "sha256": file_sha256(path)}


def build_frozen_few_shot_bank(indicators_path: Path | str, labels_path: Path | str, bank_path: Path | str, *, bank_id: str, asset: str, candidate_start_date: str, candidate_end_date: str, crash_percentile: float, valid_dates_path: Path | str | None = None, returns_path: Path | str | None = None, window_size: int = 126, label_window_size: int = 252, expected_indicator_count: int = 63, horizon_days: int = 1, created_at_utc: str | None = None) -> dict[str, Any]:
    indicators_file = Path(indicators_path)
    labels_file = Path(labels_path)
    returns_file = Path(returns_path) if returns_path is not None else labels_file.with_name("df_Log_Returns.csv")
    valid_dates_file = Path(valid_dates_path) if valid_dates_path is not None else None
    bank_file = Path(bank_path)
    if bank_file.name != "few_shot_bank.jsonl":
        raise FewShotValidationError("Few-shot bank filename must be few_shot_bank.jsonl.")
    manifest_file = bank_file.with_name("few_shot_bank_manifest.json")
    scaler_file = bank_file.with_name("few_shot_scaler.json")
    if any(path.exists() for path in (bank_file, manifest_file, scaler_file)):
        raise FewShotValidationError("Frozen few-shot bank or sidecar already exists.")
    if not isinstance(bank_id, str) or not bank_id or not isinstance(asset, str) or not asset:
        raise FewShotValidationError("bank_id and asset must be non-empty strings.")
    start = _iso_date(candidate_start_date, "candidate_start_date")
    end = _iso_date(candidate_end_date, "candidate_end_date")
    if start > end:
        raise FewShotValidationError("candidate_start_date must not exceed candidate_end_date.")
    q = _q(crash_percentile)
    if window_size != 126 or expected_indicator_count != 63 or horizon_days != 1:
        raise FewShotValidationError("The frozen bank requires a 126 x 63 window and one-trading-day horizon.")
    try:
        label_artifact = verify_label_artifact(labels_file, returns_file, crash_percentile=q, window_size=label_window_size, horizon_days=horizon_days)
    except LabelArtifactError as exc:
        raise FewShotValidationError(f"The label artifact is not valid for crash_percentile={q:.2f}: {exc}") from exc
    labels: Mapping[str, Mapping[str, str]] = label_artifact["labels"]
    verified_target_dates: Mapping[str, str] = label_artifact["target_dates"]

    indicator_fields, indicator_rows = _read_csv(indicators_file, "indicators")
    indicator_date_field = _date_field(indicator_fields, "indicators")
    feature_fields = [name for name in indicator_fields if name != indicator_date_field]
    if len(feature_fields) != expected_indicator_count or len(set(feature_fields)) != len(feature_fields):
        raise FewShotValidationError("Indicator schema must contain exactly 63 unique fields.")
    indicator_dates: list[str] = []
    matrix: list[list[float]] = []
    for row_index, row in enumerate(indicator_rows, start=2):
        current_date = _iso_date(row[indicator_date_field], f"indicators row {row_index} date")
        indicator_dates.append(current_date)
        matrix.append([_finite(row[field], f"indicators row {row_index} {field}") for field in feature_fields])
    if indicator_dates != sorted(indicator_dates) or len(set(indicator_dates)) != len(indicator_dates):
        raise FewShotValidationError("Indicator dates must be unique and strictly ordered.")

    allowed_dates: set[str] | None = None
    if valid_dates_file is not None:
        valid_fields, valid_rows = _read_csv(valid_dates_file, "valid dates")
        valid_date_field = _date_field(valid_fields, "valid dates")
        allowed_dates = {_iso_date(row[valid_date_field], "valid date") for row in valid_rows}
        if len(allowed_dates) != len(valid_rows):
            raise FewShotValidationError("Valid-dates CSV contains duplicate dates.")

    input_schema_fingerprint = canonical_sha256({"window_size": window_size, "indicator_fields": feature_fields})
    source_files = [_source_record(indicators_file, "indicators"), _source_record(labels_file, "labels"), _source_record(returns_file, "label_returns")]
    if valid_dates_file is not None:
        source_files.append(_source_record(valid_dates_file, "valid_dates"))
    source_fingerprints = {item["role"]: item["sha256"] for item in source_files}
    cases: list[dict[str, Any]] = []
    exclusions = {"outside_candidate_period": 0, "not_in_valid_dates": 0, "incomplete_window": 0, "target_unavailable": 0, "label_unavailable": 0}
    required_label_fields = ("crash_label", "min_future_logret", "crash_threshold")
    for position, cutoff in enumerate(indicator_dates):
        if cutoff < start or cutoff > end:
            exclusions["outside_candidate_period"] += 1
            continue
        if allowed_dates is not None and cutoff not in allowed_dates:
            exclusions["not_in_valid_dates"] += 1
            continue
        first = position - window_size + 1
        if first < 0:
            exclusions["incomplete_window"] += 1
            continue
        target_position = position + horizon_days
        if target_position >= len(indicator_dates):
            exclusions["target_unavailable"] += 1
            continue
        label_row = labels.get(cutoff)
        if label_row is None or any(not str(label_row[name]).strip() for name in required_label_fields):
            exclusions["label_unavailable"] += 1
            continue
        if verified_target_dates.get(cutoff) != indicator_dates[target_position]:
            raise FewShotValidationError(f"Indicator and verified return target dates disagree at {cutoff}.")
        observed_label = int(_finite(label_row["crash_label"], "crash_label"))
        tail_threshold = _finite(label_row["crash_threshold"], "crash_threshold")
        window_dates = indicator_dates[first : position + 1]
        window_values = matrix[first : position + 1]
        if len(window_values) != window_size or any(len(row) != expected_indicator_count for row in window_values):
            raise FewShotValidationError("Few-shot input window does not satisfy the 126 x 63 contract.")
        columns = list(zip(*window_values))
        if len(columns) != expected_indicator_count or any(len(column) != window_size for column in columns):
            raise FewShotValidationError("Few-shot input columns do not satisfy the 126 x 63 contract.")
        case: dict[str, Any] = {
            "schema_version": CASE_SCHEMA_VERSION,
            "asset": asset,
            "information_cutoff": cutoff,
            "forecast_target_date": indicator_dates[target_position],
            "forecast_horizon_days": horizon_days,
            "crash_percentile": q,
            "input_schema_fingerprint": input_schema_fingerprint,
            "window_observation_dates": window_dates,
            "input_hash": canonical_sha256({"indicator_fields": feature_fields, "window_observation_dates": window_dates, "values": window_values}),
            "indicator_latest": {field: window_values[-1][index] for index, field in enumerate(feature_fields)},
            "indicator_mean": {field: statistics.fmean(columns[index]) for index, field in enumerate(feature_fields)},
            "indicator_std": {field: statistics.stdev(columns[index]) for index, field in enumerate(feature_fields)},
            "observed_anomaly_label": observed_label,
            "frozen_tail_threshold": tail_threshold,
            "source_fingerprints": source_fingerprints,
        }
        case["case_id"] = f"FS{len(cases) + 1:04d}"
        cases.append(case)
    if not cases:
        raise FewShotValidationError("No complete labelled cases match the requested reference period.")

    bank_bytes = ("\n".join(canonical_json(case) for case in cases) + "\n").encode("utf-8")
    scaler = {"schema_version": SCALER_SCHEMA_VERSION, "scaler_id": SCALER_ID, "transformation": "none", "parameters": {}, "fitted": False}
    scaler_bytes = (json.dumps(scaler, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")
    manifest: dict[str, Any] = {
        "schema_version": BANK_SCHEMA_VERSION,
        "bank_id": bank_id,
        "created_at_utc": _utc_timestamp(created_at_utc),
        "mode": FEW_SHOT_MODE,
        "asset": asset,
        "candidate_start_date": start,
        "candidate_end_date": end,
        "window_size": window_size,
        "indicator_count": expected_indicator_count,
        "indicator_fields": feature_fields,
        "input_schema_fingerprint": input_schema_fingerprint,
        "forecast_horizon_days": horizon_days,
        "crash_percentile": q,
        "selection_policy": SELECTION_POLICY,
        "maximum_examples": 4,
        "scaler_id": SCALER_ID,
        "scaler_sha256": hashlib.sha256(scaler_bytes).hexdigest(),
        "bank_sha256": hashlib.sha256(bank_bytes).hexdigest(),
        "case_count": len(cases),
        "class_counts": {"ANOMALY": sum(case["observed_anomaly_label"] == 1 for case in cases), "NON_ANOMALY": sum(case["observed_anomaly_label"] == 0 for case in cases)},
        "source_files": source_files,
        "label_artifact_fingerprint": label_artifact["fingerprint"],
        "label_threshold_method": label_artifact["method"],
        "verified_label_rows": label_artifact["verified_outcome_rows"],
        "exclusion_counts": exclusions,
        "case_payload": "126_dates_plus_63_latest_mean_std_label_threshold_and_source_hashes",
    }
    manifest["manifest_hash"] = canonical_sha256(manifest)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _write_frozen_bundle(((bank_file, bank_bytes), (scaler_file, scaler_bytes), (manifest_file, manifest_bytes)))
    return manifest


__all__ = ["build_frozen_few_shot_bank"]
