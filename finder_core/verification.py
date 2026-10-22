from __future__ import annotations

import copy
import math
import re
import unicodedata
from datetime import date
from typing import Any, Mapping


def build_machine_comparisons(reference: Mapping[str, Any]) -> dict[str, Any]:

    values = reference.get("latest_values", {})
    spec = reference.get("target_event_spec", {})
    comparisons = []
    for left_name, right_name in (("close", "sma20"), ("close", "sma50"), ("close", "sma200"), ("hv_10d_ann", "hv_126d_ann"), ("plus_di_14d", "minus_di_14d")):
        left, right = values.get(left_name), values.get(right_name)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and math.isfinite(left) and math.isfinite(right):
            comparisons.append({"left_field": left_name, "operator": "<" if left < right else ">" if left > right else "=", "right_field": right_name})
    current_return, threshold = values.get("logreturn"), spec.get("target_tail_threshold")
    if isinstance(current_return, (int, float)) and isinstance(threshold, (int, float)) and math.isfinite(current_return) and math.isfinite(threshold):
        comparisons.append({"left_field": "current_observed_logreturn", "operator": "<=" if current_return <= threshold else ">", "right_field": "target_event_past_only_cutoff", "interpretation": "Historical-state comparison only; not the unknown target return, target classification, or required future price fall."})
    return {"basis": "Python comparisons of supplied past-only values; no predictive or causal verification", "comparisons": comparisons}


def ascii_news_title(title: str) -> str:

    replacements = {"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"', "«": '"', "»": '"', "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-", "…": "...", "£": "GBP ", "€": "EUR ", "¥": "YEN/YUAN ", "₹": "INR ", "₩": "KRW ", "₽": "RUB ", "₿": "BTC ", "¢": " cents", "ŋ": "ng", "Ŋ": "Ng", "ß": "ss", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L"}
    text = "".join(replacements.get(char, char) for char in title)
    text = unicodedata.normalize("NFKD", text)
    return "".join(char if char.isascii() else f"[U+{ord(char):04X}]" for char in text if not unicodedata.combining(char))


def reconcile_news_selection(selections: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:

    by_index = {item["index"]: item for item in candidates}
    by_title: dict[str, list[int]] = {}
    for item in candidates:
        by_title.setdefault(item["title"], []).append(item["index"])
    accepted, used = [], set()
    for position, source in enumerate(selections):
        item = copy.deepcopy(source)
        index, title = item["index"], item["selected_title"]
        if by_index.get(index, {}).get("title") != title:
            matches = by_title.get(title, [])
            if len(matches) != 1:
                continue
            index = item["index"] = matches[0]
        if index in used:
            continue
        used.add(index)
        accepted.append(item)
    return accepted


def _explicit_quote_dates(quote: str) -> set[date]:

    found = set()
    for value in re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", quote):
        try:
            found.add(date.fromisoformat(value))
        except ValueError:
            pass
    months = {name.lower(): index for index, names in enumerate(((), ("January", "Jan"), ("February", "Feb"), ("March", "Mar"), ("April", "Apr"), ("May",), ("June", "Jun"), ("July", "Jul"), ("August", "Aug"), ("September", "Sep", "Sept"), ("October", "Oct"), ("November", "Nov"), ("December", "Dec"))) for name in names}
    month = "(?:" + "|".join(sorted(months, key=len, reverse=True)) + ")"
    for pattern, order in ((rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month})\.?[,]?\s+(\d{{4}})\b", (2, 1, 0)), (rf"\b({month})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(\d{{4}})\b", (2, 0, 1))):
        for match in re.finditer(pattern, quote, re.IGNORECASE):
            values = match.groups()
            try:
                found.add(date(int(values[order[0]]), months[values[order[1]].lower()], int(values[order[2]])))
            except ValueError:
                pass
    return found


def prepare_event_timeline(records: list[dict[str, Any]], target_date: str, *, articles: list[dict[str, Any]]) -> list[dict[str, Any]]:

    target = date.fromisoformat(target_date)
    sources = {item.get("summary_key", f"N{item['index']:03d}"): item["content"] for item in articles}
    timeline = []
    for index, source in enumerate(records, 1):
        event = copy.deepcopy(source)
        event["event_id"] = f"EV{index:03d}"
        raw_date = event.get("event_date", "UNKNOWN")
        event["days_after_target"] = None
        event["date_status"] = "UNKNOWN"
        refs, quote = event.get("article_refs", []), event.get("date_evidence", "")
        content = sources.get(refs[0], "") if len(refs) == 1 else ""
        checked_quote, quote_match = quote, "UNVERIFIED"
        if quote.strip() and quote in content:
            quote_match = "EXACT"
        elif len(quote) > 2 and (quote[0], quote[-1]) in {(chr(34), chr(34)), ("'", "'"), ("“", "”"), ("‘", "’")} and quote[1:-1].strip() and quote[1:-1] in content:
            checked_quote, quote_match = quote[1:-1], "OUTER_QUOTE_PAIR_REMOVED"
        quote_supported = quote_match != "UNVERIFIED"
        event["source_quote_verified"] = quote_supported
        event["source_quote_match"] = quote_match
        if raw_date != "UNKNOWN":
            try:
                parsed = date.fromisoformat(raw_date)
                if parsed.isoformat() != raw_date:
                    raise ValueError("Noncanonical date.")
                if not quote_supported or parsed not in _explicit_quote_dates(checked_quote):
                    event["event_date"] = "UNKNOWN"
                    event["date_status"] = "UNSUPPORTED_EXTRACTED_DATE"
                else:
                    event["days_after_target"] = (parsed - target).days
                    event["date_status"] = "SOURCE_QUOTE_DATE_AND_CALENDAR_OFFSET_CHECKED"
            except (TypeError, ValueError):
                event["event_date"] = "UNKNOWN"
                event["date_status"] = "INVALID_EXTRACTED_DATE"
        timeline.append(event)
    return timeline
