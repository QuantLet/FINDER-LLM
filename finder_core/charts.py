from __future__ import annotations
import hashlib
import re
from pathlib import Path
from typing import Any, Sequence
import fcntl
import json
import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

CHART_EXPERT = "chart_expert"
CHART_PACKET_SPEC: tuple[dict[str, Any], ...] = (
    {"source_chart": 1, "panels": ["price_and_long_term_regime", "money_flow_and_normalized_range"], "visible_fields": ["open", "high", "low", "close", "bb_lower", "bb_upper", "sma200", "cmf_20d", "atr_14d_norm"]},
    {"source_chart": 2, "panels": ["momentum", "trend_strength"], "visible_fields": ["macd_hist_12_26_9_norm", "rsi_14d", "stoch_k", "adx"]},
    {"source_chart": 3, "panels": ["volatility_and_tail_risk", "distribution_shape", "drawdown_and_ulcer"], "visible_fields": ["hv_10d_ann", "hv_126d_ann", "log_return_1d_q05_roll126d", "es_5pct_126", "skewness_126", "kurtosis_126", "dd_20", "ulcer_index"]},
)


class ChartPacketError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _chart_number(path: Path) -> int:
    matches = re.findall(r"chart[ _-]?(\d+)", path.name.lower()) or re.findall(r"chart[ _-]?(\d+)", path.parent.name.lower())
    if not matches:
        raise ChartPacketError(f"Cannot identify chart number from {path}.")
    return int(matches[-1])


def _source_map(chart_paths: Sequence[Path]) -> dict[int, Path]:
    mapping: dict[int, Path] = {}
    for raw_path in chart_paths:
        path = Path(raw_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ChartPacketError(f"Missing or empty source chart: {path}")
        number = _chart_number(path)
        if number in mapping:
            raise ChartPacketError(f"Duplicate source Chart {number}.")
        mapping[number] = path
    if set(mapping) != {1, 2, 3}:
        raise ChartPacketError(f"Expected source Charts 1..3; received {sorted(mapping)}.")
    return mapping


def prepare_chart_packet(chart_paths: Sequence[Path]) -> tuple[tuple[Path, ...], dict[str, Any]]:


    sources = _source_map(chart_paths)
    packet = tuple(sources[number] for number in (1, 2, 3))
    records = []
    for spec in CHART_PACKET_SPEC:
        chart_number = int(spec["source_chart"])
        source = sources[chart_number]
        records.append({"source_chart": chart_number, "source_file": source.name, "source_sha256": _sha256(source), "image_scope": "complete_source_chart", "panels": list(spec["panels"]), "visible_fields": list(spec["visible_fields"])})
    return packet, {"source_chart_count": 3, "packet_type": "whole_source_charts", "agent": CHART_EXPERT, "assigned_image_count": 3, "assigned_charts": records}


def _context_chart_record(frame, ticker, context_window_size):
    if isinstance(context_window_size, bool) or not isinstance(context_window_size, int) or not 2 <= context_window_size <= 126:
        raise ValueError("context_window_size must be an integer between 2 and 126.")
    fields = list(dict.fromkeys(field for item in CHART_PACKET_SPEC for field in item["visible_fields"]))
    window = frame.loc[:, fields].tail(context_window_size)
    if len(window) != context_window_size or window.index.has_duplicates or not window.index.is_monotonic_increasing:
        raise ValueError("A context chart requires the complete ordered display window.")
    if not np.isfinite(window.to_numpy(dtype=float)).all():
        raise ValueError("A context chart cannot contain missing or infinite values.")
    record = {"asset": str(ticker), "context_window_size": context_window_size, "start_date": window.index[0].date().isoformat(), "end_date": window.index[-1].date().isoformat(), "visible_fields": fields, "input_sha256": hashlib.sha256(window.to_csv(float_format="%.17g").encode()).hexdigest(), "renderer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    record["fingerprint"] = hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return window, record


def context_chart_paths(frame, ticker, figures_root, context_window_size=21):
    _, record = _context_chart_record(frame, ticker, context_window_size)
    folder = Path(figures_root) / f"Context{context_window_size}" / record["fingerprint"]
    return tuple(folder / f"Chart {number}" / f"{ticker}_{record['end_date']}_chart{number}.png" for number in (1, 2, 3))


def ensure_context_charts(frame, ticker, figures_root, context_window_size=21):
    window, record = _context_chart_record(frame, ticker, context_window_size)
    paths = context_chart_paths(frame, ticker, figures_root, context_window_size)
    folder = paths[0].parent.parent
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "context_chart_manifest.json"
    with (folder / ".render.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if manifest_path.is_file():
            saved = json.loads(manifest_path.read_text())
            if any(saved.get(key) != value for key, value in record.items()):
                raise ValueError("The frozen context-chart input manifest has changed.")
            for path, expected in zip(paths, saved["charts"]):
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
                    raise ValueError("A frozen context chart is missing or changed; restore it before resuming.")
            return paths
        rendered = create_technical_chart(window, ticker, record["end_date"], folder)
        record["charts"] = [{"path": str(path.relative_to(folder)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in rendered]
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        temporary.replace(manifest_path)
    return paths


def candlestick(axis, x_values, open_values, high_values, low_values, close_values, width=0.6):
    x_values = np.asarray(x_values)
    open_values = np.asarray(open_values, dtype=float)
    high_values = np.asarray(high_values, dtype=float)
    low_values = np.asarray(low_values, dtype=float)
    close_values = np.asarray(close_values, dtype=float)
    upward = close_values >= open_values
    body_bottom = np.minimum(open_values, close_values)
    body_height = np.maximum(np.abs(close_values - open_values), 0.02)
    for mask, color in ((upward, "green"), (~upward, "red")):
        if mask.any():
            axis.vlines(x_values[mask], low_values[mask], high_values[mask], color=color, lw=1.5, zorder=3)
            axis.bar(x_values[mask], body_height[mask], bottom=body_bottom[mask], width=width, color=color, edgecolor=color, lw=0.5, zorder=3)
    axis.plot([], [], color="green", lw=6, label="Up Day")
    axis.plot([], [], color="red", lw=6, label="Down Day")


def _format_panel(axis, x_values, ticks, tick_labels, ylabel):
    axis.set_ylabel(ylabel, color="black", fontsize=20, fontweight="bold")
    axis.yaxis.get_offset_text().set_fontsize(20)
    axis.yaxis.get_offset_text().set_fontweight("bold")
    axis.tick_params(axis="y", labelsize=20, width=1.5)
    for label in axis.get_yticklabels():
        label.set_fontweight("bold")
    axis.set_xlabel("Date", fontsize=20, fontweight="bold")
    axis.set_xticks(ticks)
    axis.set_xticklabels(tick_labels, rotation=0, ha="center")
    axis.tick_params(axis="x", labelsize=20, width=1.5, length=6, pad=12)
    for label in axis.get_xticklabels():
        label.set_fontweight("bold")
    axis.set_xlim([x_values[0] - pd.Timedelta(days=2), x_values[-1] + pd.Timedelta(days=2)])
    axis.grid(True, linestyle="--", alpha=0.3)


def _format_legend(axis, location="upper left"):
    handles, labels = axis.get_legend_handles_labels()
    if handles:
        axis.legend(handles, labels, loc=location, fontsize=18, prop={"weight": "bold", "size": 18}, framealpha=0.8)
        axis.get_legend().get_frame().set_edgecolor("black")
        axis.get_legend().get_frame().set_linewidth(2)


def plot_panel_1(axis, window, x_values, ticks, tick_labels):
    base_value = window["close"].iloc[0]
    open_values = window["open"].values / base_value * 100
    high_values = window["high"].values / base_value * 100
    low_values = window["low"].values / base_value * 100
    close_values = window["close"].values / base_value * 100
    axis.plot(x_values, window["bb_lower"].values / base_value * 100, label="BB Lower", color="red", lw=4, linestyle=":")
    axis.plot(x_values, window["bb_upper"].values / base_value * 100, label="BB Upper", color="green", lw=4, linestyle=":")
    axis.fill_between(x_values, window["bb_upper"].values / base_value * 100, window["bb_lower"].values / base_value * 100, alpha=0.1, color="red", label="BB Band")
    candlestick(axis, x_values, open_values, high_values, low_values, close_values)
    axis.plot(x_values, window["sma200"].values / base_value * 100, label="SMA200", lw=4, linestyle="--", color="purple")
    axis.axhline(100, color="black", linestyle="-", lw=1.5, alpha=0.25)
    _format_panel(axis, x_values, ticks, tick_labels, "Price (Start = 100)")
    _format_legend(axis)


def plot_panel_2(axis, window, x_values, ticks, tick_labels):
    axis.plot(x_values, window["cmf_20d"].values, label="CMF (20d)", color="blue", lw=3)
    axis.fill_between(x_values, 0, window["cmf_20d"].values, alpha=0.15, color="blue")
    axis.axhline(0, color="black", linestyle="-", lw=2, alpha=0.3)
    _format_panel(axis, x_values, ticks, tick_labels, "CMF")
    _format_legend(axis)
    secondary_axis = axis.twinx()
    secondary_axis.plot(x_values, window["atr_14d_norm"].values, label="Normalized ATR (14d)", color="red", lw=3)
    _format_panel(secondary_axis, x_values, ticks, tick_labels, "Normalized ATR")
    _format_legend(secondary_axis, "upper right")


def plot_panel_3(axis, window, x_values, ticks, tick_labels):
    colors = ["green" if value >= 0 else "red" for value in window["macd_hist_12_26_9_norm"]]
    axis.bar(x_values, window["macd_hist_12_26_9_norm"].values, alpha=0.3, label="Normalized MACD Hist (12, 26, 9)", color=colors)
    axis.axhline(0, color="black", linestyle="-", lw=1.5, alpha=0.2)
    _format_panel(axis, x_values, ticks, tick_labels, "MACD Histogram")
    _format_legend(axis)
    secondary_axis = axis.twinx()
    secondary_axis.plot(x_values, window["rsi_14d"].values, label="RSI (14d)", color="purple", lw=3)
    secondary_axis.plot(x_values, [70] * len(x_values), color="red", linestyle="--", lw=3, alpha=0.5, label="Overbought (70)")
    secondary_axis.plot(x_values, [30] * len(x_values), color="green", linestyle="--", lw=3, alpha=0.5, label="Oversold (30)")
    secondary_axis.set_ylim(-5, 105)
    _format_panel(secondary_axis, x_values, ticks, tick_labels, "RSI")
    _format_legend(secondary_axis, "upper right")


def plot_panel_4(axis, window, x_values, ticks, tick_labels):
    axis.plot(x_values, window["stoch_k"].values, label="Stochastic K", color="blue", lw=3)
    axis.plot([x_values[0], x_values[-1]], [80, 80], color="red", linestyle="--", lw=3, alpha=0.6, label="Overbought (80)")
    axis.plot([x_values[0], x_values[-1]], [20, 20], color="green", linestyle="--", lw=3, alpha=0.6, label="Oversold (20)")
    axis.set_ylim(-5, 105)
    _format_panel(axis, x_values, ticks, tick_labels, "Stochastic K")
    _format_legend(axis)
    secondary_axis = axis.twinx()
    secondary_axis.plot(x_values, window["adx"].values, label="ADX", color="black", lw=3)
    secondary_axis.plot([x_values[0], x_values[-1]], [25, 25], color="grey", linestyle=":", lw=3, alpha=0.6, label="ADX Trending (25)")
    _format_panel(secondary_axis, x_values, ticks, tick_labels, "ADX")
    _format_legend(secondary_axis, "upper right")


def plot_panel_5(axis, window, x_values, ticks, tick_labels):
    axis.plot(x_values, window["hv_10d_ann"].values, label="HV (10d, annualized)", lw=3, color="blue")
    axis.plot(x_values, window["hv_126d_ann"].values, label="HV (126d, annualized)", lw=3, color="black")
    _format_panel(axis, x_values, ticks, tick_labels, "Annualized Volatility")
    _format_legend(axis)
    secondary_axis = axis.twinx()
    secondary_axis.plot(x_values, window["log_return_1d_q05_roll126d"].values, label="5% VaR (126d)", color="orange", lw=3)
    secondary_axis.plot(x_values, window["es_5pct_126"].values, label="5% ES (126d)", color="red", lw=3, linestyle="--")
    secondary_axis.axhline(0, color="black", linestyle="-", lw=1.5, alpha=0.3)
    _format_panel(secondary_axis, x_values, ticks, tick_labels, "Tail Risk (Log Return)")
    _format_legend(secondary_axis, "upper right")


def plot_panel_6(axis, window, x_values, ticks, tick_labels):
    axis.plot(x_values, window["skewness_126"].values, label="Skewness (126d)", color="black", lw=3)
    axis.axhline(0, color="grey", linestyle="--", lw=3, alpha=0.5, label="Skewness = 0")
    _format_panel(axis, x_values, ticks, tick_labels, "Skewness")
    _format_legend(axis)
    secondary_axis = axis.twinx()
    secondary_axis.plot(x_values, window["kurtosis_126"].values, label="Excess Kurtosis (126d)", color="orange", lw=3, linestyle="--")
    secondary_axis.axhline(0, color="lightcoral", linestyle="-", lw=3, alpha=0.5, label="Excess Kurtosis = 0")
    _format_panel(secondary_axis, x_values, ticks, tick_labels, "Excess Kurtosis")
    _format_legend(secondary_axis, "upper right")


def plot_panel_7(axis, window, x_values, ticks, tick_labels):
    axis.plot(x_values, window["dd_20"].values, label="Drawdown (20d)", lw=3, color="red")
    axis.plot(x_values, window["ulcer_index"].values, label="Ulcer Index", lw=3, color="black", linestyle="--")
    axis.axhline(0, color="black", linestyle="-", lw=2, alpha=0.3)
    _format_panel(axis, x_values, ticks, tick_labels, "Drawdown / Pain (%)")
    _format_legend(axis)


def create_chart(window, ticker, date_text, panels, chart_index, save_figure_dir, transparent=False):
    figure = Figure(figsize=(40, 7 * len(panels)))
    FigureCanvasAgg(figure)
    axes = figure.subplots(len(panels), 1, sharex=False)
    axes = axes if isinstance(axes, np.ndarray) else [axes]
    x_values = window.index
    ticks = window.index[::max(1, len(window) // 10)]
    tick_labels = [date.strftime("%Y-%m-%d") for date in ticks]
    for index, panel_function in enumerate(panels):
        panel_function(axes[index], window, x_values, ticks, tick_labels)
    chart_folder = Path(save_figure_dir) / f"Chart {chart_index}"
    chart_folder.mkdir(parents=True, exist_ok=True)
    figure_path = chart_folder / f"{ticker}_{date_text}_chart{chart_index}.png"
    figure.suptitle(f"{ticker} - {date_text.replace('-', '')}", fontsize=28, fontweight="bold", y=0.995)
    figure.tight_layout(rect=[0, 0.03, 1, 0.95])
    figure.savefig(figure_path, transparent=transparent, dpi=144)
    figure.clear()
    return figure_path


def create_technical_chart(window, ticker, date_text, save_figure_dir):
    panel_requirements = {
        plot_panel_1: ["open", "high", "low", "close", "sma200", "bb_lower", "bb_upper"],
        plot_panel_2: ["cmf_20d", "atr_14d_norm"],
        plot_panel_3: ["macd_hist_12_26_9_norm", "rsi_14d"],
        plot_panel_4: ["stoch_k", "adx"],
        plot_panel_5: ["hv_10d_ann", "hv_126d_ann", "log_return_1d_q05_roll126d", "es_5pct_126"],
        plot_panel_6: ["skewness_126", "kurtosis_126"],
        plot_panel_7: ["dd_20", "ulcer_index"],
    }
    panel_functions = [plot_panel_1, plot_panel_2, plot_panel_3, plot_panel_4, plot_panel_5, plot_panel_6, plot_panel_7]
    missing = {f"panel {index + 1}": [column for column in panel_requirements[panel] if column not in window.columns] for index, panel in enumerate(panel_functions)}
    missing = {panel: columns for panel, columns in missing.items() if columns}
    if missing:
        raise KeyError(f"Missing columns required by the 7 panels: {missing}")
    panels_per_chart = [2, 2, 3]
    if sum(panels_per_chart) != len(panel_functions):
        raise ValueError("Panel layout must account for all chart panels.")
    charts = []
    start = 0
    for chart_index, panel_count in enumerate(panels_per_chart, start=1):
        end = start + panel_count
        charts.append(create_chart(window, ticker, date_text, panel_functions[start:end], chart_index, save_figure_dir))
        start = end
    return tuple(charts)
