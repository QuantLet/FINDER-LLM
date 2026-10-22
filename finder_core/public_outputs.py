from pathlib import Path
import json
import os
import pandas as pd
import numpy as np
from sklearn.metrics import f1_score, precision_score, recall_score, matthews_corrcoef, roc_auc_score, average_precision_score, confusion_matrix, accuracy_score
from .model_context import without_anomaly_scores
from .events import write_json_atomic

STAGES = [('Quantitative Expert', 'quantitative.evidence_report'), ('Chart Expert', 'chart.evidence_report'), ('News Reader', 'news.reader_selection'), ('News Assistant', 'news.assistant_summary'), ('News Expert', 'news.evidence_report'), ('Decision Supervisor', 'supervisor.independent')]
SCORES = {'quantitative_expert': 'quantitative.evidence_report', 'chart_expert': 'chart.evidence_report', 'news_reader': 'news.reader_selection', 'news_expert': 'news.evidence_report'}


def write_outputs(destination, result, evidence, day, charts):
    target = result['target_date']
    folder = destination / 'Reports'
    folder.mkdir(exist_ok=True)
    outputs = {}
    for name, stage in STAGES:
        captures = sorted((day / 'inputs').glob(stage + '_*.json'))
        validated = day / 'validated_stages' / (stage + '.json')
        response = json.loads(validated.read_text())['output'] if validated.exists() else next(json.loads(p.read_text())['response_json'] for p in reversed(captures) if 'response_json' in json.loads(p.read_text()))
        if stage == 'supervisor.independent':
            response = result['response']
        outputs[name] = without_anomaly_scores(response) if stage == 'news.assistant_summary' else response
    write_json_atomic(folder / (target + '.json'), {'target_date': target, 'information_cutoff': result['information_cutoff'], 'agents': outputs})
    text = '# ' + target + '\n\nInformation cutoff: ' + result['information_cutoff'] + '\n\n'
    text += '\n'.join('![Chart](<' + os.path.relpath(p, folder) + '>)\n' for p in charts)
    for name, response in outputs.items():
        text += '\n## ' + name + '\n\n```json\n' + json.dumps(response, ensure_ascii=False, indent=2) + '\n```\n'
    (folder / (target + '.md')).write_text(text)
    score = {'target_date': target, 'information_cutoff': result['information_cutoff'], **{name: evidence['agent_scores'][stage] for name, stage in SCORES.items()}, 'decision_supervisor': result['response']['anomaly_score']}
    path = destination / 'scores.csv'
    frame = pd.read_csv(path) if path.exists() else pd.DataFrame()
    pd.concat([frame, pd.DataFrame([score])], ignore_index=True).to_csv(path, index=False)


def evaluate(destination, labels_path):
    destination = Path(destination)
    scores = pd.read_csv(destination / 'scores.csv').sort_values('target_date').reset_index(drop=True)
    labels = pd.read_csv(labels_path).rename(columns={'date': 'information_cutoff'})
    scores = scores.merge(labels, on='information_cutoff', how='left', validate='one_to_one')
    if scores[['crash_label', 'min_future_logret', 'crash_threshold']].isna().any().any():
        raise ValueError('Evaluation outcomes are incomplete')
    metrics = []
    y = scores.crash_label.astype(int).to_numpy()
    for agent in [*SCORES, 'decision_supervisor']:
        s = scores[agent].to_numpy()
        threshold = 0.5
        thresholds = []
        for i in range(len(scores)):
            eligible = (np.arange(len(scores)) < i) & (scores.target_date.to_numpy() <= scores.information_cutoff.iloc[i])
            if y[eligible].sum() > 0:
                candidates = np.unique(np.r_[0., 1., 0.5, threshold, s[eligible]])
                alerts = s[eligible, None] >= candidates[None, :]
                true_positives = (alerts & (y[eligible, None] == 1)).sum(axis=0)
                denominator = alerts.sum(axis=0) + y[eligible].sum()
                values = np.divide(2 * true_positives, denominator, out=np.zeros(len(candidates)), where=denominator != 0)
                best = candidates[np.isclose(values, max(values), rtol=0, atol=1e-12)]
                threshold = min(best, key=lambda t: (abs(t - threshold), -t))
            thresholds.append(threshold)
        predicted = s >= np.array(thresholds)
        scores[agent + '_threshold'] = thresholds
        scores[agent + '_alert'] = predicted.astype(int)
        tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[0, 1]).ravel()
        metrics.append({'agent': agent, 'n': len(y), 'events': int(y.sum()), 'tn': int(tn), 'fp': int(fp), 'fn': int(fn), 'tp': int(tp), 'accuracy': accuracy_score(y, predicted), 'roc_auc': roc_auc_score(y, s) if len(set(y)) == 2 else None, 'average_precision': average_precision_score(y, s) if y.sum() else None, 'f1': f1_score(y, predicted, zero_division=0), 'precision': precision_score(y, predicted, zero_division=0), 'recall': recall_score(y, predicted, zero_division=0), 'mcc': matthews_corrcoef(y, predicted)})
    metrics = pd.DataFrame(metrics)
    scores.to_csv(destination / 'evaluation.csv', index=False)
    metrics.to_csv(destination / 'metrics.csv', index=False)
    return scores, metrics
