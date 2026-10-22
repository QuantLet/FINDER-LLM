from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo


class MarketError(ValueError):
    pass


PATH_TEMPLATES = {
    "benchmark": "Runs/{market_id}/Benchmark/benchmark_predictions.csv", "evaluation": "Runs/{market_id}/Evaluation",
    "figures_root": "Data/{market_id}/Figures", "full_data": "Data/{market_id}/Market/df_Full.csv",
    "indicator_list": "Data/{market_id}/Market/indicators_list.txt", "indicator_list_small": "Data/{market_id}/Market/indicators_list_small.txt",
    "indicators": "Data/{market_id}/Market/df_Indicators.csv", "indicators_small": "Data/{market_id}/Market/df_Indicators_small.csv",
    "labels": "Data/{market_id}/Market/df_Crash_Label.csv", "log_returns": "Data/{market_id}/Market/df_Log_Returns.csv",
    "memory": "Runs/{market_id}/Memory", "ohlcv": "Data/{market_id}/Market/df_OHLCV.csv",
    "quantile_series": "Data/{market_id}/Market/df_Quantile_Series.csv", "runs": "Runs/{market_id}",
    "few_shot_bank": "Data/{market_id}/Reference/few_shot_bank.jsonl", "valid_dates": "Data/{market_id}/Market/df_Valid_Dates.csv",
    "news_catalog": "Data/News/Guardian Raw/Raw Full New List.csv", "news_full_text": "Data/News/Full/Full News Complete.csv",
    "news_prepared": "Data/News/Prepared/news_evidence.csv", "news_market_articles": "Data/News/market_articles.csv",
    "news_retrievals": "Data/News/Retrievals",
}


def market_fingerprint(market):
    return hashlib.sha256(json.dumps(market, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def bind_market(settings, market_id):
    values = deepcopy(settings)
    registry = values["market_registry"]
    if market_id not in registry["markets"]:
        raise MarketError(f"Unknown market: {market_id}.")
    previous = values.get("ticker", market_id)
    values.update(market_id=market_id, ticker=market_id, market=deepcopy(registry["markets"][market_id]))
    values["market_fingerprint"] = market_fingerprint(values["market"])
    paths = {key: value.format(market_id=market_id) for key, value in PATH_TEMPLATES.items()}
    for key, value in values.get("paths", {}).items():
        paths[key] = value.replace(f"Data/{previous}/", f"Data/{market_id}/").replace(f"Results/{previous}/", f"Results/{market_id}/").replace(f"Runs/{previous}/", f"Runs/{market_id}/")
        if value == f"Results/{previous}":
            paths[key] = f"Results/{market_id}"
    values["paths"] = paths
    return values


def require_market(settings):
    market = settings.get("market")
    if not market or market["market_id"] != settings.get("ticker") or market["market_id"] != settings.get("market_id"):
        raise MarketError("Load settings or use bind_market; do not change ticker alone.")
    if settings.get("market_fingerprint") != market_fingerprint(market):
        raise MarketError("Market settings changed after binding; reload or bind the market again.")
    return market


def market_context(market):
    return {key: deepcopy(market[key]) for key in ("market_id", "name", "description", "instrument_type", "region", "currency", "return_type")}


def news_window(market, previous_session, target_session, lookback_days=7, *, session_dates=None):
    previous = date.fromisoformat(str(previous_session))
    target = date.fromisoformat(str(target_session))
    if type(lookback_days) is not int or not 1 <= lookback_days <= 30:
        raise MarketError("news_lookback_days must be an integer between 1 and 30.")
    session = market["session"]
    if not session["timezone"] or not session["open_local"] or session["basis"] == "requires_research_clock":
        raise MarketError(f"{market['market_id']}: no single opening clock; define a sourced research clock before pre-open prediction.")
    if previous >= target:
        raise MarketError("The target must follow the last observed market session.")
    zone = ZoneInfo(session["timezone"])
    opening = session["overrides"].get(target.isoformat(), session)["open_local"]
    if opening is None:
        raise MarketError(f"{market['market_id']}: {target} is marked closed but occurs in market data.")
    end = datetime.combine(target, time.fromisoformat(opening), zone)
    if session_dates is not None:
        dates = [date.fromisoformat(str(day)) for day in session_dates]
        if not dates or dates != sorted(set(dates)) or dates[-1] != previous or dates[-1] >= target:
            raise MarketError("News context requires ordered observed sessions ending at the market cutoff.")
        first_open = session["overrides"].get(dates[0].isoformat(), session)["open_local"]
        start = datetime.combine(dates[0], time.fromisoformat(first_open), zone)
    else:
        start = end - timedelta(days=lookback_days)
    close_clock = session["overrides"].get(target.isoformat(), {}).get("close_local", session.get("close_local"))
    closing = datetime.combine(target, time.fromisoformat(close_clock), zone) if close_clock else None

    for stamp in (start, end):
        if stamp.utcoffset() != stamp.replace(fold=1).utcoffset() or stamp.astimezone(timezone.utc).astimezone(zone) != stamp:
            raise MarketError("Ambiguous or nonexistent local research time.")
    return {"market_id": market["market_id"], "market_data_cutoff_date": previous.isoformat(), "forecast_target_date": target.isoformat(),
            "start_local": start.isoformat(), "end_local": end.isoformat(), "start_utc": start.astimezone(timezone.utc).isoformat(),
            "end_utc": end.astimezone(timezone.utc).isoformat(), "timezone": session["timezone"], "boundary": "start_inclusive_end_exclusive",
            "clock_basis": session["basis"], "calendar_basis": "consecutive_observed_market_dates", "opening_status": "scheduled_not_actual_auction",
            "news_lookback_trading_days": len(dates) if session_dates is not None else None, "news_lookback_days": lookback_days if session_dates is None else None, "lookback_basis": "observed_trading_sessions" if session_dates is not None else "local_calendar_days_before_target_open",
            "target_close_local": closing.isoformat() if closing else None, "target_close_utc": closing.astimezone(timezone.utc).isoformat() if closing else None,
            "availability_status": "unverified_latest_snapshot", "point_in_time_gate_passed": False}


def guardian_queries(market):
    news = market["news"]
    excluded = " ".join('AND NOT ' + json.dumps(term, ensure_ascii=False) for term in news["exclude_terms"])
    return {group: "(" + " OR ".join(json.dumps(term, ensure_ascii=False) for term in terms) + ")" + (" " + excluded if excluded else "")
            for group, terms in news["query_groups"].items() if terms}
