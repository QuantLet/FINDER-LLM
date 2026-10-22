"""Offline paper regressions. All generated files stay in temporary directories."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from finder_core.config import RuntimeConfig
from finder_core.contracts import evidence_report_schema, news_assistant_schema, news_reader_schema, supervisor_schema
from finder_core.pipeline import run_directory
from finder_core.prompts import PROMPTS
from finder_core.public_outputs import SCORES, STAGES, evaluate, write_outputs
from finder_core.supervisor import audit_payload

AGENTS = ['quantitative_expert', 'chart_expert', 'news_reader', 'news_expert', 'decision_supervisor']
PROMPT_KEYS = ('shared.task_definition', 'quantitative.report.system', 'chart.report.system', 'news.reader.system', 'news.assistant.system', 'news.expert.system', 'supervisor.system', 'response.rules', 'shared.indicator_guide', 'schema_repair.system', 'news.discovery.system', 'news.review.system')
# Digests of the active prompts and schemas before the public-code simplification.
PROMPT_DIGEST = 'dc4c9f416f0a6e2ebfd3c50d2e45c8f82cca324b3a3a157145f4d126e9ba7eea'
SCHEMA_DIGEST = '8e433fbc667990a30dc6ecb0bfbe9bbd7628e548159fbfdaa35c26cf4a0d78f8'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='finder-paper-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def evaluate_rows(self, rows, labels, name='evaluation'):
        destination = self.root / 'Runs' / name
        destination.mkdir(parents=True)
        rows.to_csv(destination / 'scores.csv', index=False)
        labels_path = self.root / (name + '-labels.csv')
        labels.to_csv(labels_path, index=False)
        return evaluate(destination, labels_path)

    def synthetic_rows(self, outcomes):
        rows = pd.DataFrame({'target_date': ['2025-01-10', '2025-01-11', '2025-01-12'], 'information_cutoff': ['2025-01-01', '2025-01-02', '2025-01-10']})
        for agent in AGENTS:
            rows[agent] = [.2, .9, .8]
        labels = pd.DataFrame({'date': rows.information_cutoff, 'crash_label': outcomes, 'min_future_logret': [-.02, -.03, .01], 'crash_threshold': [-.015] * 3})
        return rows, labels

    def test_all_published_thresholds_predictions_and_metrics(self):
        folder = ROOT / 'FINDER' / 'Official'
        paths = list(folder.glob('*.csv'))
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        source = pd.read_csv(folder / 'daily_scores_all_markets.csv')
        thresholds = pd.read_csv(folder / 'daily_thresholds.csv')
        metrics = pd.read_csv(folder / 'market_agent_metrics.csv')
        checked = 0
        for market, frame in source.groupby('market', sort=False):
            with self.subTest(market=market):
                scores = frame[['target_date', 'information_cutoff', *AGENTS]]
                labels = frame[['information_cutoff', 'actual_label', 'actual_log_return', 'crash_threshold']].rename(columns={'information_cutoff': 'date', 'actual_label': 'crash_label', 'actual_log_return': 'min_future_logret'})
                actual, summary = self.evaluate_rows(scores, labels, name=market)
                for agent in AGENTS:
                    expected = thresholds.loc[thresholds.market.eq(market) & thresholds.agent.eq(agent)].sort_values('target_date')
                    self.assertEqual(actual.target_date.tolist(), expected.target_date.tolist())
                    np.testing.assert_array_equal(actual[agent + '_threshold'], expected.threshold)
                    np.testing.assert_array_equal(actual[agent + '_alert'], expected.prediction)
                    row = summary.set_index('agent').loc[agent]
                    saved = metrics.loc[metrics.market.eq(market) & metrics.agent.eq(agent)].iloc[0]
                    for field in ['n', 'events', 'tn', 'fp', 'fn', 'tp', 'accuracy', 'roc_auc', 'precision', 'recall', 'f1', 'mcc']:
                        self.assertAlmostEqual(row[field], saved[field], places=12, msg=f'{market}/{agent}/{field}')
                    self.assertAlmostEqual(row.average_precision, saved.ap, places=12)
                    checked += len(expected)
        self.assertEqual(checked, 11395)
        self.assertEqual({path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}, before)

    def test_thresholds_exclude_outcomes_not_yet_available(self):
        rows, labels = self.synthetic_rows([1, 1, 0])
        actual, _ = self.evaluate_rows(rows, labels)
        altered = labels.copy()
        altered.loc[1:, 'crash_label'] = [0, 1]
        changed, _ = self.evaluate_rows(rows, altered, name='changed-future-outcomes')
        for agent in AGENTS:
            np.testing.assert_array_equal(actual[agent + '_threshold'], [.5, .5, .2])
            np.testing.assert_array_equal(actual[agent + '_threshold'], changed[agent + '_threshold'])

    def test_initial_threshold_stays_fixed_without_historical_events(self):
        rows, labels = self.synthetic_rows([0, 0, 1])
        actual, _ = self.evaluate_rows(rows, labels)
        for agent in AGENTS:
            np.testing.assert_array_equal(actual[agent + '_threshold'], [.5, .5, .5])

    def test_evaluation_rejects_missing_outcomes_before_writing_results(self):
        rows, labels = self.synthetic_rows([1, 1, 0])
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.evaluate_rows(rows, labels.iloc[:-1])
        self.assertFalse((self.root / 'Runs/evaluation/evaluation.csv').exists())
        self.assertFalse((self.root / 'Runs/evaluation/metrics.csv').exists())

    def test_reports_expose_five_scores_and_mask_assistant_scores(self):
        destination = self.root / 'Runs' / 'synthetic'
        day = destination / 'Private' / '2025-01-03'
        stages = day / 'validated_stages'
        stages.mkdir(parents=True)
        for _, stage in STAGES:
            response = {'anomaly_score': .4, 'report': 'Synthetic response.'}
            if stage == 'news.assistant_summary':
                response['summaries'] = {'N001': 'Synthetic summary with risk score: 0.4.'}
                response['nested'] = {'anomaly_score': .7, 'text': 'Retained summary.'}
            (stages / (stage + '.json')).write_text(json.dumps({'output': response}))
        result = {'target_date': '2025-01-03', 'information_cutoff': '2025-01-02', 'response': {'anomaly_score': .6, 'report': 'Synthetic supervisor response.'}}
        evidence = {'agent_scores': {stage: .4 for stage in SCORES.values()}}
        write_outputs(destination, result, evidence, day, [])
        scores = pd.read_csv(destination / 'scores.csv')
        self.assertEqual(scores.columns.tolist(), ['target_date', 'information_cutoff', *AGENTS])
        self.assertEqual(scores.decision_supervisor.iloc[0], .6)
        report = json.loads((destination / 'Reports/2025-01-03.json').read_text())
        assistant = report['agents']['News Assistant']
        self.assertNotIn('anomaly_score', assistant)
        self.assertNotIn('anomaly_score', assistant['nested'])
        self.assertEqual(assistant['nested']['text'], 'Retained summary.')
        self.assertIn('[withheld]', assistant['summaries']['N001'])
        self.assertEqual(len(report['agents']), 6)
        self.assertTrue((destination / 'Reports/2025-01-03.md').is_file())

    def test_paper_routes_use_six_roles_and_documented_models(self):
        config = RuntimeConfig.from_settings()
        routes = config.agent_routes()
        self.assertEqual(set(routes), {*AGENTS, 'news_assistant'})
        self.assertEqual(config.openai_reasoning_effort, 'low')
        for agent, route in routes.items():
            self.assertEqual(route['model'], 'gpt-5-nano' if agent == 'news_assistant' else 'gpt-5-mini')
            self.assertEqual(route['modality'], 'vision' if agent in {'chart_expert', 'decision_supervisor'} else 'text')

    def test_new_runs_default_to_runs_and_reject_existing_destinations(self):
        expected = self.root / 'Runs' / '^GSPC' / 'sample'
        self.assertEqual(run_directory(self.root, '^GSPC', 'sample'), expected)
        self.assertFalse(expected.exists())
        expected.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            run_directory(self.root, '^GSPC', 'sample')

    def test_run_paths_cannot_enter_saved_artifacts(self):
        for folder in ['Results', 'FINDER', 'Benchmark', 'Figures', 'Examples', 'FigureSources']:
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                run_directory(self.root, '^GSPC', 'sample', output_root=self.root / folder / 'nested')
        (self.root / 'FINDER').mkdir()
        (self.root / 'shortcut').symlink_to(self.root / 'FINDER', target_is_directory=True)
        with self.assertRaises(ValueError):
            run_directory(self.root, '^GSPC', 'sample', output_root=self.root / 'shortcut')

    def test_run_paths_reject_traversal(self):
        for run_id in ['../sample', '/tmp/sample', '.', '..', 'a/b', 'a\\b', '']:
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                run_directory(self.root, '^GSPC', run_id)
        for ticker in ['../FINDER', '/tmp', '.', '..', 'a/b', 'a\\b', '']:
            with self.subTest(ticker=ticker), self.assertRaises(ValueError):
                run_directory(self.root, ticker, 'sample')

    def test_supervisor_rejects_current_scores_and_outcomes(self):
        for field in ['anomaly_score', 'current_label', 'actual_target_return', 'realized_target_return', 'observed_anomaly_label']:
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'Current score'):
                audit_payload({'current_observation': {field: .1}}, '2025-01-03')

    def test_supervisor_accepts_only_resolved_history_at_cutoff(self):
        case = {'information_cutoff': '2025-01-01', 'forecast_target_date': '2025-01-02', 'prior_anomaly_score': .2, 'outcome': {'outcome_available_at': '2025-01-02', 'realized_target_return': -.02, 'observed_anomaly_label': 1}}
        payload = {'memory': {'recent_decisions': [case]}}
        audit_payload(payload, '2025-01-02')
        future = deepcopy(payload)
        future['memory']['recent_decisions'][0]['outcome']['outcome_available_at'] = '2025-01-03'
        with self.assertRaisesRegex(RuntimeError, 'Future resolved outcome'):
            audit_payload(future, '2025-01-02')
        future = deepcopy(payload)
        future['memory']['recent_decisions'][0]['forecast_target_date'] = '2025-01-03'
        with self.assertRaisesRegex(RuntimeError, 'Future history'):
            audit_payload(future, '2025-01-02')

    def test_active_paper_prompts_match_original_baseline(self):
        self.assertEqual(digest({key: PROMPTS[key] for key in PROMPT_KEYS}), PROMPT_DIGEST)

    def test_active_response_schemas_match_original_baseline(self):
        schemas = {**{'reader_' + str(n): news_reader_schema(list(range(1, n + 1))) for n in [0, 5, 1000]}, **{'assistant_' + str(n): news_assistant_schema(list(range(1, n + 1))) for n in [0, 1, 5]}, **{'evidence_' + domain: evidence_report_schema(domain) for domain in ['QUANTITATIVE', 'CHART', 'NEWS']}, 'supervisor_empty': supervisor_schema([], []), 'supervisor_memory': supervisor_schema([], ['MEM01', 'MEM02'])}
        self.assertEqual(digest(schemas), SCHEMA_DIGEST)


if __name__ == '__main__':
    unittest.main()
