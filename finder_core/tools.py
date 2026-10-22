from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd
import numpy as np

from .charts import CHART_PACKET_SPEC, prepare_chart_packet
from .config import CHART_EXPERT, NEWS_ASSISTANT, NEWS_EXPERT, NEWS_READER, QUANTITATIVE_EXPERT


INPUT_PLAN = {
    QUANTITATIVE_EXPERT: "numeric_snapshot_and_definitions",
    CHART_EXPERT: "complete_charts_and_manifest",
    NEWS_READER: "news_candidates",
    NEWS_ASSISTANT: "all_selected_news_content",
    NEWS_EXPERT: "all_selected_news_summaries",
}

MAX_HISTORY_LOOKBACK = 126
_FORBIDDEN_INPUT_KEYS = {
    "accuracy",
    "evaluation",
    "future_market_data",
    "future_return",
    "label",
    "observed_anomaly",
    "outcome",
    "performance",
    "real_anomaly",
    "target_label",
}


class ToolError(RuntimeError):
    pass


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode("ascii")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _safe_scalar(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    return _safe_scalar(value)


def _assert_no_forbidden_keys(value: Any, *, source: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace(" ", "_")
            if normalized in _FORBIDDEN_INPUT_KEYS or normalized.endswith("_label"):
                raise ToolError(f"{source} contains prediction-forbidden field {key!r}.")
            _assert_no_forbidden_keys(item, source=source)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_forbidden_keys(item, source=source)


def _normalize_date(value: str | date | datetime, field_name: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ToolError(f"{field_name} must use YYYY-MM-DD.") from exc


def _ascii_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise ToolError(f"{field_name} must be an ASCII identifier.")
    return value


@dataclass(frozen=True, init=False)
class ToolContext:
    window_id: str
    asset: str
    information_cutoff: str
    forecast_target_date: str
    window_start_date: str
    window_end_date: str
    horizon_days: int
    context_window_size: int
    crash_percentile: float
    anomaly_threshold: float
    _indicator_history: pd.DataFrame = field(repr=False)
    _numeric_snapshot: dict[str, Any] = field(repr=False)
    _chart_paths: tuple[Path, ...] = field(repr=False)
    _chart_manifest: dict[str, Any] = field(repr=False)
    _news_articles: tuple[dict[str, Any], ...] = field(repr=False)
    _indicator_definitions: dict[str, dict[str, Any]] = field(repr=False)
    _source_manifest: dict[str, Any] = field(repr=False)
    _source_manifest_sha256: str = field(repr=False)
    _input_schema_fingerprint: str = field(repr=False)

    def __init__(
        self,
        *,
        window_id: str,
        asset: str,
        information_cutoff: str | date | datetime,
        forecast_target_date: str | date | datetime,
        indicator_history: pd.DataFrame,
        numeric_snapshot: Mapping[str, Any],
        chart_paths: Sequence[Path | str],
        news_articles: Sequence[Mapping[str, Any]],
        indicator_definitions: Mapping[str, Mapping[str, Any]],
        source_manifest: Mapping[str, Any],
        crash_percentile: float,
        anomaly_threshold: float,
        horizon_days: int = 1,
        chart_manifest: Mapping[str, Any] | None = None,
    ) -> None:
        window_id = _ascii_identifier(window_id, "window_id")
        if not isinstance(asset, str) or not asset.strip() or not asset.isascii():
            raise ToolError("asset must be a non-empty ASCII string.")
        cutoff = _normalize_date(information_cutoff, "information_cutoff")
        target = _normalize_date(forecast_target_date, "forecast_target_date")
        if target <= cutoff:
            raise ToolError("forecast_target_date must be after information_cutoff.")
        if not isinstance(indicator_history, pd.DataFrame) or len(indicator_history) != MAX_HISTORY_LOOKBACK:
            raise ToolError(f"indicator_history must contain exactly {MAX_HISTORY_LOOKBACK} frozen observations.")
        history = indicator_history.copy(deep=True)
        if history.empty or history.columns.duplicated().any() or history.index.has_duplicates:
            raise ToolError("indicator_history must have unique dates and fields.")
        if history.shape[1] != 63:
            raise ToolError("indicator_history must contain exactly 63 numeric fields.")
        try:
            history = history.apply(pd.to_numeric, errors="raise")
        except (TypeError, ValueError) as exc:
            raise ToolError("indicator_history must contain only numeric values.") from exc
        if history.isna().any().any() or not np.isfinite(history.to_numpy(dtype=float)).all():
            raise ToolError("indicator_history must not contain missing or infinite values.")
        history.index = pd.to_datetime(history.index, errors="raise")
        if getattr(history.index, "tz", None) is not None:
            history.index = history.index.tz_localize(None)
        history = history.sort_index()
        if history.index[-1].date().isoformat() != cutoff:
            raise ToolError("indicator_history must end at information_cutoff.")
        if any(str(column).strip().lower() in _FORBIDDEN_INPUT_KEYS or str(column).strip().lower().endswith("_label") for column in history.columns):
            raise ToolError("indicator_history contains a prediction-forbidden column.")
        numeric = _json_copy(numeric_snapshot)
        context_window_size = numeric.get("context_window_size", len(history))
        if type(context_window_size) is not int or not 2 <= context_window_size <= len(history):
            raise ToolError("context_window_size must be between 2 and the complete indicator history length.")
        event_spec = numeric.get("target_event_spec", {})
        if event_spec and (event_spec.get("market_data_cutoff_date") != cutoff or event_spec.get("forecast_target_date") != target or event_spec.get("threshold_observations") not in {126, 252, 504} or event_spec.get("threshold_sample_end", cutoff) >= cutoff or event_spec.get("return_units") != "decimal_log_return" or not np.isfinite(float(event_spec.get("target_tail_threshold", float("nan"))))):
            raise ToolError("The target tail specification must describe a finite threshold calculated before the cutoff return.")
        articles = tuple(_json_copy(article) for article in news_articles)
        definitions = {str(key): _json_copy(value) for key, value in indicator_definitions.items()}
        sources = _json_copy(source_manifest)
        _assert_no_forbidden_keys(numeric, source="numeric_snapshot")
        _assert_no_forbidden_keys(articles, source="news_articles")
        _assert_no_forbidden_keys(definitions, source="indicator_definitions")
        _assert_no_forbidden_keys(sources, source="source_manifest")
        if set(definitions) - {str(column) for column in history.columns}:
            raise ToolError("indicator_definitions contains fields outside the frozen numeric contract.")
        timing = sources.get("news_window")
        if timing:
            from .markets import news_window, market_fingerprint
            market = sources.get("market", {})
            lookback_days = sources.get("news_lookback_days", timing.get("news_lookback_days", 7))
            expected_timing = news_window(market, cutoff, target, session_dates=history.index[-context_window_size:].date) if timing.get("lookback_basis") == "observed_trading_sessions" else news_window(market, cutoff, target, lookback_days=lookback_days)
            if market.get("market_id") != asset or sources.get("market_fingerprint") != market_fingerprint(market) or timing != expected_timing:
                raise ToolError("Market identity or scheduled news boundary differs from the frozen source manifest.")
            start, end = pd.Timestamp(timing["start_utc"]), pd.Timestamp(timing["end_utc"])
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ToolError("News window requires increasing timezone-aware boundaries.")
            for article in articles:
                published = pd.Timestamp(article["published_at"])
                version = pd.Timestamp(article["content_version_at"]) if article.get("content_version_at") else None
                if published.tzinfo is None or not start <= published < end or (version is not None and (version.tzinfo is None or not version < end)):
                    raise ToolError("Article publication or content version crosses the news boundary.")
        indices = [article.get("index") for article in articles]
        if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indices) or len(set(indices)) != len(indices):
            raise ToolError("news_articles requires unique non-negative stable indices.")
        packet, generated_manifest = prepare_chart_packet(tuple(Path(path).resolve() for path in chart_paths))
        manifest = _json_copy(generated_manifest)
        if chart_manifest is not None and _json_copy(chart_manifest) != manifest:
            raise ToolError("chart_manifest does not match the complete chart bytes frozen at context creation.")
        _assert_no_forbidden_keys(manifest, source="chart_manifest")
        percentile = float(crash_percentile)
        threshold = float(anomaly_threshold)
        if isinstance(horizon_days, bool) or horizon_days != 1:
            raise ToolError("horizon_days is fixed at one trading day.")
        if percentile not in {0.01, 0.05} or not 0 < threshold <= 1:
            raise ToolError("crash_percentile must be 0.01 or 0.05 and anomaly_threshold must be in (0, 1].")
        object.__setattr__(self, "window_id", window_id)
        object.__setattr__(self, "asset", asset.strip())
        object.__setattr__(self, "information_cutoff", cutoff)
        object.__setattr__(self, "forecast_target_date", target)
        object.__setattr__(self, "window_start_date", history.index[0].date().isoformat())
        object.__setattr__(self, "window_end_date", history.index[-1].date().isoformat())
        object.__setattr__(self, "horizon_days", horizon_days)
        object.__setattr__(self, "context_window_size", context_window_size)
        object.__setattr__(self, "crash_percentile", percentile)
        object.__setattr__(self, "anomaly_threshold", threshold)
        object.__setattr__(self, "_indicator_history", history)
        object.__setattr__(self, "_numeric_snapshot", numeric)
        object.__setattr__(self, "_chart_paths", packet)
        object.__setattr__(self, "_chart_manifest", manifest)
        object.__setattr__(self, "_news_articles", articles)
        object.__setattr__(self, "_indicator_definitions", definitions)
        object.__setattr__(self, "_source_manifest", sources)
        object.__setattr__(self, "_source_manifest_sha256", _sha256_value(sources))
        schema_contract = {
            "numeric_fields": [{"name": str(column), "dtype": str(history[column].dtype)} for column in history.columns],
            "numeric_observation_count": len(history),
            "model_context_observation_count": context_window_size,
            "numeric_context_fields": sorted(numeric),
            "chart_contract": _json_copy(CHART_PACKET_SPEC),
            "news_article_fields": sorted({str(key) for article in articles for key in article}),
        }
        object.__setattr__(self, "_input_schema_fingerprint", _sha256_value(schema_contract))

    def public_descriptor(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "asset": self.asset,
            "information_cutoff": self.information_cutoff,
            "forecast_target_date": self.forecast_target_date,
            "window_start_date": self.window_start_date,
            "window_end_date": self.window_end_date,
            "horizon_days": self.horizon_days,
            "context_window_size": self.context_window_size,
            "target_event_spec": _json_copy(self._numeric_snapshot.get("target_event_spec", {})),
            "crash_percentile": self.crash_percentile,
            "anomaly_threshold": self.anomaly_threshold,
            "market_data_cutoff_date": self.information_cutoff,
            "news_window": _json_copy(self._source_manifest.get("news_window", {})),
            "market_fingerprint": self._source_manifest.get("market_fingerprint"),
            "source_manifest_sha256": self._source_manifest_sha256,
            "input_schema_fingerprint": self._input_schema_fingerprint,
        }

    def public_dict(self) -> dict[str, Any]:
        return self.public_descriptor()

    @property
    def chart_paths(self) -> tuple[Path, ...]:
        return self._chart_paths

    @property
    def chart_hashes(self) -> tuple[str, ...]:
        return tuple(str(item["source_sha256"]) for item in self._chart_manifest["assigned_charts"])

    @property
    def observation_dates(self) -> tuple[str, ...]:
        return tuple(timestamp.date().isoformat() for timestamp in self._indicator_history.index)

    @property
    def indicator_fields(self) -> tuple[str, ...]:
        return tuple(str(column) for column in self._indicator_history.columns)

    @property
    def current_numeric_reference(self) -> dict[str, Any]:
        result = {"information_cutoff": self.information_cutoff, "forecast_target_date": self.forecast_target_date, "latest_values": {str(field): float(value) for field, value in self._indicator_history.iloc[-1].items()}, "context_window_size": self.context_window_size}
        result.update({key: _json_copy(self._numeric_snapshot[key]) for key in ("target_event_spec", "current_window_context", "global_market_context", "previous_indicator_series") if key in self._numeric_snapshot})
        return result

    @property
    def input_schema_fingerprint(self) -> str:
        return self._input_schema_fingerprint


@dataclass(frozen=True)
class ToolReceipt:
    receipt_id: str
    window_id: str
    agent_id: str
    tool_name: str
    payload: dict[str, Any] = field(repr=False)
    payload_sha256: str

    def to_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        result = self.audit_dict()
        if include_payload:
            result["payload"] = copy.deepcopy(self.payload)
        return result

    def audit_dict(self) -> dict[str, Any]:
        return {"receipt_id": self.receipt_id, "window_id": self.window_id, "agent_id": self.agent_id, "tool_name": self.tool_name, "payload_sha256": self.payload_sha256}


class ToolRegistry:


    def __init__(self, context: ToolContext) -> None:
        self.context = context

    def _receipt(self, agent_id: str, index: str, payload: dict[str, Any]) -> ToolReceipt:
        return ToolReceipt(receipt_id=f"{self.context.window_id}-{index}", window_id=self.context.window_id, agent_id=agent_id, tool_name=INPUT_PLAN[agent_id], payload=payload, payload_sha256=_sha256_value(payload))

    def quantitative(self) -> ToolReceipt:
        payload = copy.deepcopy(self.context._numeric_snapshot)
        return self._receipt(QUANTITATIVE_EXPERT, "Q01", payload)

    def charts(self) -> ToolReceipt:
        payload = copy.deepcopy(self.context._chart_manifest)
        return self._receipt(CHART_EXPERT, "C01", payload)

    def _event_timing(self):
        from .future_events import with_future_horizon
        return with_future_horizon(_json_copy(self.context._source_manifest["news_window"]), self.context._source_manifest["market"]["session"]["calendar_id"], self.context.context_window_size)

    def news_candidates(self) -> ToolReceipt:
        from .future_events import source_id
        from .news_sources import candidate_previews
        timing = self._event_timing()
        candidates = [{"index": item["index"], "source_id": source_id(item), "title": item.get("title", ""), "section": item.get("section", ""), "published_at": item.get("published_at", ""), "lead": item["content"][:240], "source_passages": candidate_previews(item, timing)} for item in self.context._news_articles if str(item.get("content", "")).strip()]
        return self._receipt(NEWS_READER, "NR01", {"candidates": candidates, "news_window": timing})

    def news_content(self, selected_indices: Sequence[int]) -> ToolReceipt:
        from .future_events import source_id
        articles = {item["index"]: item for item in self.context._news_articles}
        selected = []
        for index in selected_indices:
            item = articles[index]
            selected.append({"index": index, "source_id": source_id(item), "summary_key": f"N{index:03d}", "title": item.get("title", ""), "published_at": item.get("published_at", ""), "content": str(item["content"])})
        return self._receipt(NEWS_ASSISTANT, "NA01", {"articles": selected})

    def news_summaries(self, summaries: Sequence[Mapping[str, Any]], *, event_timeline: Any = None, future_events: Any = None, event_sources: Any = None) -> ToolReceipt:
        articles = {item["index"]: item for item in self.context._news_articles}
        rows = [{**copy.deepcopy(dict(item)), "article_ref": f"N{item['index']:03d}", "title": articles[item["index"]].get("title", ""), "published_at": articles[item["index"]].get("published_at", "")} for item in summaries]
        payload = {"summaries": rows, "future_events": copy.deepcopy(future_events or []), "event_sources": copy.deepcopy(event_sources or {}), "news_window": self._event_timing()}
        if event_timeline is not None:
            payload["event_timeline"] = _json_copy(event_timeline)
        return self._receipt(NEWS_EXPERT, "NE01", payload)
