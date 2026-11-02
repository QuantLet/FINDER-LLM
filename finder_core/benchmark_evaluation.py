import numpy as np
from scipy.stats import rankdata


def ranking_draws(labels, probabilities, indices):
    """ROC AUC and AP with exact tie handling for paired resampled rows."""
    y = np.asarray(labels)[indices]
    p = np.asarray(probabilities)[indices]
    positives = y.sum(axis=1)
    ranks = rankdata(p, axis=1)
    auc = ((ranks * y).sum(axis=1) - positives * (positives + 1) / 2) / (positives * (y.shape[1] - positives))
    order = np.argsort(-p, axis=1, kind='stable')
    sorted_p = np.take_along_axis(p, order, axis=1)
    sorted_y = np.take_along_axis(y, order, axis=1)
    cumulative = sorted_y.cumsum(axis=1)
    endpoints = np.concatenate([sorted_p[:, :-1] != sorted_p[:, 1:], np.ones((len(y), 1), dtype=bool)], axis=1)
    previous = np.maximum.accumulate(np.where(endpoints, cumulative, 0), axis=1)
    previous = np.concatenate([np.zeros((len(y), 1)), previous[:, :-1]], axis=1)
    ap = ((cumulative - previous) * cumulative / np.arange(1, y.shape[1] + 1) * endpoints).sum(axis=1) / positives
    return np.column_stack([auc, ap])


def moving_block_intervals(frame, models, n_bootstrap=10000, block_length=21, random_seed=42):
    """Non-circular moving blocks; reject single-class draws, capped at 20B attempts."""
    rng = np.random.default_rng(random_seed)
    y = frame.actual_label.to_numpy(dtype=int)
    n = len(y)
    if not 1 <= block_length <= n or np.unique(y).size != 2:
        raise ValueError('Invalid block length or labels')
    accepted, attempts = [], 0
    while sum(len(x) for x in accepted) < n_bootstrap and attempts < 20 * n_bootstrap:
        count = min(200, 20 * n_bootstrap - attempts)
        starts = rng.integers(n - block_length + 1, size=(count, int(np.ceil(n / block_length))))
        indices = (starts[:, :, None] + np.arange(block_length)).reshape(count, -1)[:, :n]
        attempts += count
        positives = y[indices].sum(axis=1)
        accepted.append(indices[(positives > 0) & (positives < n)])
    indices = np.concatenate(accepted)[:n_bootstrap]
    if len(indices) != n_bootstrap:
        raise RuntimeError('Insufficient valid bootstrap replicates')
    draws = np.stack([np.concatenate([ranking_draws(y, frame[model].to_numpy(), batch) for batch in np.array_split(indices, 50)]) for model in models], axis=1)
    return draws
