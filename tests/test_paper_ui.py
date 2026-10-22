"""Check the app's paper adapter without private data or an API call."""

from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

from studio import paper


class PaperAdapterTests(TestCase):
    def test_readiness_lists_the_six_markets_and_five_scores(self):
        config = paper.get_paper_config()
        self.assertEqual(len(config['markets']), 6)
        self.assertEqual(len(config['fields']), 5)
        self.assertNotIn('news_assistant', config['fields'])
        self.assertEqual(config['models']['news_assistant'], 'gpt-5-nano')
        self.assertTrue(all(not name.startswith('/') for info in config['readiness'].values() for name in info['missing']))

    def test_input_check_stops_before_model_execution(self):
        config = paper.get_paper_config()
        config['readiness'][config['defaults']['ticker']] = {'ready': False, 'missing': ['Data/input.csv']}
        with patch.object(paper, 'get_paper_config', return_value=config), patch('finder_core.pipeline.run') as run:
            with self.assertRaisesRegex(ValueError, 'Private inputs are missing'):
                paper.run_paper({}, {'mode': 'live', 'endpoint': 'https://api.openai.com/v1', 'api_key': 'test-key'}, lambda event: None)
            run.assert_not_called()

    def test_edited_paper_method_and_invalid_windows_are_rejected(self):
        for value in [{'prompt': 'Replace the method'}, {'ticker': '../outside'}, {'max_windows': 0}, {'max_windows': True}, {'max_windows': 6}, {'start_date': '2025-03-06', 'end_date': '2025-03-05'}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                paper.run_paper(value, {}, lambda event: None)

    def test_paper_endpoint_cannot_be_replaced(self):
        config = paper.get_paper_config()
        config['readiness'][config['defaults']['ticker']]['ready'] = True
        with patch.object(paper, 'get_paper_config', return_value=config), self.assertRaisesRegex(ValueError, 'original OpenAI endpoint'):
            paper.run_paper({}, {'mode': 'live', 'endpoint': 'https://example.test/v1'}, lambda event: None)

    def test_new_runs_receive_separate_keys_and_unique_output_names(self):
        config = paper.get_paper_config()
        config['readiness'][config['defaults']['ticker']]['ready'] = True
        calls = []

        def fake_run(root, settings, parameters, **kwargs):
            calls.append((deepcopy(parameters), kwargs['api_key']))
            return pd.DataFrame([{'target_date': '2025-03-06', 'information_cutoff': '2025-03-05', **dict.fromkeys(paper.SCORE_FIELDS, 0.25)}])

        with patch.object(paper, 'get_paper_config', return_value=config), patch('finder_core.pipeline.run', side_effect=fake_run):
            first = paper.run_paper({}, {'mode': 'live', 'endpoint': 'https://api.openai.com/v1', 'api_key': 'first-test-key'}, lambda event: None)
            second = paper.run_paper({}, {'mode': 'live', 'endpoint': 'https://api.openai.com/v1', 'api_key': 'second-test-key'}, lambda event: None)
        self.assertNotEqual(first['output_directory'], second['output_directory'])
        self.assertTrue(first['output_directory'].startswith('Runs/'))
        self.assertEqual([key for parameters, key in calls], ['first-test-key', 'second-test-key'])
        self.assertNotIn('test-key', json.dumps([first, second, [parameters for parameters, key in calls]]))
