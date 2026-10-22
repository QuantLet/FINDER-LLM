from copy import deepcopy
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo
import hashlib
import re
from difflib import SequenceMatcher

MAX_FUTURE_EVENTS = 10
POST_EVENT_TRADING_DAYS = 3
EVENT_FIELDS = ("event_id", "event_key", "title", "certainty", "certainty_basis", "summary", "event_start_date", "event_end_date", "event_time", "timezone", "timing_basis", "status", "source_passage_refs")


def source_id(article):
    identity = article.get("article_id") or f"{article.get('published_at')}|{article.get('title')}"
    return "NS" + hashlib.sha256(identity.encode()).hexdigest()[:16]


def source_catalog(articles):
    return {source_id(a): {"source_id": source_id(a), "title": a.get("title", ""), "published_at": a.get("published_at", ""), "article_id": a.get("article_id", ""), "_source_text": a.get("content", ""), "source_passages": [a.get("content", "")]} for a in articles}


def future_event_schema():
    from .contracts import _object, _text, _enum
    refs = {"type": "array", "items": {"type": "string"}, "minItems": 0, "maxItems": 6}
    event = _object({"event_id": {"type": "string", "description": "Use an inherited FE-prefixed ID, or NEW."}, "event_key": {"type": "string", "pattern": "^[a-z0-9_]+\\|[a-z0-9_]+\\|[a-z0-9_]+$", "description": "Stable lowercase subject|specific_action|milestone identity. Separate distinct scheduled decisions; copy exactly when inherited. No generic monitoring clusters."}, "title": {**_text(180), "description": "Stable actor/action/milestone identity label. Omit occurrence dates, changing quantities, current status and provisional outcomes; put those in dates or summary. This title is immutable after creation."}, "certainty": {"type": "number", "minimum": 0, "maximum": 1, "description": "Return a number with exactly four decimal places: certainty of occurrence within the stated dates, not source quality."}, "certainty_basis": {"type": "string", "description": "Explain certainty that the event occurs within its dates; preserve source attribution. Do not present paraphrases as quotations. Empty for unchanged inherited evidence."}, "summary": {"type": "string", "description": "At most 250 English words about this single milestone; keep its subject and action. Related milestones need separate records."}, "event_start_date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "description": "Reported occurrence start; milestones never move to today. For ONGOING_ACTIVITY without a reported end, use the target-session monitoring date and keep the historical onset in summary."}, "event_end_date": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "description": "Reported finite end; a milestone has equal occurrence endpoints. For ONGOING_ACTIVITY without a reported end, use the target-session monitoring date, which is not an actual ending."}, "event_time": {"type": "string", "description": "HH:MM local clock or UNKNOWN; never invent an opening or closing time."}, "timezone": {"type": "string", "description": "IANA timezone or UNKNOWN."}, "timing_basis": _text(360), "status": _enum(("UPCOMING", "ACTIVE", "RESOLVED", "CANCELLED")), "source_passage_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 6, "description": "Copy supplied passage_ref IDs supporting the facts AND dates. Empty only for unchanged events."}})
    return {"type": "array", "items": event, "maxItems": MAX_FUTURE_EVENTS}


def certainty_update_schema():
    from .contracts import _object, _text, _enum
    update = _object({"event_id": _text(), "certainty": {"type": "number", "minimum": 0, "maximum": 1, "description": "Return a number with exactly four decimal places: certainty of occurrence within the stated dates, not source quality."}, "certainty_basis": {"type": "string", "description": "Explain certainty that the event occurs within its dates; preserve source attribution. Do not present paraphrases as quotations. Empty for unchanged inherited evidence."}, "source_passage_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 6}})
    return {"type": "array", "items": update, "maxItems": MAX_FUTURE_EVENTS}


def _sources(prior, catalog):
    result = {s["source_id"]: deepcopy(s) for event in prior for s in event.get("sources", [])}
    result.update(deepcopy(catalog))
    return result


def _eligible_sources(refs, catalog, timing):
    from .contracts import SchemaError
    start, end = datetime.fromisoformat(timing["start_utc"]), datetime.fromisoformat(timing["end_utc"])
    if any(ref not in catalog for ref in refs):
        raise SchemaError("Future event cites an unavailable source_id.")
    return [deepcopy(catalog[ref]) for ref in dict.fromkeys(refs) if start <= datetime.fromisoformat(catalog[ref]["published_at"]) < end]


@lru_cache(maxsize=256)
def scheduled_horizon(calendar_id, target, count):
    import exchange_calendars
    calendar = exchange_calendars.get_calendar(calendar_id, start=target, end=str(date.fromisoformat(target) + timedelta(days=count * 4 + 30)))
    sessions = calendar.sessions_in_range(target, str(date.fromisoformat(target) + timedelta(days=count * 4)))
    if len(sessions) < count:
        raise ValueError("Insufficient scheduled exchange sessions for the future horizon.")
    return sessions[count - 1].date().isoformat()


def with_future_horizon(timing, calendar_id, count):
    return {**timing, "future_horizon_start": timing["forecast_target_date"], "future_horizon_end": scheduled_horizon(calendar_id, timing["forecast_target_date"], count), "future_horizon_trading_days": count, "future_calendar_id": calendar_id}


def public_events(events):
    private = {"certainty_basis", "news_source_refs", "sources", "timing_basis", "selection_source_refs", "source_quotes", "source_quote_indices", "source_passage_refs", "evidence_history", "_source_text", "selection_rank"}
    return [{k: deepcopy(v) for k, v in event.items() if k not in private} for event in events]


def session_relation(event, timing):
    target = date.fromisoformat(timing["forecast_target_date"])
    if date.fromisoformat(event["event_end_date"]) < target:
        return "RECENTLY_ENDED"
    if event["event_start_date"] != event["event_end_date"]:
        return "ONGOING_INTERVAL" if date.fromisoformat(event["event_start_date"]) <= target else "FUTURE_INTERVAL"
    event_day = date.fromisoformat(event["event_start_date"])
    if event["event_time"] == "UNKNOWN" or event["timezone"] == "UNKNOWN":
        return "DATE_ONLY_AFTER_TARGET" if event_day > target else "SAME_TARGET_DATE_TIME_UNKNOWN"
    stamp = datetime.combine(event_day, time.fromisoformat(event["event_time"]), ZoneInfo(event["timezone"]))
    opening, closing = datetime.fromisoformat(timing["end_utc"]), datetime.fromisoformat(timing["target_close_utc"])
    return "BEFORE_TARGET_OPEN" if stamp < opening else "DURING_TARGET_SESSION" if stamp < closing else "AT_OR_AFTER_TARGET_CLOSE"


def validate_interval(event, timing, allow_expired=False):
    from .contracts import SchemaError
    try:
        start, end = (date.fromisoformat(event[k]) for k in ("event_start_date", "event_end_date"))
        if not start <= end <= date.fromisoformat(timing["future_horizon_end"]):
            raise ValueError("Outside horizon or reversed dates")
        if not allow_expired and retention_end(timing["future_calendar_id"], event["event_end_date"]) < timing["forecast_target_date"]:
            raise ValueError("Outside three-session grace period")
        if event["certainty"] != round(event["certainty"], 4):
            raise ValueError("Certainty precision")
        session_relation(event, timing)
    except (ValueError, KeyError) as exc:
        raise SchemaError("Use supported ordered event dates, within the future horizon or three-session post-event grace period; certainty needs four decimals.") from exc


def retain_events(prior, timing):
    result = []
    for event in prior:
        e = deepcopy(event)
        if e.get("event_kind") == "ONGOING_ACTIVITY" and e["status"] == "ACTIVE" and not e.get("closed_on"):
            e["event_start_date"] = e["event_end_date"] = timing["forecast_target_date"]
        ended = min(e["event_end_date"], e.get("closed_on", e["event_end_date"]))
        e["calendar_as_of"] = timing["forecast_target_date"]
        e["retention_until"] = retention_end(timing["future_calendar_id"], ended)
        if timing["forecast_target_date"] > e["retention_until"]:
            continue
        e["target_session_relation"] = session_relation(e, timing)
        e["lifecycle"] = "RECENTLY_ENDED" if ended < timing["forecast_target_date"] else e["status"]
        result.append(e)
    return result


def limit_event_calendar(events, timing):
    retained = retain_events(events, timing)
    live = [e for e in retained if e["lifecycle"] != "RECENTLY_ENDED"][:MAX_FUTURE_EVENTS]
    return [*live, *[e for e in retained if e["lifecycle"] == "RECENTLY_ENDED"]]


def merge_events(prior, proposed, catalog, timing, stage):
    from .contracts import SchemaError
    current = {e["event_id"]: e for e in retain_events(prior, timing)}
    result, used, keys = [], set(), set()
    prior_keys = {normalize_key(e["event_key"]): e["event_id"] for e in current.values()}
    for index, raw in enumerate(proposed, 1):
        e = {key: deepcopy(raw[key]) for key in EVENT_FIELDS}
        if raw.get("event_kind"):e["event_kind"] = raw["event_kind"]
        identity = e["event_id"]
        old = current.get(identity)
        key = normalize_key(e["event_key"])
        if not key or len(key.split("|")) != 3:
            raise SchemaError("event_key must identify subject|specific_action|milestone.")
        if identity == "NEW":
            target = timing["forecast_target_date"]
            if e["event_end_date"] < target or e["status"] not in ("UPCOMING", "ACTIVE") or (e["event_start_date"] < target and e["status"] != "ACTIVE"):
                raise SchemaError("New events must be upcoming or explicitly ongoing at the target date. Three-session grace applies only to inherited events.")
            if e.get("event_kind") == "MILESTONE" and e["event_start_date"] < target:raise SchemaError("A new milestone must occur within the forward horizon; lasting effects do not extend its occurrence.")
            if e.get("event_kind") == "ONGOING_ACTIVITY" and (e["event_start_date"] != target or e["event_end_date"] != target or e["event_time"] != "UNKNOWN"):raise SchemaError("Open-ended ongoing activity uses a target-session monitoring interval and no obsolete occurrence clock.")
            if key in prior_keys:
                raise SchemaError("This event_key already exists; reuse its inherited event_id.")
            identity = f"FE{timing['forecast_target_date'].replace('-', '')}{stage}{index:02d}"
        elif old is None:
            raise SchemaError("Use an inherited event_id or NEW.")
        elif e["event_key"] != old["event_key"] or e["title"] != old["title"]:
            raise SchemaError("Inherited event identity is immutable. A different subject or action needs NEW; do not recycle IDs.")
        if identity in used or key in keys:
            raise SchemaError("Duplicate event identity; retain each distinct milestone once.")
        if any(SequenceMatcher(None, normalize_key(e["title"]), normalize_key(other["title"])).ratio() >= .93 and e["event_start_date"] == other["event_start_date"] and e["event_end_date"] == other["event_end_date"] for other in result):
            raise SchemaError("Near-identical same-date events must be consolidated.")
        used.add(identity);keys.add(key)
        if len(e["summary"].split()) > 250:
            raise SchemaError("Event summaries must have at most 250 English words.")
        validate_interval(e, timing, allow_expired=bool(old and e["status"] in ("RESOLVED", "CANCELLED")))
        factual = ("title", "summary", "certainty", "event_start_date", "event_end_date", "event_time", "timezone", "status")
        kind_changed = bool(old and e.get("event_kind", old.get("event_kind")) != old.get("event_kind"))
        changed = not old or any(e[k] != old[k] for k in factual) or kind_changed
        monitoring_roll = bool(old and not kind_changed and old.get("event_kind") == "ONGOING_ACTIVITY" and old["status"] == e["status"] == "ACTIVE" and e["event_start_date"] == e["event_end_date"] == timing["forecast_target_date"] and all(e[k] == old[k] for k in ("title", "summary", "certainty", "event_time", "timezone")) and not e["source_passage_refs"])
        evidence = verify_event_quotes(e, catalog, timing)
        if changed and not e["news_source_refs"] and not monitoring_roll:
            raise SchemaError("New or changed facts need a current source and exact supporting quote. Preserve unchanged inherited facts verbatim.")
        if e["source_quote_indices"] and not e["news_source_refs"]:
            raise SchemaError("Quotes require matching source references.")
        for field in ("certainty_basis", "timing_basis"):
            if not e[field] and old:e[field] = old[field]
        e["event_id"] = identity
        if old and old.get("event_kind") and not raw.get("event_kind"):e["event_kind"] = old["event_kind"]
        if changed and not monitoring_roll:
            e["sources"] = evidence
            e["selection_source_refs"] = list(e["news_source_refs"])
        else:
            e["sources"] = deepcopy(old.get("sources", []))
            e["news_source_refs"] = list(old.get("news_source_refs", []))
            e["source_quotes"] = list(old.get("source_quotes", []))
            e["source_quote_indices"] = list(old.get("source_quote_indices", []))
            e["source_passage_refs"] = list(old.get("source_passage_refs", []))
            e["selection_source_refs"] = list(old.get("selection_source_refs", []))
        e["first_seen_on"] = old.get("first_seen_on", timing["forecast_target_date"]) if old else timing["forecast_target_date"]
        e["last_updated_on"] = timing["forecast_target_date"] if changed else old["last_updated_on"]
        e["evidence_history"] = deepcopy(old.get("evidence_history", [])) if old else []
        if changed and old:e["evidence_history"].append({"as_of": old["last_updated_on"], "title": old["title"], "summary": old["summary"], "event_start_date": old["event_start_date"], "event_end_date": old["event_end_date"], "sources": deepcopy(old.get("sources", [])), "quotes": list(old.get("source_quotes", []))})
        if old and old.get("closed_on") and e["status"] in ("RESOLVED", "CANCELLED"):e["closed_on"] = old["closed_on"]
        if e["status"] in ("RESOLVED", "CANCELLED") and (not old or old["status"] != e["status"]):e["closed_on"] = timing["forecast_target_date"]
        result.append(e)
    protected = {identity for identity, event in current.items() if event.get("lifecycle") == "RECENTLY_ENDED" and event.get("first_seen_on", "") < timing["forecast_target_date"]}
    if protected - used:
        raise SchemaError("Keep inherited recently ended events through retention_until; do not silently drop the three-session history.")
    return limit_event_calendar(result, timing)


def apply_certainty_updates(prior, updates, catalog, timing):
    from .contracts import SchemaError
    events = {e["event_id"]: deepcopy(e) for e in prior}
    seen = set()
    for update in updates:
        identity = update["event_id"]
        if identity not in events or identity in seen:
            raise SchemaError("News Assistant may update each inherited event once.")
        seen.add(identity)
        e = events[identity]
        if update["certainty"] < e["certainty"] or update["certainty"] != round(update["certainty"], 4):
            raise SchemaError("News Assistant cannot reduce event certainty.")
        if update["certainty"] > e["certainty"]:
            evidence = verify_event_quotes(update, catalog, timing)
            if not update["certainty_basis"].strip() or not update["news_source_refs"]:
                raise SchemaError("Increasing certainty requires a source and occurrence evidence.")
            e["evidence_history"].append({"as_of": e["last_updated_on"], "sources": deepcopy(e["sources"]), "quotes": list(e["source_quotes"])})
            e.update(certainty=update["certainty"], certainty_basis=update["certainty_basis"], sources=evidence, news_source_refs=update["news_source_refs"], source_quotes=update["source_quotes"], source_quote_indices=update["source_quote_indices"], source_passage_refs=update["source_passage_refs"], selection_source_refs=update["news_source_refs"], last_updated_on=timing["forecast_target_date"])
    return list(events.values())


def bind_event_schema(schema, prior, source_ids=(), timing=None):

    result = deepcopy(schema)
    properties = result["properties"]
    for field in ("future_events", "certainty_updates"):
        if field not in properties:
            continue
        array = properties[field]
        array["maxItems"] = len(prior) + MAX_FUTURE_EVENTS if field == "future_events" else len(prior)
        event = array["items"]["properties"]
        ids = [e["event_id"] for e in prior] + (["NEW"] if field == "future_events" else [])
        if ids:
            event["event_id"] = {"type": "string", "enum": list(dict.fromkeys(ids))}
        else:
            array["maxItems"] = 0
        if timing and field == "future_events":
            first, last = date.fromisoformat(timing["start_utc"][:10]), date.fromisoformat(timing["future_horizon_end"])
            if prior:first = min(first, *(date.fromisoformat(e["event_start_date"]) for e in prior))
            allowed = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
            for key in ("event_start_date", "event_end_date"):
                event[key] = {"type": "string", "enum": allowed}
        available = [f"{ref}:P{i}" for ref in source_ids for i in range(1, len(source_ids[ref].get("source_passages", []))+1)] if isinstance(source_ids, dict) else [f"{ref}:P1" for ref in source_ids]
        result.setdefault("$defs", {})["event_source_passage_ref"] = {"type": "string", "enum": available} if available else {"type": "string"}
        event["source_passage_refs"]["items"] = {"$ref": "#/$defs/event_source_passage_ref"}
        if not available:event["source_passage_refs"]["maxItems"] = 0
        if timing and field == "future_events":
            from .news_sources import earliest_retained_date
            minimum_end = earliest_retained_date(timing["future_calendar_id"], timing["forecast_target_date"]).isoformat()
            event["event_end_date"]["enum"] = [day for day in allowed if day >= minimum_end]
    return result


def compact_event_actions_schema(prior, timing=None):

    from .contracts import _object
    event = deepcopy(future_event_schema()["items"])
    event["properties"]["event_kind"] = {"type": "string", "enum": ["MILESTONE", "BOUNDED_PERIOD", "ONGOING_ACTIVITY"], "description": "BOUNDED_PERIOD requires a reported finite ending date. The forward horizon is not an event ending date. Open-ended continuing activity is ONGOING_ACTIVITY, monitored for the target session."}
    event["required"].append("event_kind")
    rank = {"type": "integer", "minimum": 1, "maximum": 30}
    new = deepcopy(event)
    new["properties"]["event_id"] = {"type": "string", "enum": ["NEW"]}
    new["properties"]["source_passage_refs"]["minItems"] = 1
    new["properties"]["selection_rank"] = rank
    new["required"].append("selection_rank")
    if timing:
        target = date.fromisoformat(timing["forecast_target_date"])
        last = date.fromisoformat(timing["future_horizon_end"])
        dates = [(target + timedelta(days=i)).isoformat() for i in range((last-target).days+1)]
        milestone, bounded, ongoing = deepcopy(new), deepcopy(new), deepcopy(new)
        for item, kind in ((milestone,"MILESTONE"),(bounded,"BOUNDED_PERIOD"),(ongoing,"ONGOING_ACTIVITY")):
            item["properties"]["event_kind"] = {"type":"string","enum":[kind]}
            item["properties"]["status"] = {"type":"string","enum":["UPCOMING","ACTIVE"]}
            item["properties"]["event_end_date"] = {"type":"string","enum":dates}
        milestone["properties"]["event_start_date"] = {"type":"string","enum":dates}
        ongoing["properties"]["status"] = {"type":"string","enum":["ACTIVE"]}
        for field in ("event_start_date","event_end_date"):ongoing["properties"][field] = {"type":"string","enum":[target.isoformat()]}
        ongoing["properties"]["event_time"] = {"type":"string","enum":["UNKNOWN"]}
        new = {"anyOf":[milestone,bounded,ongoing]}
    update = deepcopy(event)
    for key in ("event_id", "event_key"):
        update["properties"].pop(key);update["required"].remove(key)
    update["properties"]["source_passage_refs"]["minItems"] = 0
    if prior: update["properties"]["title"]["enum"] = [event["title"] for event in prior]
    inherited = _object({"event_id": {"type": "string", **({"enum": [e["event_id"] for e in prior]} if prior else {})}, "action": {"type": "string", "enum": ["KEEP", "UPDATE", "REMOVE"]}, "selection_rank": rank, "reason": {"type": "string", "maxLength": 180}, "event": {"anyOf": [update, {"type": "null"}]}})
    return _object({"new_events": {"type": "array", "items": new, "maxItems": MAX_FUTURE_EVENTS}, "inherited": {"type": "array", "items": inherited, "minItems": len(prior), "maxItems": len(prior)}})


def expand_compact_event_actions(prior, actions):
    from .contracts import SchemaError
    inherited = {}
    for item in actions["inherited"]:
        identity = item["event_id"]
        if identity in inherited:raise SchemaError("Account for each inherited event once.")
        if (item["action"] == "UPDATE") != (item["event"] is not None):raise SchemaError("Only UPDATE supplies an event; KEEP and REMOVE use null.")
        inherited[identity] = {k:deepcopy(v) for k,v in item.items() if k != "event_id"}
    return expand_event_actions(prior, {"new_events": actions["new_events"], "inherited": inherited})


@lru_cache(maxsize=2048)
def retention_end(calendar_id, ended):
    import exchange_calendars
    day = date.fromisoformat(ended)
    upper = day + timedelta(days=30)
    calendar = exchange_calendars.get_calendar(calendar_id, start=str(day-timedelta(days=7)), end=str(upper+timedelta(days=7)))
    sessions = calendar.sessions_in_range(str(day+timedelta(days=1)), str(upper))
    return sessions[POST_EVENT_TRADING_DAYS-1].date().isoformat()


def normalize_key(value):
    return re.sub(r"[^a-z0-9|]+", "_", value.lower()).strip("_")


def verify_event_quotes(event, catalog, timing):
    from .contracts import SchemaError
    refs, indices, quotes = [], [], []
    if len(set(event["source_passage_refs"])) != len(event["source_passage_refs"]):
        raise SchemaError("Cite each source passage only once.")
    for passage_ref in event["source_passage_refs"]:
        try:ref, number = passage_ref.rsplit(":P", 1);index = int(number)
        except (ValueError, AttributeError):raise SchemaError("Copy an exact supplied passage_ref.")
        passages = catalog.get(ref, {}).get("source_passages", [])
        if not 1 <= index <= len(passages):raise SchemaError("Copy an available passage_ref for its source.")
        refs.append(ref);indices.append(index);quotes.append(passages[index-1])
    event.update(news_source_refs=refs, source_quote_indices=indices, source_quotes=quotes)
    evidence = []
    for ref, quote in zip(refs, quotes):
        if ref not in catalog:
            raise SchemaError("New evidence must refer to an available current article.")
        source = catalog[ref]
        if not datetime.fromisoformat(timing["start_utc"]) <= datetime.fromisoformat(source["published_at"]) < datetime.fromisoformat(timing["end_utc"]):
            raise SchemaError("New evidence must be inside the current news window.")
        normal = lambda value: re.sub(r"\s+", " ", value).strip()
        if len(normal(quote)) < 20 or normal(quote) not in normal(source.get("_source_text", "")):
            raise SchemaError("Supporting quotes must copy an exact source passage of at least 20 characters; do not paraphrase.")
        evidence.append({k:deepcopy(v) for k,v in source.items() if k not in {"_source_text", "source_passages"}})
    return evidence


def expand_event_actions(prior, actions):

    from .contracts import SchemaError
    current = {e["event_id"]: e for e in prior}
    if set(actions["inherited"]) != set(current):raise SchemaError("Account for every inherited event exactly once.")
    protected = {e["event_id"] for e in prior if e.get("lifecycle") == "RECENTLY_ENDED" and e.get("first_seen_on", "") < e.get("calendar_as_of", "")}
    ranked, reserved = [], []
    for index, (identity, action) in enumerate(actions["inherited"].items()):
        old = current[identity]
        if action["action"] == "REMOVE":
            if identity in protected:raise SchemaError("Protected recently ended events cannot be removed before retention_until.")
            continue
        if action["action"] == "KEEP":
            e = {k:deepcopy(old[k]) for k in EVENT_FIELDS};e["source_passage_refs"] = []
            if old.get("event_kind"):e["event_kind"] = old["event_kind"]
        else:e = {**deepcopy(action["event"]), **{k:old[k] for k in ("event_id", "event_key")}}
        ended = min(e["event_end_date"], old.get("closed_on", e["event_end_date"])) if e["status"] in ("RESOLVED", "CANCELLED") else e["event_end_date"]
        if ended < old.get("calendar_as_of", "") or e["status"] in ("RESOLVED", "CANCELLED"):reserved.append(e)
        else:ranked.append((action["selection_rank"], 0, index, e))
    for index, raw in enumerate(actions["new_events"]):
        e = deepcopy(raw);rank = e.pop("selection_rank")
        matches_prior = any(normalize_key(e["event_key"]) == normalize_key(old["event_key"]) or (normalize_key(e["title"]) == normalize_key(old["title"]) and e["event_start_date"] == old["event_start_date"] and e["event_end_date"] == old["event_end_date"]) for old in prior)
        if not matches_prior:ranked.append((rank, 1, index, e))
    selected = []
    for _, _, _, e in sorted(ranked, key=lambda row: row[:3]):
        duplicate = any(normalize_key(e["event_key"]) == normalize_key(other["event_key"]) or (normalize_key(e["title"]) == normalize_key(other["title"]) and e["event_start_date"] == other["event_start_date"] and e["event_end_date"] == other["event_end_date"]) for other in [*reserved, *selected])
        if not duplicate:selected.append(e)
    return [*selected[:MAX_FUTURE_EVENTS], *reserved]
