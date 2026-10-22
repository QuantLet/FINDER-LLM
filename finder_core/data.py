from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from .config import (
    CRASH_PERCENTILE,
    END_DATE,
    EXPECTED_CHART_COUNT,
    HORIZON_DAYS,
    PREVIOUS_WINDOWS,
    REFERENCE_START_DATE,
    RANDOM_SEED,
    WINDOW_SIZE,
    anomaly_threshold_for,
)
from .tools import ToolContext
from .markets import require_market, market_context, news_window
from .settings import load_settings
from .news_collection import market_news_mask


class DataError(RuntimeError):
    pass


@dataclass(frozen=True)
class InputPaths:
    project_root: Path
    indicators: Path
    news_catalog: Path
    news_full_text: Path
    news_market_articles: Path
    figures_root: Path
    ohlcv: Path

    @classmethod
    def from_settings(
        cls,
        project_root: Path | str,
        settings: Mapping[str, Any] | None = None,
    ) -> "InputPaths":
        root = Path(project_root).expanduser().resolve()
        values = dict(settings) if settings is not None else load_settings(project_root)
        path_values = values.get("paths")
        paths = dict(path_values) if isinstance(path_values, Mapping) else {}

        def resolve(key: str, default: str) -> Path:
            raw = paths.get(key, values.get(f"{key}_path", default))
            candidate = Path(str(raw))
            if candidate.is_absolute():
                raise DataError(f"{key} must be a project-relative path.")
            resolved = (root / candidate).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise DataError(f"{key} leaves the project folder.") from exc
            return resolved

        ticker = str(values.get("ticker", "^GSPC"))
        return cls(
            project_root=root,
            indicators=resolve("indicators", f"Data/{ticker}/Market/df_Indicators.csv"),
            news_catalog=resolve(
                "news_catalog", "Data/News/Guardian Raw/Raw Full New List.csv"
            ),
            news_full_text=resolve("news_full_text", "Data/News/Full/Full News Complete.csv"),
            news_market_articles=resolve("news_market_articles", "Data/News/market_articles.csv"),
            figures_root=resolve("figures_root", f"Data/{ticker}/Figures"),
            ohlcv=resolve("ohlcv", f"Data/{ticker}/Market/df_OHLCV.csv"),
        )

    def prediction_csvs(self) -> tuple[Path, ...]:
        return (self.indicators, self.news_catalog, self.news_full_text, *((self.news_market_articles,) if self.news_market_articles.is_file() else ()))

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.project_root).as_posix()


@dataclass(frozen=True)
class PreparedWindow:
    window: Any
    tool_context: ToolContext
    source_manifest: dict[str, Any]
    selection: dict[str, Any]
    input_fingerprint: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_record(paths: InputPaths, path: Path) -> dict[str, Any]:
    return {
        "path": paths.relative(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _load_indexed_csv(path: Path, label: str) -> pd.DataFrame:
    if not path.is_file() or path.stat().st_size <= 0:
        raise DataError(f"Missing or empty {label} CSV: {path.name}.")
    try:
        frame = pd.read_csv(path, index_col=0, parse_dates=True)
        frame.index = pd.to_datetime(frame.index, errors="raise")
    except Exception as exc:
        raise DataError(f"Cannot read the {label} CSV.") from exc
    if frame.empty or frame.index.has_duplicates:
        raise DataError(f"The {label} CSV is empty or has duplicate dates.")
    frame = frame.sort_index()
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)
    return frame


def _safe_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _row_payload(row: pd.Series) -> dict[str, Any]:
    return {str(column): _safe_scalar(value) for column, value in row.items()}


def _statistics_payload(window: pd.DataFrame) -> dict[str, Any]:
    numeric = window.select_dtypes(include="number")
    if numeric.empty:
        raise DataError("The indicator table contains no numeric columns.")
    description = numeric.agg(["mean", "std"])
    return {
        str(column): {
            "mean": _safe_scalar(description.at["mean", column]),
            "std": _safe_scalar(description.at["std", column]),
        }
        for column in description.columns
    }


def _indicator_definitions(fields: Iterable[Any]) -> dict[str, dict[str, str]]:
    unit_overrides = {
        "roc_5d": "percentage_points",
        "roc_10d": "percentage_points",
        "roc_20d": "percentage_points",
        "dd_20": "percentage_points",
        "es_5pct_126": "decimal_log_return",
    }
    return {
        str(field): {
            "definition": "Frozen field from the local 63-field technical-indicator contract.",
            "unit": unit_overrides.get(str(field), "as_stored"),
            "lineage": "04 Technical Indicators.ipynb -> configured indicators file",
        }
        for field in fields
    }


def _clean_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    return " ".join(str(value).split())


def _load_news(paths: InputPaths) -> pd.DataFrame:
    catalog_columns = (
        "id",
        "webPublicationDate",
        "sectionName",
        "sectionId",
        "webTitle",
        "headline",
        "trailText",
    )
    full_columns = ("id", "bodyText", "lastModified", "downloaded_at_utc")
    try:
        catalog = pd.read_csv(paths.news_catalog, usecols=list(catalog_columns))
        full = pd.read_csv(paths.news_full_text, usecols=list(full_columns))
    except Exception as exc:
        raise DataError("Cannot read the local news stores.") from exc
    if catalog["id"].isna().any() or catalog["id"].duplicated().any():
        raise DataError("The news catalog requires unique article IDs.")
    if full["id"].isna().any() or full["id"].duplicated().any():
        raise DataError("The full-text store requires unique article IDs.")
    if set(full["id"]) - set(catalog["id"]):
        raise DataError("The full-text store contains IDs outside the catalog.")
    published = pd.to_datetime(catalog["webPublicationDate"], errors="coerce", utc=True)
    if published.isna().any():
        raise DataError("The news catalog contains invalid publication times.")
    catalog = catalog.copy()
    catalog["_date"] = published.dt.date
    catalog["_publication_timestamp"] = published
    catalog["_published_at"] = published.map(lambda value: value.isoformat())
    catalog = catalog.sort_values(
        ["_date", "_published_at", "id"], kind="mergesort"
    ).reset_index(drop=True)
    catalog["_index"] = catalog.groupby("_date", sort=False).cumcount() + 1
    full_by_id = full.set_index("id")
    catalog["_full_text"] = catalog["id"].map(full_by_id["bodyText"])
    catalog["_last_modified"] = catalog["id"].map(full_by_id["lastModified"])
    catalog["_downloaded_at"] = catalog["id"].map(full_by_id["downloaded_at_utc"])
    return catalog


def _news_for_window(news: pd.DataFrame, timing: Mapping[str, Any]) -> dict[str, Any]:
    start, end = pd.Timestamp(timing["start_utc"]), pd.Timestamp(timing["end_utc"])
    rows = news.loc[(news["_publication_timestamp"] >= start) & (news["_publication_timestamp"] < end)].sort_values(["_publication_timestamp", "id"]).copy()
    rows["_index"] = range(1, len(rows) + 1)
    articles: list[dict[str, Any]] = []
    for _, row in rows.iterrows():
        title = _clean_text(row.get("headline")) or _clean_text(row.get("webTitle"))
        content = row.get("_full_text")
        if pd.isna(content) or not str(content).strip():
            continue
        version = pd.to_datetime(row.get("_last_modified"), errors="coerce", utc=True)
        if pd.notna(version) and version >= end:
            continue
        if pd.isna(version) and _clean_text(row.get("_last_modified")):
            continue
        articles.append(
            {
                "index": int(row["_index"]),
                "title": title,
                "section": _clean_text(row.get("sectionName")),
                "published_at": str(row["_published_at"]),
                "article_id": str(row["id"]),
                "trail_text": _clean_text(row.get("trailText")),
                "content": str(content),
                "content_version_at": _clean_text(row.get("_last_modified")) or None,
                "downloaded_at": _clean_text(row.get("_downloaded_at")) or None,
            }
        )
    return {
        "date": timing["forecast_target_date"],
        "news_window": dict(timing),
        "articles": articles,
        "article_count": len(articles),
        "title_index_scope": "all_articles_in_market_preopen_interval",
        "index_definition": "one_based_by_publication_then_article_id_within_window_before_body_filter",
        "content_availability_policy": "complete_body_only_exclude_known_post_cutoff_updates",
        "point_in_time_gate_passed": False,
    }


def _chart_paths(paths: InputPaths, ticker: str, frame: pd.DataFrame, context_window_size: int) -> tuple[Path, ...]:
    from .charts import context_chart_paths
    return context_chart_paths(frame, ticker, paths.figures_root, context_window_size)


def _target_event_spec(indicators: pd.DataFrame, position: int, crash_percentile: float, label_window_size: int = WINDOW_SIZE, historical_returns: pd.Series | None = None) -> dict[str, Any]:
    history = indicators.iloc[:position]
    returns = np.log(history["close"]).diff().iloc[-label_window_size - 1:-1] if historical_returns is None else historical_returns[historical_returns.index < history.index[-1]].tail(label_window_size)
    if len(returns) != label_window_size or not np.isfinite(returns.to_numpy(dtype=float)).all():
        raise DataError(f"The target tail threshold requires {label_window_size} complete past returns before the cutoff return.")
    return {"target_tail_threshold": float(returns.quantile(crash_percentile)), "crash_percentile": crash_percentile, "return_units": "decimal_log_return", "return_definition": "log(target_close / previous_session_close)", "threshold_observations": label_window_size, "threshold_sample_start": returns.index[0].date().isoformat(), "threshold_sample_end": returns.index[-1].date().isoformat(), "market_data_cutoff_date": history.index[-1].date().isoformat(), "forecast_target_date": indicators.index[position].date().isoformat(), "threshold_rule": f"past_{label_window_size}_log_returns_excluding_cutoff_return"}


def _complete(paths: Iterable[Path]) -> bool:
    files = tuple(paths)
    return len(files) == EXPECTED_CHART_COUNT and all(
        path.is_file() and path.stat().st_size > 0 for path in files
    )


def _fixed_method_settings(settings: Mapping[str, Any]) -> None:
    expected = {
        "window_size": WINDOW_SIZE,
        "horizon_days": HORIZON_DAYS,
        "chart_count": EXPECTED_CHART_COUNT,
        "random_seed": RANDOM_SEED,
    }
    for key, value in expected.items():
        if key in settings and settings[key] != value:
            raise DataError(f"{key} does not match the frozen prediction method.")
    crash_percentile = float(settings.get("crash_percentile", CRASH_PERCENTILE))
    context_window_size = settings.get("context_window_size", 21)
    if type(context_window_size) is not int or not 2 <= context_window_size <= WINDOW_SIZE:
        raise DataError("context_window_size must be between 2 and the unchanged 126-observation history.")
    try:
        anomaly_threshold_for(crash_percentile, float(settings.get("anomaly_threshold", 0.5)))
    except Exception as exc:
        raise DataError("crash_percentile or the independently configured anomaly_threshold is invalid.") from exc


def _load_inputs(
    project_root: Path | str,
    settings: Mapping[str, Any] | None,
    *, include_news: bool = True,
) -> tuple[InputPaths, pd.DataFrame, pd.DataFrame, list[int]]:
    values = dict(settings) if settings is not None else load_settings(project_root)
    require_market(values)
    _fixed_method_settings(values)
    paths = InputPaths.from_settings(project_root, values)
    missing = [path.name for path in (paths.prediction_csvs() if include_news else (paths.indicators,)) if not path.is_file()]
    if missing:
        raise DataError(f"Missing prediction inputs: {missing}.")
    indicators = _load_indexed_csv(paths.indicators, "indicators")
    if "indicator_size" in values and int(values["indicator_size"]) != len(indicators.columns):
        raise DataError("indicator_size does not match the indicator table.")
    analysis_end = pd.Timestamp(values.get("analysis_end_date", END_DATE))
    indicators = indicators.loc[indicators.index <= analysis_end]
    news = pd.DataFrame()
    if include_news:
        news = _load_news(paths)
        associations = pd.read_csv(paths.news_market_articles, usecols=["market_id", "article_id"]) if paths.news_market_articles.is_file() else None
        news = news.loc[market_news_mask(news, values["market"], associations)].copy()
    index = indicators.index
    reference_date = date.fromisoformat(
        str(values.get("analysis_start_date", REFERENCE_START_DATE.isoformat()))
    )
    start_position = int(index.searchsorted(pd.Timestamp(reference_date)))
    if start_position < PREVIOUS_WINDOWS:
        raise DataError("The indicator table lacks previous-window context.")
    first_position = start_position + WINDOW_SIZE
    positions = list(range(first_position, len(indicators) - HORIZON_DAYS + 1))
    if not positions:
        raise DataError("No complete prediction window is available.")
    return paths, indicators, news, positions


def list_prediction_windows(
    project_root: Path | str,
    settings: Mapping[str, Any] | None = None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    max_windows: int | None = None,
) -> list[dict[str, Any]]:
    if start_date and end_date and start_date > end_date:
        raise DataError("start_date cannot be after end_date.")
    if max_windows is not None and max_windows <= 0:
        raise DataError("max_windows must be positive.")
    values = dict(settings) if settings is not None else load_settings(project_root)
    paths, indicators, news, positions = _load_inputs(project_root, values)
    ticker = values["ticker"]
    context_window_size = int(values.get("context_window_size", 21))
    rows: list[dict[str, Any]] = []
    for position in positions:
        prediction_date = indicators.index[position - 1].date()
        if start_date and prediction_date < start_date:
            continue
        if end_date and prediction_date > end_date:
            continue
        charts = _chart_paths(paths, ticker, indicators.iloc[position - WINDOW_SIZE:position], context_window_size)
        timing = news_window(values["market"], prediction_date, indicators.index[position].date(), session_dates=indicators.index[position-context_window_size:position].date)
        available_news_count = _news_for_window(news, timing)["article_count"]
        rows.append(
            {
                "window_no": len(rows) + 1,
                "prediction_date": prediction_date.isoformat(),
                "target_date": indicators.index[position].date().isoformat(),
                "chart_count": len(charts),
                "charts_ready": _complete(charts),
                "context_window_size": context_window_size,
                "news_count": available_news_count,
                "news_start_utc": timing["start_utc"],
                "news_cutoff_utc": timing["end_utc"],
                "indicator_position": position,
            }
        )
        if max_windows is not None and len(rows) >= max_windows:
            break
    return rows


def prepare_prediction_windows(
    project_root: Path | str,
    settings: Mapping[str, Any] | None = None,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    max_windows: int | None = None,
    include_news: bool = True,
) -> tuple[PreparedWindow, ...]:
    from .workflow import WorkflowWindow, render_prompt

    if start_date and end_date and start_date > end_date:
        raise DataError("start_date cannot be after end_date.")
    if max_windows is not None and max_windows <= 0:
        raise DataError("max_windows must be positive.")
    values = dict(settings) if settings is not None else load_settings(project_root)
    paths, indicators, news, positions = _load_inputs(project_root, values, include_news=include_news)
    ticker = str(values.get("ticker", "^GSPC"))
    crash_percentile = float(values.get("crash_percentile", CRASH_PERCENTILE))
    anomaly_threshold = anomaly_threshold_for(crash_percentile, float(values.get("anomaly_threshold", 0.5)))
    context_window_size = int(values.get("context_window_size", 21))
    reference_start_date = date.fromisoformat(str(values.get("analysis_start_date", REFERENCE_START_DATE.isoformat())))
    base_records = [_file_record(paths, path) for path in (paths.prediction_csvs() if include_news else (paths.indicators,))]
    from .overseas import overseas_source_paths, prepare_overseas_context
    base_records.extend(_file_record(paths, path) for path in overseas_source_paths(paths.project_root))
    if paths.ohlcv.is_file() and paths.ohlcv.stat().st_size > 0:
        base_records.insert(0, _file_record(paths, paths.ohlcv))
    prepared: list[PreparedWindow] = []
    label_window_size = int(values.get("label_window_size", 252))
    historical_returns = np.log(_load_indexed_csv(paths.ohlcv, "ohlcv")["Close"]).diff().dropna() if label_window_size != WINDOW_SIZE else None
    for position in positions:
        prediction_date = indicators.index[position - 1].date()
        if start_date and prediction_date < start_date:
            continue
        if end_date and prediction_date > end_date:
            continue
        current = indicators.iloc[position - WINDOW_SIZE : position]
        previous = [
            indicators.iloc[position - WINDOW_SIZE - offset : position - offset]
            for offset in range(1, PREVIOUS_WINDOWS + 1)
        ]
        if len(current) != WINDOW_SIZE or any(len(frame) != WINDOW_SIZE for frame in previous):
            raise DataError("A numeric evidence window is incomplete.")
        from .charts import ensure_context_charts
        charts = ensure_context_charts(current, ticker, paths.figures_root, context_window_size)
        context = current.tail(context_window_size)
        target_date = indicators.index[position].date()
        timing = news_window(values["market"], prediction_date, target_date, session_dates=indicators.index[position-context_window_size:position].date)
        target_event_spec = _target_event_spec(indicators, position, crash_percentile, label_window_size, historical_returns)
        global_market_context = prepare_overseas_context(paths.project_root, timing["end_utc"], ticker)
        news_evidence = _news_for_window(news, timing) if include_news else {"articles": [], "article_count": 0}
        available_news_count = sum(
            bool(article.get("content")) for article in news_evidence["articles"]
        )
        numeric_evidence = {
            "date": prediction_date.isoformat(),
            "window_size": WINDOW_SIZE,
            "context_window_size": context_window_size,
            "horizon_days": HORIZON_DAYS,
            "crash_percentile": crash_percentile,
            "anomaly_threshold": anomaly_threshold,
            "target_event_spec": target_event_spec,
            "global_market_context": global_market_context,
            "current_technical_summary": _row_payload(current.iloc[-1]),
            "current_window_context": {
                "start_date": context.index[0].date().isoformat(),
                "end_date": prediction_date.isoformat(),
                "observations": len(context),
                "mean_and_std": _statistics_payload(context),
            },
            "previous_indicator_series": {
                "dates": [frame.index[-1].date().isoformat() for frame in reversed(previous)],
                "values": {field: [_row_payload(frame.iloc[-1])[field] for frame in reversed(previous)] for field in current.columns},
            },
        }
        target_date = indicators.index[position].date()
        records = [*base_records, *(_file_record(paths, path) for path in charts)]
        manifest = {
            "origin": "local_notebook_outputs_and_news_stores",
            "market": values["market"],
            "market_fingerprint": values["market_fingerprint"],
            "news_window": timing,
            "news_lookback_trading_days": context_window_size,
            "context_window_size": context_window_size,
            "target_event_spec": target_event_spec,
            "global_market_context_fingerprint": _canonical_fingerprint(global_market_context),
            "global_market_dataset_fingerprint": global_market_context.get("dataset_fingerprint"),
            "information_cutoff": prediction_date.isoformat(),
            "files": records,
        }
        tool_context = ToolContext(
            window_id=f"W{position - positions[0] + 1:04d}",
            asset=ticker,
            information_cutoff=prediction_date,
            forecast_target_date=target_date,
            indicator_history=current,
            numeric_snapshot=numeric_evidence,
            chart_paths=charts,
            news_articles=news_evidence["articles"],
            indicator_definitions=_indicator_definitions(current.columns),
            source_manifest=manifest,
            crash_percentile=crash_percentile,
            anomaly_threshold=anomaly_threshold,
            horizon_days=HORIZON_DAYS,
        )
        window = WorkflowWindow(
            window_id=f"W{position - positions[0] + 1:04d}",
            asset=ticker,
            information_cutoff=prediction_date,
            forecast_target_date=target_date,
            window_start_date=current.index[0].date(),
            window_end_date=prediction_date,
            reference_start_date=reference_start_date,
            horizon_days=HORIZON_DAYS,
            crash_percentile=crash_percentile,
            anomaly_threshold=anomaly_threshold,
            market=market_context(values["market"]),
            news_window=timing,
            task_definition=render_prompt(
                "shared.task_definition",
                INFORMATION_CUTOFF=prediction_date.isoformat(),
                NEWS_CUTOFF_UTC=timing["end_utc"],
                FORECAST_TARGET_DATE=target_date.isoformat(),
                HORIZON_DAYS=HORIZON_DAYS,
                CRASH_PERCENTILE=crash_percentile,
                ANOMALY_THRESHOLD=anomaly_threshold,
            ),
        )
        window.validate()
        fingerprint = _canonical_fingerprint(
            {
                "selection": {
                    "prediction_date": prediction_date.isoformat(),
                    "target_date": target_date.isoformat(),
                    "indicator_position": position,
                    "observation_dates": [timestamp.date().isoformat() for timestamp in current.index],
                },
                "source_records": records,
                "tool_context": tool_context.public_descriptor(),
            }
        )
        prepared.append(
            PreparedWindow(
                window=window,
                tool_context=tool_context,
                source_manifest=manifest,
                selection={
                    "prediction_date": prediction_date.isoformat(),
                    "target_date": target_date.isoformat(),
                    "indicator_position": position,
                },
                input_fingerprint=fingerprint,
            )
        )
        if max_windows is not None and len(prepared) >= max_windows:
            break
    if not prepared:
        raise DataError("No complete prediction window matches the request.")
    return tuple(prepared)
