from __future__ import annotations

from datetime import datetime, timezone
import argparse
import fcntl
import os
import re
import shutil
import time
import hashlib
import json
from pathlib import Path

import pandas as pd
import requests

from .markets import guardian_queries, news_window, MarketError
from .storage import write_json_once
from .events import write_json_atomic


def guardian_retrieval_plan(registry):
    plan = []
    sections = sorted({section for market in registry["markets"].values() if market["news"]["mode"] in {"sections", "hybrid"} for section in market["news"]["provider_options"]["guardian"]["sections"]})
    if sections:
        plan.append({"market_id": "*", "query_group": "shared_section", "match_method": "guardian_section", "params": {"section": "|".join(sections)}})
    for market in registry["markets"].values():
        if market["news"]["mode"] in {"keywords", "hybrid"}:
            queries = guardian_queries(market)
            plan.append({"market_id": market["market_id"], "query_group": "|".join(queries), "match_method": "guardian_query", "params": {"q": " OR ".join(queries.values())}})
    for query in plan:
        query["query_id"] = "Q" + hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()[:12]
    return plan


def collect_guardian(session, settings, api_key, normalize_article, *, project_root=".", page_size=200, retrieval_id="market_union_2006_2026", backfill_start="2006-01-01", include_catalog=None, archive_root=None, wait_for_quota=True, max_requests=None):
    root = Path(project_root).resolve()
    folder = root / settings["paths"]["news_retrievals"] / retrieval_id
    folder.mkdir(parents=True, exist_ok=True)
    lock = (folder.parent / "collection.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("A news collector is already active.") from None
    archive = Path(archive_root) if archive_root else root.parent / "Archive" / ("News_" + retrieval_id)
    state_path = folder / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"requests_total": 0, "requests_by_utc_date": {}, "completed_phases": []}
    state.update(pid=os.getpid(), status="initializing", updated_at_utc=datetime.now(timezone.utc).isoformat())
    calls_this_run = 0
    last_call = 0.0
    plan = guardian_retrieval_plan(settings["market_registry"])
    phases = [{"phase": 1, "start": settings["news_start_date"], "end": settings["news_end_date"]}]
    if backfill_start:
        phases.append({"phase": 2, "start": backfill_start, "end": (pd.Timestamp(settings["news_start_date"]) - pd.Timedelta(days=1)).date().isoformat()})
    if any(item["start"] > item["end"] for item in phases):
        lock.close()
        raise ValueError("News phase dates are not chronological.")
    if include_catalog is None and (folder / "request.json").exists():
        include_catalog = json.loads((folder / "request.json").read_text()).get("include_catalog")
    definition = {"schema_version": 2, "retrieval_id": retrieval_id, "phases": phases, "plan": plan, "registry": settings["market_registry"], "order": "newest_month_first_round_robin_queries", "page_size": page_size, "archive": str(archive), "include_catalog": str(include_catalog) if include_catalog else None}
    request_file = folder / "request.json"
    if request_file.exists() and json.loads(request_file.read_text()) != definition:
        lock.close()
        raise RuntimeError("Saved collection settings differ; resume with the original settings or choose another retrieval ID.")
    write_json_once(request_file, definition)

    def save_state(status=None, **values):
        state.update(values, updated_at_utc=datetime.now(timezone.utc).isoformat())
        if status:
            state["status"] = status
        write_json_atomic(state_path, state)

    def event(kind, **values):
        row = {"time_utc": datetime.now(timezone.utc).isoformat(), "event": kind, **values}
        with (folder / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        print(json.dumps(row, ensure_ascii=False), flush=True)

    def file_hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def pause_until(stamp):
        while time.time() < stamp:
            if (folder / "STOP").exists():
                raise InterruptedError("Stop file detected.")
            time.sleep(min(30, stamp - time.time()))

    catalog, bodies, matches = {}, {}, {}
    source_rows = []
    visited_pages = 0

    def add_article(row, source_id, query=None, request_ref=None):
        article_id = str(row["id"])
        if article_id not in catalog:
            catalog[article_id] = dict(row)
            catalog[article_id].update(source_refs=set(), query_ids=set(), request_refs=set())
        current = catalog[article_id]
        for key, value in row.items():
            if key not in {"source_refs", "query_ids", "request_refs"} and not current.get(key) and value:
                current[key] = value
        current["source_refs"].add(source_id)
        if query:
            current["query_ids"].add(query["query_id"])
            current["request_refs"].add(request_ref)
            match = matches.setdefault((article_id, query["query_id"]), {"article_id": article_id, "query_id": query["query_id"], "market_id": query["market_id"], "query_group": query["query_group"], "match_method": query["match_method"], "search_query": query["params"].get("q", ""), "section": query["params"].get("section", ""), "retrieval_id": retrieval_id, "request_refs": set()})
            match["request_refs"].add(request_ref)

    def publish():
        frames = {}
        rows = [{**row, **{key: json.dumps(sorted(row[key]), ensure_ascii=False) for key in ("source_refs", "query_ids", "request_refs")}} for row in catalog.values()]
        frames["news_catalog"] = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["id", "webPublicationDate", "sectionId", "sectionName", "webTitle", "headline", "trailText", "bodyText_200", "source_refs", "query_ids", "request_refs"])
        frames["news_full_text"] = pd.DataFrame([{**row, "source_refs": json.dumps(sorted(catalog[key]["source_refs"])), "query_ids": json.dumps(sorted(catalog[key]["query_ids"]))} for key, row in bodies.items() if key in catalog], columns=["id", "bodyText", "lastModified", "downloaded_at_utc", "content_source_id", "source_refs", "query_ids"])
        frames["news_market_articles"] = pd.DataFrame([{**row, "request_refs": json.dumps(sorted(row["request_refs"]))} for row in matches.values()], columns=["market_id", "article_id", "query_id", "query_group", "match_method", "search_query", "section", "retrieval_id", "request_refs"])
        prepared = []
        for row in catalog.values():
            body = bodies.get(row["id"], {})
            content = body.get("bodyText") or row.get("bodyText_200") or row.get("trailText") or ""
            prepared.append({"article_id": row["id"], "publication_timestamp": row.get("webPublicationDate", ""), "publication_date": str(row.get("webPublicationDate", ""))[:10], "title": row.get("headline") or row.get("webTitle", ""), "content": content, "content_type": "full_text" if body.get("bodyText") else "excerpt_200", "content_truncated": False if body.get("bodyText") else True, "readable": bool(content), "source_file": settings["paths"]["news_full_text"] if body.get("bodyText") else settings["paths"]["news_catalog"], "content_sha256": hashlib.sha256(content.encode()).hexdigest(), "availability_status": "unverified_latest_snapshot", "content_source_id": body.get("content_source_id", ""), "source_refs": json.dumps(sorted(row["source_refs"])), "query_ids": json.dumps(sorted(row["query_ids"])), "request_refs": json.dumps(sorted(row["request_refs"]))})
        frame = pd.DataFrame(prepared) if prepared else pd.DataFrame(columns=["article_id", "publication_timestamp", "publication_date", "title", "content"])
        frame = frame.sort_values(["publication_timestamp", "article_id"]).reset_index(drop=True)
        frame["article_index"] = frame.groupby("publication_date", sort=False).cumcount()
        frames["news_prepared"] = frame
        queries = pd.DataFrame([{**{key: value for key, value in query.items() if key != "params"}, "search_query": query["params"].get("q", ""), "section": query["params"].get("section", ""), "definition_file": str(request_file.relative_to(root)), "parameters_json": json.dumps(query["params"])} for query in plan])
        outputs = {root / settings["paths"][key]: frame for key, frame in frames.items()}
        outputs[root / "Data/News/search_queries.csv"] = queries
        outputs[root / "Data/News/source_files.csv"] = pd.DataFrame(source_rows)
        for destination, frame in outputs.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".union.tmp")
            frame.to_csv(temporary, index=False)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            union_path = archive / "Union" / destination.relative_to(root / "Data/News")
            union_path.parent.mkdir(parents=True, exist_ok=True)
            link = union_path.with_name(union_path.name + ".link.tmp")
            if link.exists():
                link.unlink()
            os.link(destination, link)
            os.replace(link, union_path)
        snapshot = {"retrieval_id": retrieval_id, "articles": len(catalog), "complete_bodies": len(bodies), "article_query_links": len(matches), "status": state["status"], "completed_phases": state["completed_phases"], "published_at_utc": datetime.now(timezone.utc).isoformat(), "historical_availability_verified": False}
        write_json_atomic(archive / "Union/snapshot.json", snapshot)
        save_state(union_articles=len(catalog), complete_bodies=len(bodies), article_query_links=len(matches), last_export_at_utc=snapshot["published_at_utc"])

    try:
        source_manifest = folder / "source_manifest.json"
        if not source_manifest.exists():
            sources = []
            for key, kind in [("news_catalog", "catalog"), ("news_full_text", "full"), ("news_prepared", "preserved")]:
                source = root / settings["paths"][key]
                if source.exists():
                    target = archive / "Original" / source.relative_to(root / "Data/News")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = file_hash(source)
                    if not target.exists():
                        shutil.copy2(source, target)
                    if file_hash(target) != digest:
                        raise RuntimeError("Archive source hash mismatch; no union replacement performed.")
                    sources.append({"source_id": f"S{len(sources) + 1:03d}", "kind": kind, "path": str(target), "sha256": digest})
            for source in sorted((root / "Data/News").rglob("*")):
                if not source.is_file() or "Retrievals" in source.parts or source.name.startswith("."):
                    continue
                target = archive / "Original" / source.relative_to(root / "Data/News")
                if any(item["path"] == str(target) for item in sources):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = file_hash(source)
                if not target.exists():
                    shutil.copy2(source, target)
                if file_hash(target) != digest:
                    raise RuntimeError("Archive metadata hash mismatch.")
                sources.append({"source_id": f"S{len(sources) + 1:03d}", "kind": "preserved", "path": str(target), "sha256": digest})
            historical = root.parent / "Archive/20260910/News"
            candidates = [(historical / "Guardian Raw/Raw Full New List.csv", "catalog"), *[(path, "full") for path in sorted((historical / "Full").glob("*.csv"))]]
            if include_catalog:
                source = Path(include_catalog).resolve()
                target = archive / "Original/Imports" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(source, target)
                if file_hash(source) != file_hash(target):
                    raise RuntimeError("Imported catalog archive hash mismatch.")
                candidates.append((target, "catalog"))
            for path, kind in candidates:
                if path.exists():
                    digest = file_hash(path)
                    if digest not in {item["sha256"] for item in sources}:
                        sources.append({"source_id": f"S{len(sources) + 1:03d}", "kind": kind, "path": str(path), "sha256": digest})
            write_json_once(source_manifest, {"sources": sources, "archive": str(archive)})
            write_json_once(archive / "original_manifest.json", {"sources": sources, "policy": "Original snapshots are preserved; Union is updated by article ID."})
        source_rows = json.loads(source_manifest.read_text())["sources"]
        for source in source_rows:
            path = Path(source["path"])
            if file_hash(path) != source["sha256"]:
                raise RuntimeError("A frozen source changed; collection stopped.")
            if source["kind"] == "preserved":
                continue
            for chunk in pd.read_csv(path, dtype=str, keep_default_na=False, chunksize=2000):
                for row in chunk.to_dict("records"):
                    if source["kind"] == "catalog":
                        add_article(row, source["source_id"])
                    elif row.get("bodyText", "").strip() and row["id"] not in bodies:
                        bodies[row["id"]] = {**row, "content_source_id": source["source_id"]}
        if set(bodies) - set(catalog):
            raise RuntimeError("Archived full text has IDs missing from every catalog; reconcile metadata before publishing.")
        seed_articles, seed_bodies = len(catalog), len(bodies)
        source_rows = [*source_rows, {"source_id": "S_API", "kind": "raw_response_directory", "path": str(folder / "pages"), "sha256": ""}]
        save_state("running", seed_articles=seed_articles, reused_complete_bodies=seed_bodies, union_articles=seed_articles, complete_bodies=seed_bodies)
        event("sources_loaded", articles=seed_articles, reused_full_texts=seed_bodies, source_count=len(source_rows) - 1)
        readme = "# News sources\n\nArticles are joined by id. Original files stay in Archive/Original; the merged copy is in Union and the data folder.\n\n- query_ids link to search_queries.csv and its saved search settings.\n- source_refs and content_source_id link to source_files.csv. Existing full text is kept; only missing text is added.\n- request_refs point to raw API replies in this retrieval folder.\n- market_articles.csv records search matches. Old files do not imply a search match.\n- state.json and events.jsonl record progress, waits and errors.\n- completed_phases means all search pages were read. It does not mean every full article was returned or historical availability was verified.\n"
        (root / "Data/News/README.md").write_text(readme, encoding="utf-8")
        (archive / "README.md").write_text(readme, encoding="utf-8")
        for phase in phases:
            months = pd.period_range(phase["start"], phase["end"], freq="M")[::-1]
            for month in months:
                start = max(phase["start"], month.start_time.date().isoformat())
                end = min(phase["end"], month.end_time.date().isoformat())
                month_had_download = False
                tasks = [{"query": query, "page": 1, "total": None, "pages": None, "seen": set(), "done": False} for query in plan]
                while any(not task["done"] for task in tasks):
                    for task in tasks:
                        if task["done"]:
                            continue
                        if (folder / "STOP").exists():
                            raise InterruptedError("Stop file detected.")
                        query, page = task["query"], task["page"]
                        params = {"from-date": start, "to-date": end, "order-by": "newest", "page-size": page_size, "page": page, "show-fields": "headline,trailText,bodyText,wordcount,byline,lastModified", "show-tags": "all", "show-references": "all", "show-rights": "all", **query["params"]}
                        cache = folder / "pages" / f"phase{phase['phase']}" / str(month) / query["query_id"] / f"page_{page:05d}.json"
                        reused = cache.exists()
                        save_state(phase=phase["phase"], month=str(month), query_id=query["query_id"], market=query["market_id"], page=page)
                        if reused:
                            envelope = json.loads(cache.read_text())
                            if envelope["params"] != params:
                                raise RuntimeError("Cached request does not match frozen search parameters.")
                        else:
                            failures = 0
                            while True:
                                if max_requests is not None and calls_this_run >= max_requests:
                                    raise InterruptedError("Requested call budget reached.")
                                today = datetime.now(timezone.utc).date().isoformat()
                                time.sleep(max(0, 1.1 - (time.monotonic() - last_call)))
                                state["requests_total"] += 1
                                state["requests_by_utc_date"][today] = state["requests_by_utc_date"].get(today, 0) + 1
                                calls_this_run += 1
                                save_state("running")
                                last_call = time.monotonic()
                                try:
                                    response = session.get("https://content.guardianapis.com/search", params={**params, "api-key": api_key}, timeout=60)
                                except requests.RequestException:
                                    failures += 1
                                    if failures >= 3:
                                        raise RuntimeError("Guardian transport failed three times; credentials omitted.") from None
                                    pause_until(time.time() + 30)
                                    continue
                                status = getattr(response, "status_code", 200)
                                if status == 429 or (status == 403 and any(word in getattr(response, "text", "").lower() for word in ("rate limit", "quota"))):
                                    retry = getattr(response, "headers", {}).get("Retry-After", "")
                                    until = time.time() + (max(60, int(retry)) if retry.isdigit() else 3600)
                                    save_state("waiting_quota", http_status=status, retry_at_utc=datetime.fromtimestamp(until, timezone.utc).isoformat())
                                    event("quota_wait", http_status=status, retry_at_utc=state["retry_at_utc"])
                                    publish()
                                    if not wait_for_quota:
                                        raise InterruptedError("Guardian quota exhausted.")
                                    pause_until(until)
                                    continue
                                if status in {500, 502, 503, 504} and failures < 2:
                                    failures += 1
                                    pause_until(time.time() + 30)
                                    continue
                                if status != 200:
                                    raise RuntimeError(f"Guardian HTTP {status}; credentials and request URL omitted.")
                                payload = response.json()["response"]
                                envelope = {"params": params, "received_at_utc": datetime.now(timezone.utc).isoformat(), "payload": payload}
                                break
                        payload = envelope["payload"]
                        total, pages = int(payload["total"]), int(payload["pages"])
                        if task["total"] is not None and (task["total"] != total or task["pages"] != pages):
                            raise RuntimeError("Guardian result counts changed across pages; saved pages retained for review.")
                        if total and int(payload["currentPage"]) != page:
                            raise RuntimeError("Guardian page mismatch.")
                        for item in payload["results"]:
                            published = pd.Timestamp(item["webPublicationDate"])
                            if not start <= published.date().isoformat() <= end or item["id"] in task["seen"]:
                                raise RuntimeError("Guardian returned an out-of-range or duplicate article.")
                        if not reused:
                            month_had_download = True
                            cache.parent.mkdir(parents=True, exist_ok=True)
                            write_json_once(cache, envelope)
                        request_ref = cache.relative_to(folder).as_posix()
                        for item in payload["results"]:
                            task["seen"].add(item["id"])
                            add_article(normalize_article(item, page), "S_API", query, request_ref)
                            fields = item.get("fields") or {}
                            if fields.get("bodyText", "").strip() and item["id"] not in bodies:
                                bodies[item["id"]] = {"id": item["id"], "bodyText": fields["bodyText"], "lastModified": fields.get("lastModified", ""), "downloaded_at_utc": envelope["received_at_utc"], "content_source_id": "S_API"}
                        task.update(total=total, pages=pages, page=page + 1, done=page >= pages)
                        if task["done"] and len(task["seen"]) != total:
                            raise RuntimeError("Guardian unique article count does not match its total.")
                        visited_pages += 1
                        save_state("running", pages_loaded=visited_pages, union_articles=len(catalog), complete_bodies=len(bodies), new_articles=len(catalog) - seed_articles, added_full_texts=len(bodies) - seed_bodies, article_query_links=len(matches))
                        event("page_reused" if reused else "page_saved", phase=phase["phase"], month=str(month), query_id=query["query_id"], page=page, rows=len(payload["results"]), union_articles=len(catalog), new_articles=state["new_articles"], added_full_texts=state["added_full_texts"])
                        if not reused and (visited_pages == 1 or visited_pages % 50 == 0):
                            publish()
                event("month_complete", phase=phase["phase"], month=str(month), union_articles=len(catalog))
                if month_had_download:
                    publish()
            if phase["phase"] not in state["completed_phases"]:
                state["completed_phases"].append(phase["phase"])
            save_state()
            event("phase_complete", phase=phase["phase"])
        save_state("completed")
        publish()
        write_json_atomic(folder / "complete.json", state)
        return pd.read_csv(root / settings["paths"]["news_catalog"], low_memory=False), pd.read_csv(root / settings["paths"]["news_market_articles"])
    except InterruptedError as exc:
        save_state("waiting_quota" if state.get("status") == "waiting_quota" else "paused", reason=str(exc))
        if catalog:
            publish()
        return pd.DataFrame(), pd.DataFrame()
    except Exception as exc:
        save_state("failed", error_type=type(exc).__name__, error=str(exc)[:240] if isinstance(exc, RuntimeError) else "Inspect the local collector; raw exception omitted to protect credentials.")
        event("failed", error_type=type(exc).__name__, error=state["error"])
        raise
    finally:
        lock.close()

def market_news_mask(catalog, market, associations=None):
    if associations is None and market["news"]["selection_policy"] == "market_only":
        raise MarketError("market_only requires collected market/article associations; run news collection first.")
    matches = set() if associations is None else set(associations.loc[associations["market_id"] == market["market_id"], "article_id"])
    selected = catalog["id"].isin(matches)
    if market["news"]["selection_policy"] == "shared_plus_market":
        selected |= catalog["sectionId"].isin(market["news"]["provider_options"]["guardian"]["sections"])
    return selected


def catalog_for_preopen_sessions(catalog, session_dates, market, start_date, end_date, associations=None):
    dates = pd.DatetimeIndex(pd.to_datetime(session_dates)).sort_values()
    if dates.has_duplicates:
        raise MarketError("Market session dates must be unique.")
    published = pd.to_datetime(catalog["webPublicationDate"], utc=True, errors="raise")
    selected = pd.Series(False, index=catalog.index)
    intervals = []
    for previous, target in zip(dates[:-1], dates[1:]):
        if str(start_date) <= previous.date().isoformat() <= str(end_date):
            timing = news_window(market, previous.date(), target.date())
            intervals.append(timing)
            selected |= (published >= pd.Timestamp(timing["start_utc"])) & (published < pd.Timestamp(timing["end_utc"]))
    selected &= market_news_mask(catalog, market, associations)
    return catalog.loc[selected].copy(), intervals


def normalize_article(item, page):
    fields = item.get("fields") or {}
    body_text = re.sub(r"\s+", " ", str(fields.get("bodyText") or "")).strip()
    return {
        "id": item.get("id", ""),
        "type": item.get("type", ""),
        "sectionId": item.get("sectionId", ""),
        "sectionName": item.get("sectionName", ""),
        "webPublicationDate": item.get("webPublicationDate", ""),
        "webTitle": item.get("webTitle", ""),
        "webUrl": item.get("webUrl", ""),
        "apiUrl": item.get("apiUrl", ""),
        "tags": json.dumps(item.get("tags", []), ensure_ascii=True),
        "references": json.dumps(item.get("references", []), ensure_ascii=True),
        "rights": json.dumps(item.get("rights", {}), ensure_ascii=True),
        "isHosted": item.get("isHosted", ""),
        "pillarId": item.get("pillarId", ""),
        "pillarName": item.get("pillarName", ""),
        "headline": fields.get("headline", ""),
        "trailText": re.sub(r"\s+", " ", str(fields.get("trailText") or "")).strip(),
        "bodyText_200": body_text[:200],
        "wordcount": fields.get("wordcount", ""),
        "byline": fields.get("byline", ""),
        "_fetch_page": page,
    }


if __name__ == "__main__":
    from dotenv import dotenv_values
    from .settings import load_settings

    parser = argparse.ArgumentParser(description="Resume the existing Guardian collector, preserving archived article unions.")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--retrieval-id", default="market_union_2006_2026")
    parser.add_argument("--backfill-start", default="2006-01-01")
    parser.add_argument("--include-catalog")
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    key = os.environ.get("GUARDIAN_API_KEY") or dotenv_values(root.parent / ".env").get("GUARDIAN_API_KEY")
    if not key:
        raise SystemExit("Guardian API key is missing; no request made.")
    try:
        collect_guardian(requests.Session(), load_settings(root), key, normalize_article, project_root=root, retrieval_id=args.retrieval_id, backfill_start=args.backfill_start, include_catalog=args.include_catalog)
    except Exception:
        raise SystemExit("Collector stopped. Read its local state.json; no credentials or request URLs are logged.") from None
