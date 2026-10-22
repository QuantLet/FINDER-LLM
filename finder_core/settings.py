"""Load the small JSON settings files used by the notebooks."""

from copy import deepcopy
import json
from pathlib import Path

from .markets import bind_market


def load_settings(project_root=".", *, ticker=None):
    root = Path(project_root).resolve() / "config"
    if ticker is None:
        ticker = json.loads((root / "settings.json").read_text(encoding="utf-8"))["ticker"]
    saved = json.loads((root / "markets.json").read_text(encoding="utf-8"))
    if ticker not in saved["markets"]:
        raise ValueError(f"Unknown market: {ticker}.")
    values = {**deepcopy(saved["defaults"]), **deepcopy(saved["markets"][ticker]), "market_registry": deepcopy(saved["market_registry"])}
    return bind_market(values, ticker)
