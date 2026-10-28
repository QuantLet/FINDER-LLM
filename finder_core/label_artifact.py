from __future__ import annotations

import csv
import math
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .memory import canonical_sha256, file_sha256


LABEL_METHOD = "rolling-lower-tail-quantile-through-cutoff"
SUPPORTED_CRASH_PERCENTILES = (0.01, 0.05)


class LabelArtifactError(RuntimeError):
    pass


class LabelArtifactValidationError(LabelArtifactError):
    pass


def _iso_date(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise LabelArtifactValidationError(f"{name} must use YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LabelArtifactValidationError(f"{name} must use YYYY-MM-DD.") from exc
    if parsed.isoformat() != value:
        raise LabelArtifactValidationError(f"{name} must use canonical YYYY-MM-DD.")
    return value


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LabelArtifactValidationError(f"{name} must be numeric.") from exc
    if not math.isfinite(number):
        raise LabelArtifactValidationError(f"{name} must be finite.")
    return number


def _optional_finite(value: Any, name: str) -> float | None:
    if value is None or not str(value).strip():
        return None
    return _finite(value, name)


def _read_csv(path: Path, label: str) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except OSError as exc:
        raise LabelArtifactError(f"Cannot read {label} CSV.") from exc
    if not fields or not rows:
        raise LabelArtifactValidationError(f"{label} CSV is empty.")
    return fields, rows


def _single_field(fields: list[str], expected: str, label: str) -> str:
    matches = [field for field in fields if field.lower() == expected.lower()]
    if len(matches) != 1:
        raise LabelArtifactValidationError(f"{label} CSV must contain exactly one {expected} column.")
    return matches[0]


def verify_label_artifact(labels_path: Path | str, returns_path: Path | str, *, crash_percentile: float, window_size: int = 126, horizon_days: int = 1, tolerance: float = 1e-12) -> dict[str, Any]:
    labels_file = Path(labels_path)
    returns_file = Path(returns_path)
    q = _finite(crash_percentile, "crash_percentile")
    if not any(math.isclose(q, supported, rel_tol=0.0, abs_tol=1e-12) for supported in SUPPORTED_CRASH_PERCENTILES):
        raise LabelArtifactValidationError("crash_percentile must be 0.01 or 0.05.")
    if window_size not in {126, 252} or horizon_days != 1:
        raise LabelArtifactValidationError("The label artifact requires a 126- or 252-observation window and one-observation horizon.")

    return_fields, return_rows = _read_csv(returns_file, "log returns")
    return_date_field = _single_field(return_fields, "date", "log returns")
    return_value_field = _single_field(return_fields, "logreturn", "log returns")
    return_dates: list[str] = []
    return_values: list[float] = []
    for row_number, row in enumerate(return_rows, start=2):
        current_date = _iso_date(row[return_date_field], f"log returns row {row_number} date")
        return_dates.append(current_date)
        value = _optional_finite(row[return_value_field], f"log returns row {row_number} logreturn")
        return_values.append(float("nan") if value is None else value)
    if return_dates != sorted(return_dates) or len(set(return_dates)) != len(return_dates):
        raise LabelArtifactValidationError("Log-return dates must be unique and strictly ordered.")
    return_position = {current_date: position for position, current_date in enumerate(return_dates)}
    series = pd.Series(return_values, index=return_dates, dtype="float64")
    expected_thresholds = series.rolling(window_size, min_periods=window_size).quantile(q)

    label_fields, label_rows = _read_csv(labels_file, "labels")
    label_date_field = _single_field(label_fields, "date", "labels")
    required_fields = {"crash_label", "min_future_logret", "crash_threshold"}
    if not required_fields.issubset(label_fields):
        raise LabelArtifactValidationError("Labels CSV is missing the frozen outcome fields.")
    labels: dict[str, dict[str, str]] = {}
    target_dates: dict[str, str] = {}
    verified_threshold_rows = 0
    verified_outcome_rows = 0
    for row_number, row in enumerate(label_rows, start=2):
        current_date = _iso_date(row[label_date_field], f"labels row {row_number} date")
        if current_date in labels or current_date not in return_position:
            raise LabelArtifactValidationError("Label dates must be unique and present in the log-return artifact.")
        labels[current_date] = row
        position = return_position[current_date]
        if position + horizon_days < len(return_dates):
            target_dates[current_date] = return_dates[position + horizon_days]
        expected_threshold = expected_thresholds.iloc[position]
        threshold = _optional_finite(row["crash_threshold"], f"labels row {row_number} crash_threshold")
        if pd.isna(expected_threshold):
            if threshold is not None:
                raise LabelArtifactValidationError(f"Label threshold is available before the required observation history at {current_date}.")
        else:
            if threshold is None or not math.isclose(threshold, float(expected_threshold), rel_tol=0.0, abs_tol=tolerance):
                raise LabelArtifactValidationError(f"Label threshold does not match crash_percentile={q:.2f} at {current_date}.")
            verified_threshold_rows += 1

        label_value = _optional_finite(row["crash_label"], f"labels row {row_number} crash_label")
        realized_return = _optional_finite(row["min_future_logret"], f"labels row {row_number} min_future_logret")
        if (label_value is None) != (realized_return is None):
            raise LabelArtifactValidationError(f"Label and realized return availability disagree at {current_date}.")
        target_values = return_values[position + 1 : position + horizon_days + 1]
        target_available = len(target_values) == horizon_days and all(math.isfinite(value) for value in target_values)
        if threshold is not None and target_available:
            if label_value is None or realized_return is None:
                raise LabelArtifactValidationError(f"A mature label outcome is missing at {current_date}.")
            if label_value not in {0.0, 1.0}:
                raise LabelArtifactValidationError(f"Crash label must be binary at {current_date}.")
            expected_return = min(target_values)
            if not math.isclose(realized_return, expected_return, rel_tol=0.0, abs_tol=tolerance):
                raise LabelArtifactValidationError(f"Realized return does not match the one-observation horizon at {current_date}.")
            if int(label_value) != int(realized_return <= threshold):
                raise LabelArtifactValidationError(f"Crash label disagrees with its verified threshold at {current_date}.")
            verified_outcome_rows += 1
        elif label_value is not None or realized_return is not None:
            raise LabelArtifactValidationError(f"Outcome fields are available without a mature verified threshold at {current_date}.")
    if verified_threshold_rows == 0:
        raise LabelArtifactValidationError("Labels CSV contains no threshold row that can verify the requested crash percentile.")

    labels_sha256 = file_sha256(labels_file)
    returns_sha256 = file_sha256(returns_file)
    fingerprint = canonical_sha256({
        "method": LABEL_METHOD,
        "labels_sha256": labels_sha256,
        "returns_sha256": returns_sha256,
        "crash_percentile": q,
        "window_size": window_size,
        "horizon_days": horizon_days,
        "verified_threshold_rows": verified_threshold_rows,
        "verified_outcome_rows": verified_outcome_rows,
    })
    return {
        "labels": labels,
        "target_dates": target_dates,
        "crash_percentile": q,
        "method": LABEL_METHOD,
        "window_size": window_size,
        "horizon_days": horizon_days,
        "verified_threshold_rows": verified_threshold_rows,
        "verified_outcome_rows": verified_outcome_rows,
        "labels_sha256": labels_sha256,
        "returns_sha256": returns_sha256,
        "fingerprint": fingerprint,
    }


__all__ = ["LABEL_METHOD", "LabelArtifactError", "LabelArtifactValidationError", "verify_label_artifact"]
