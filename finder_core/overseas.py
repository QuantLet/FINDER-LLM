from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


PRICE_FIELDS = ("Open", "High", "Low", "Close", "Volume")


def overseas_source_paths(root: Path | str) -> list[Path]:
    root = Path(root).resolve()
    manifest_path = root / "Data/Global/overseas_manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [manifest_path]
    for entry in manifest.get("markets", []):
        for name in ("data_path", "schedule_path"):
            if entry.get(name):
                path = (root / entry[name]).resolve()
                if not path.is_relative_to((root / "Data/Global").resolve()):
                    raise ValueError("Overseas data path is outside Data/Global.")
                paths.append(path)
    return paths


def _read_verified(root: Path, relative: str, expected: str) -> pd.DataFrame:
    path = (root / relative).resolve()
    if not path.is_relative_to((root / "Data/Global").resolve()):
        raise ValueError("Overseas data path is outside Data/Global.")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(f"Overseas source changed after collection: {relative}")
    from io import BytesIO
    return pd.read_csv(BytesIO(payload), dtype={"Date": str})


def _number(value):
    return None if pd.isna(value) else float(value)


def prepare_overseas_context(root: Path | str, cutoff_utc: str, target_market: str | None = None) -> dict:

    root = Path(root).resolve()
    cutoff = pd.Timestamp(cutoff_utc)
    if cutoff.tzinfo is None:
        raise ValueError("Overseas cutoff must include an explicit timezone.")
    cutoff = cutoff.tz_convert("UTC")
    manifest_path = root / "Data/Global/overseas_manifest.json"
    result = {"as_of_utc": cutoff.isoformat(), "target_market": target_market, "return_units": "decimal_simple_return", "return_definition": {"close_to_close_return": "current completed-session Close / immediately preceding exchange-session Close - 1", "open_to_previous_close_return": "current-session Open / immediately preceding completed exchange-session Close - 1"}, "markets": []}
    if not manifest_path.exists():
        return {**result, "status": "unavailable", "reason": "overseas_manifest_missing"}
    payload = manifest_path.read_bytes()
    manifest = json.loads(payload)
    result.update(status="available", dataset_fingerprint=hashlib.sha256(payload).hexdigest())
    for entry in manifest.get("markets", []):
        if target_market and target_market in entry.get("target_aliases", []):
            continue
        item = {"market_id": entry["market_id"], "market_name": entry["market_name"], "instrument_type": entry.get("instrument_type", "equity_index"), "currency": entry["currency"], "timezone": entry["timezone"], "source": entry.get("selected_source"), "as_of_utc": cutoff.isoformat()}
        if entry.get("status") != "included":
            result["markets"].append({**item, "status": "excluded", "reason": entry.get("reason", "data_quality_or_schedule_unverified")})
            continue
        schedule = _read_verified(root, entry["schedule_path"], entry["schedule_sha256"])
        prices = _read_verified(root, entry["data_path"], entry["data_sha256"])
        schedule["open_utc"] = pd.to_datetime(schedule.open_utc, utc=True)
        schedule["close_utc"] = pd.to_datetime(schedule.close_utc, utc=True)
        local_date = cutoff.tz_convert(ZoneInfo(entry["timezone"])).strftime("%Y-%m-%d")
        item["local_date"] = local_date
        bounds = entry["schedule_date_bounds"]
        if not bounds[0] <= local_date <= bounds[1]:
            result["markets"].append({**item, "status": "excluded", "reason": "cutoff_outside_verified_schedule"})
            continue
        prices = prices.set_index("Date")
        today = schedule.loc[schedule.Date.eq(local_date)]
        completed = schedule.loc[schedule.close_utc.lt(cutoff)]
        latest = completed.iloc[-1] if not completed.empty else None
        last_payload = None
        if latest is not None:
            last_date = latest.Date
            if last_date in prices.index:
                p = prices.loc[last_date]
                last_payload = {"session_date": last_date, "session_open_utc": latest.open_utc.isoformat(), "session_close_utc": latest.close_utc.isoformat(), "ohlcv": {field.lower(): _number(p.get(field)) for field in PRICE_FIELDS}, "close_to_close_return": _number(p.get("close_to_close_return")), "source": p.get("price_source", entry.get("selected_source")), "volume_source": p.get("volume_source") if pd.notna(p.get("volume_source")) else None, "availability_basis": "session_completed_strictly_before_cutoff"}
            else:
                item["latest_completed_session_missing"] = last_date
        if today.empty:
            item["status"] = "closed_for_calendar"
        else:
            session = today.iloc[0]
            item.update(session_date=local_date, session_open_utc=session.open_utc.isoformat(), session_close_utc=session.close_utc.isoformat())
            if cutoff <= session.open_utc:
                item["status"] = "scheduled_not_open"
            elif cutoff > session.close_utc:
                item["status"] = "completed"
            else:
                item["status"] = "open"
                break_start, break_end = session.get("break_start_utc"), session.get("break_end_utc")
                if pd.notna(break_start) and pd.notna(break_end) and pd.Timestamp(break_start) <= cutoff < pd.Timestamp(break_end):
                    item["status"] = "midday_break"
                if local_date in prices.index and pd.notna(prices.loc[local_date].get("Open")):
                    current = prices.loc[local_date]
                    previous_close = None
                    if latest is not None and latest.Date in prices.index:
                        previous_close = _number(prices.loc[latest.Date].get("Close"))
                    opened = _number(current.Open)
                    item["current_session_open_only"] = {"open": opened, "open_to_previous_close_return": opened / previous_close - 1 if previous_close is not None and previous_close > 0 else None, "price_source": current.get("price_source", entry.get("selected_source")), "availability_basis": "reported_daily_open_after_session_open; no current-session high_low_close_volume exposed"}
                else:
                    item["current_open_unavailable"] = True
        if last_payload is not None:
            item["latest_completed_session"] = last_payload
        result["markets"].append(item)
    return result
