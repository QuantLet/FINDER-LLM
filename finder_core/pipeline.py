"""The paper workflow: inputs, three evidence branches, supervisor, saved outputs."""

from datetime import date
from pathlib import Path
import hashlib
import json
import threading
import re
import pandas as pd
from . import workflow
from .config import RuntimeConfig, DECISION_SUPERVISOR
from .contracts import validate_supervisor
from .data import prepare_prediction_windows
from .events import TraceRecorder, AgentEventLogger, write_json_atomic
from .few_shot import FewShotBank
from .supervisor import build_request
from .model_context import without_anomaly_scores
from .public_outputs import write_outputs


def run_directory(root, ticker, run_id, output_root=None):
    """Choose a new run folder without writing into the saved research results."""
    root = Path(root).resolve()
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", run_id):
        raise ValueError("Use a short run name with letters, numbers, dots, dashes or underscores.")
    if not isinstance(ticker, str) or not ticker or ticker in {".", ".."} or any(char in ticker for char in "/\\"):
        raise ValueError("Invalid market ID.")
    parent = Path(output_root) if output_root is not None else root / "Runs"
    if not parent.is_absolute():
        parent = root / parent
    destination = (parent / ticker / run_id).resolve()
    for protected in (root / "Results", root / "FINDER", root / "Benchmark", root / "Figures", root / "Examples", root / "FigureSources"):
        if destination == protected.resolve() or protected.resolve() in destination.parents:
            raise ValueError("Saved research outputs are read-only. Choose a new folder under Runs.")
    if destination.exists():
        raise FileExistsError(f"This run already exists: {destination}. Choose a new run name.")
    return destination


def run(root, settings, parameters, *, api_key=None, base_url=None, progress=None, output_root=None):
    """Prepare inputs, run the evidence branches, then save one supervisor result per date."""
    root = Path(root).resolve()
    run_id = parameters['run_id']
    destination = run_directory(root, settings['ticker'], run_id, output_root)
    if not (root / 'Data/Global/overseas_manifest.json').is_file():
        raise FileNotFoundError('Data/Global/overseas_manifest.json')
    config = RuntimeConfig.from_settings(settings)
    config.validate()
    items = prepare_prediction_windows(root, settings, start_date=date.fromisoformat(parameters['start_date']), end_date=date.fromisoformat(parameters['end_date']), max_windows=parameters['max_windows'])
    if not items:
        raise ValueError('No prediction windows')
    bank = FewShotBank(root / settings['paths']['few_shot_bank'])
    bank_manifest, _ = bank.load()
    destination.mkdir(parents=True)
    write_json_atomic(destination / 'configuration.json', {'settings': settings, 'parameters': parameters, 'routes': config.agent_routes(), 'source_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}})
    semaphore = threading.BoundedSemaphore(config.max_concurrent_model_calls)
    records = []
    prior_events = []
    for position, item in enumerate(items):
        target = item.window.forecast_target_date.isoformat()
        cutoff = item.window.information_cutoff.isoformat()
        def report_progress(update):
            if progress:
                progress({"completed": position, "total": len(items), "target_date": target, **update})
        report_progress({"stage": "prepare"})
        day = destination / 'Private' / target
        day.mkdir(parents=True)
        recorder = TraceRecorder(day / 'calls.jsonl')
        events = AgentEventLogger(day / 'events.jsonl')
        registry, clients, store = workflow.create_execution_resources(config=config, tool_context=item.tool_context, stage_root=day, recorder=recorder, semaphore=semaphore, api_key=api_key, base_url=base_url, progress=report_progress)
        registry.prior_future_events = prior_events
        evidence = workflow._run_evidence_stage(config=config, window=item.window, registry=registry, clients=clients, stage_store=store, event_logger=events, run_id=run_id)
        write_json_atomic(day / 'evidence.json', evidence)
        news_capture = json.loads((day / 'inputs/news.evidence_report_01.json').read_text())
        summaries = without_anomaly_scores(json.loads(news_capture['messages'][1]['content'])['tool_receipts'][0]['payload']['summaries'])
        content_capture = json.loads((day / 'inputs/news.assistant_summary_01.json').read_text())
        articles = {a['index']: a for a in json.loads(content_capture['messages'][1]['content'])['content_receipt']['payload']['articles']}
        excerpts = [{'article_ref': a['article_ref'], 'published_at': a['published_at'], 'title': a['title'], 'verbatim_opening_excerpt': articles[a['index']]['content'][:1500], 'excerpt_characters': len(articles[a['index']]['content'][:1500])} for a in sorted(summaries, key=lambda a: (a['published_at'], a['index']), reverse=True)[:3]]
        fewshot = bank.freeze_snapshot(asset=settings['ticker'], information_cutoff=cutoff, forecast_target_date=target, forecast_horizon_days=1, crash_percentile=config.crash_percentile, input_schema_fingerprint=bank_manifest['input_schema_fingerprint'], indicator_fields=bank_manifest['indicator_fields'], window_observation_dates=list(item.tool_context.observation_dates))
        history = [r for r in records if r['target_date'] <= cutoff][-5:][::-1]
        recent = []
        if history:
            labels = pd.read_csv(root / settings['paths']['labels'], index_col='date')
            for previous in history:
                row = labels.loc[previous['information_cutoff']]
                response = previous['response']
                recent.append({'decision_id': run_id + '-' + previous['target_date'], 'agent_id': DECISION_SUPERVISOR, 'information_cutoff': previous['information_cutoff'], 'forecast_target_date': previous['target_date'], 'prior_anomaly_score': response['anomaly_score'], 'prior_decision': response['debate_judgement']['stronger_side'], 'prior_report': without_anomaly_scores(response['report']), 'bounded_rationale': response['report']['rationale'], 'outcome': {'outcome_available_at': previous['target_date'], 'realized_target_return': float(row.min_future_logret), 'frozen_tail_threshold': float(row.crash_threshold), 'observed_anomaly_label': int(row.crash_label)}})
        memory = {'mode': 'agent_decision_history', 'owner_agent_id': DECISION_SUPERVISOR, 'status': 'AVAILABLE' if recent else 'NO_MEMORY', 'recent_decisions': recent}
        fixture = {'window': item.window.public_dict(), 'numeric': item.tool_context.current_numeric_reference, 'reports': evidence['reports'], 'fewshot': fewshot, 'news_summaries': summaries, 'news_excerpts': excerpts}
        payload, messages, schema, ids = build_request(fixture, memory)
        response = workflow._cached_validated_call(clients=clients, stage_store=store, event_logger=events, run_id=run_id, window_id=item.window.window_id, agent_id=DECISION_SUPERVISOR, stage_id='supervisor.independent', stage_group='prediction', input_payload=payload, messages=messages, schema=schema, validator=lambda value: validate_supervisor(value, valid_claim_ids=[], valid_memory_case_ids=ids), image_paths=item.tool_context.chart_paths, image_hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in item.tool_context.chart_paths])
        result = {'target_date': target, 'information_cutoff': cutoff, 'response': response}
        write_json_atomic(day / 'result.json', result)
        write_outputs(destination, result, evidence, day, item.tool_context.chart_paths)
        records.append(result)
        prior_events = evidence['reports']['NEWS'].get('future_events', [])
        report_progress({'stage': 'saved', 'completed': position + 1})
    return pd.read_csv(destination / 'scores.csv')
