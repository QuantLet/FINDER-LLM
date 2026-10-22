"""Connect the local app to the fixed six-role research workflow."""

from datetime import date, datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SCORE_FIELDS = ['quantitative_expert', 'chart_expert', 'news_reader', 'news_expert', 'decision_supervisor']


def get_paper_config():
    parameters = json.loads((ROOT / 'config/settings.json').read_text())
    catalog = json.loads((ROOT / 'config/markets.json').read_text())
    markets = [{'id': ticker, 'name': catalog['market_registry']['markets'][ticker]['name']} for ticker in catalog['markets']]
    readiness = {}
    for market in markets:
        ticker = market['id']
        required = [f'Data/{ticker}/Market/df_OHLCV.csv', f'Data/{ticker}/Market/df_Indicators.csv', f'Data/{ticker}/Market/df_Crash_Label.csv', f'Data/{ticker}/Reference/few_shot_bank.jsonl', f'Data/{ticker}/Reference/few_shot_bank_manifest.json', f'Data/{ticker}/Reference/few_shot_scaler.json', 'Data/News/Guardian Raw/Raw Full New List.csv', 'Data/News/Full/Full News Complete.csv', 'Data/News/market_articles.csv', 'Data/Global/overseas_manifest.json']
        missing = [name for name in required if not (ROOT / name).is_file()]
        readiness[ticker] = {'ready': not missing, 'missing': missing}
    return {
        'title': 'Paper workflow',
        'summary': 'Six fixed roles use market values, charts and news. The Supervisor gives the final risk score. This mode has no debate.',
        'defaults': {key: parameters[key] for key in ['ticker', 'start_date', 'end_date', 'max_windows']},
        'markets': markets,
        'models': {**{name: 'gpt-5-mini' for name in SCORE_FIELDS}, 'news_assistant': 'gpt-5-nano'},
        'steps': [
            {'name': 'Evidence', 'description': 'Quantitative and Chart Experts work independently. The News Reader selects articles; the Assistant summarizes them; the News Expert reviews the summaries.'},
            {'name': 'Decision', 'description': 'The Supervisor reads the evidence, prior cases and outcomes available at the cutoff. It returns a score and a report.'},
            {'name': 'Evaluation', 'description': 'After prediction, the review notebook joins outcomes by information cutoff and fits each threshold using past outcomes only.'},
        ],
        'readiness': readiness,
        'output': 'Runs/{ticker}/{run_id}',
        'fields': SCORE_FIELDS,
        'input_help': 'Use the four notebooks in notebooks/prepare with your own licensed data. File presence is checked here; the core checks content before any model call.',
    }


def run_paper(config, connection, progress):
    if not isinstance(config, dict) or set(config) - {'ticker', 'start_date', 'end_date', 'max_windows'}:
        raise ValueError('Paper settings may include only market, dates and number of windows.')
    info = get_paper_config()
    parameters = {**info['defaults'], **config}
    ticker = parameters['ticker']
    if ticker not in info['readiness']:
        raise ValueError('Choose one of the six paper markets.')
    start, end = date.fromisoformat(parameters['start_date']), date.fromisoformat(parameters['end_date'])
    if start > end:
        raise ValueError('The start date must be on or before the end date.')
    if type(parameters['max_windows']) is not int or not 1 <= parameters['max_windows'] <= 5:
        raise ValueError('Choose between 1 and 5 windows per run.')
    if not info['readiness'][ticker]['ready']:
        raise ValueError('Private inputs are missing. Complete the data preparation steps shown in Paper workflow.')
    if connection.get('mode') != 'live':
        raise ValueError('Paper runs need an API connection. Use Custom workflow for the free demo.')
    if connection.get('endpoint', '').rstrip('/') != 'https://api.openai.com/v1':
        raise ValueError('Paper mode uses the original OpenAI endpoint. Use Custom workflow for another endpoint.')
    from finder_core.pipeline import run
    from finder_core.settings import load_settings

    parameters['run_id'] = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    settings = load_settings(ROOT, ticker=ticker)

    def show_progress(event):
        progress({'kind': 'message', 'message': f"{event.get('stage', 'Working')}: {event.get('completed', 0)}/{event.get('total', '?')} windows", **event})

    scores = run(ROOT, settings, parameters, api_key=connection.get('api_key'), base_url=connection['endpoint'], progress=show_progress)
    destination = ROOT / 'Runs' / ticker / parameters['run_id']
    reports = [json.loads(path.read_text()) for path in sorted((destination / 'Reports').glob('*.json'))]
    return {'mode': 'paper', 'output_directory': destination.relative_to(ROOT).as_posix(), 'rows': json.loads(scores.to_json(orient='records')), 'reports': reports, 'message': 'New scores and reports are saved in this run folder. Published results have not changed.'}
