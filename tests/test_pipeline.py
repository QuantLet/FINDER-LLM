"""Run every paper stage with synthetic inputs and fake model replies.

The test fixes the calendar dates and performs no network request.
"""
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
import json, sys, tempfile, unittest
import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from finder_core import pipeline, workflow
from finder_core.tools import ToolContext
from finder_core.markets import news_window, market_fingerprint
from finder_core.settings import load_settings
CALLS = []
SUPERVISORS = []

class FakeModel:

    def __init__(self, **kwargs):
        pass

    def chat_json(self, **kwargs):
        stage = kwargs['stage']
        payload = json.loads(kwargs['messages'][-1]['content'])
        CALLS.append(stage)
        inherited = payload.get('future_events', [])
        actions = {'new_events': [], 'inherited': [{'event_id': e['event_id'], 'action': 'KEEP', 'selection_rank': 1, 'reason': 'Same dated source.', 'event': None} for e in inherited]}
        if stage.startswith('news.catalogue_screen'):
            return {'candidate_indices': list(range(5))}
        if stage == 'news.reader_selection':
            if not inherited:
                actions['new_events'] = [{'event_id': 'NEW', 'event_key': 'central_bank|rate_decision|march_meeting', 'title': 'Central bank rate decision', 'certainty': 0.9, 'certainty_basis': 'A published meeting date.', 'summary': 'The bank will publish a rate decision on March 10, 2025.', 'event_start_date': '2025-03-10', 'event_end_date': '2025-03-10', 'event_time': 'UNKNOWN', 'timezone': 'UNKNOWN', 'timing_basis': 'The source gives the date.', 'status': 'UPCOMING', 'source_passage_refs': ['A0:P1'], 'event_kind': 'MILESTONE', 'selection_rank': 1}]
            return {'future_events': actions, 'selections': [{'index': i, 'reason': 'Market context.'} for i in range(5)], 'anomaly_score': 0.2}
        if stage == 'news.calendar_review':
            return {'future_events': actions}
        if stage == 'news.assistant_summary':
            return {'certainty_updates': [], 'summaries': {f'N{i:03d}': 'The dated report gives market context.' for i in range(5)}, 'event_records': [], 'anomaly_score': 0.4}
        if stage.endswith('evidence_report'):
            report = {'evidence_status': 'SUFFICIENT', 'assessment': 'MIXED', 'supporting_evidence': [], 'counter_evidence': [], 'report': 'Synthetic offline evidence.', 'anomaly_score': 0.25}
            if stage.startswith('news.'):
                events = payload['tool_receipts'][0]['payload']['future_events']
                report.update(future_events={'new_events': [], 'inherited': [{'event_id': e['event_id'], 'action': 'KEEP', 'selection_rank': 1, 'reason': 'Same dated source.', 'event': None} for e in events]}, event_assessments=[])
            return report
        if stage == 'supervisor.independent':
            SUPERVISORS.append(payload)
            return {'report': {'evidence': 'Synthetic evidence.', 'counter_evidence': 'Synthetic counter.', 'rationale': 'Offline integration test.'}, 'debate_judgement': {'stronger_side': 'BALANCED', 'claim_judgements': {}, 'summary': 'No debate.'}, 'memory_comparison': {'reference_case_ids': [], 'reason': 'Use only resolved earlier dates.'}, 'anomaly_score': 0.3}
        raise AssertionError(stage)

class FakeBank:

    def __init__(self, *args):
        pass

    def load(self):
        return ({'input_schema_fingerprint': '0' * 64, 'indicator_fields': ['close'] + [f'f{i}' for i in range(62)]}, [])

    def freeze_snapshot(self, **kwargs):
        assert len(kwargs['window_observation_dates']) == 126
        return {'status': 'NO_EXAMPLES', 'examples': []}

class IntegrationTest(unittest.TestCase):

    def test_two_windows_all_stages_and_resolved_memory(self):
        CALLS.clear()
        SUPERVISORS.clear()
        with tempfile.TemporaryDirectory(prefix='finder-offline-') as tmp:
            root = Path(tmp)
            settings = load_settings(ROOT)
            market = settings['market']
            (root / 'Data/Global').mkdir(parents=True)
            (root / 'Data/Global/overseas_manifest.json').write_text('{}')
            chart_paths = []
            for i in range(3):
                p = root / f'Chart{i + 1}.png'
                p.write_bytes(b'offline chart ' + bytes([i]))
                chart_paths.append(p)
            items = []
            for (number, (cutoff, target)) in enumerate([('2025-03-05', '2025-03-06'), ('2025-03-06', '2025-03-07')], 1):
                history = pd.DataFrame(np.ones((126, 63)), index=pd.bdate_range(end=cutoff, periods=126), columns=['close'] + [f'f{i}' for i in range(62)])
                timing = news_window(market, cutoff, target, session_dates=history.index[-21:].date)
                articles = [{'index': i, 'article_id': f'synthetic-{i}', 'published_at': '2025-03-05T12:00:00+00:00', 'title': f'Market report {i}', 'content': 'The central bank will publish a rate decision on March 10, 2025. This synthetic text is used only in an offline software test.'} for i in range(5)]
                context = ToolContext(window_id=f'W{number:04d}', asset='^GSPC', information_cutoff=cutoff, forecast_target_date=target, indicator_history=history, numeric_snapshot={'context_window_size': 21}, chart_paths=chart_paths, news_articles=articles, indicator_definitions={}, source_manifest={'market': market, 'market_fingerprint': market_fingerprint(market), 'news_window': timing}, crash_percentile=0.05, anomaly_threshold=0.5)
                window = workflow.WorkflowWindow(window_id=f'W{number:04d}', asset='^GSPC', information_cutoff=date.fromisoformat(cutoff), forecast_target_date=date.fromisoformat(target), window_start_date=history.index[0].date(), window_end_date=date.fromisoformat(cutoff), reference_start_date=date(2024, 9, 1), horizon_days=1, crash_percentile=0.05, anomaly_threshold=0.5, task_definition='Offline test of the next-session event.', market=market, news_window=timing)
                items.append(SimpleNamespace(window=window, tool_context=context))
            labels = root / settings['paths']['labels']
            labels.parent.mkdir(parents=True)
            pd.DataFrame([{'date': '2025-03-05', 'min_future_logret': -0.03, 'crash_threshold': -0.02, 'crash_label': 1}]).to_csv(labels, index=False)
            progress = []
            with patch.object(pipeline, 'prepare_prediction_windows', return_value=tuple(items)), patch.object(pipeline, 'FewShotBank', FakeBank), patch.object(workflow, 'ModelClient', FakeModel), patch('finder_core.future_events.scheduled_horizon', return_value='2025-04-03'), patch('finder_core.future_events.retention_end', side_effect=lambda calendar, ended: (pd.Timestamp(ended) + pd.offsets.BDay(3)).date().isoformat()):
                scores = pipeline.run(root, settings, {'run_id': 'offline', 'start_date': '2025-03-05', 'end_date': '2025-03-06', 'max_windows': 2}, api_key='not-a-real-key', progress=progress.append)
            self.assertEqual(len(scores), 2)
            self.assertEqual(len(CALLS), 16)
            self.assertEqual(set(CALLS), {'quantitative.evidence_report', 'chart.evidence_report', 'news.catalogue_screen.1', 'news.reader_selection', 'news.calendar_review', 'news.assistant_summary', 'news.evidence_report', 'supervisor.independent'})
            self.assertEqual(SUPERVISORS[0]['memory_reference_context']['recent_decisions'], [])
            self.assertEqual(SUPERVISORS[1]['memory_reference_context']['recent_decisions'][0]['outcome']['observed_anomaly_label'], 1)
            for target in ['2025-03-06', '2025-03-07']:
                report = json.loads((root / 'Runs/^GSPC/offline/Reports' / f'{target}.json').read_text())
                self.assertEqual(len(report['agents']), 6)
                self.assertNotIn('anomaly_score', report['agents']['News Assistant'])
                evidence = json.loads((root / 'Runs/^GSPC/offline/Private' / target / 'evidence.json').read_text())
                self.assertEqual(len(evidence['reports']['NEWS']['future_events']), 1)
            self.assertEqual(progress[-1]['completed'], 2)
            self.assertFalse((root / 'Results').exists())
            self.assertFalse(any(('not-a-real-key' in p.read_text() for p in root.rglob('*.json'))))
if __name__ == '__main__':
    unittest.main()
