from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd


WIDTH = 1080
HEIGHT = 340
MARGIN_LEFT = 68
MARGIN_RIGHT = 24
MARGIN_TOP = 22
MARGIN_BOTTOM = 44
PLOT_WIDTH = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
PLOT_HEIGHT = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM


ETF_LABELS = {
    "513100": "国泰纳指ETF",
    "159915": "易方达创业板ETF",
}


TRADE_CHART_SCRIPT = r"""
(() => {
  const datasets = JSON.parse(document.getElementById('trade-chart-data').textContent);
  const colors = () => {
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    return dark
      ? { text:'#eef1f4', muted:'#aab2bc', grid:'#303741', up:'#ed6a6a', down:'#49b882', buy:'#ff7078', sell:'#49b882', cross:'#aab2bc' }
      : { text:'#18202a', muted:'#66717e', grid:'#dfe3e8', up:'#c43d3d', down:'#18875b', buy:'#d33f49', sell:'#18794e', cross:'#66717e' };
  };
  const number = value => Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 });

  document.querySelectorAll('.trade-chart-panel').forEach(panel => {
    const dataset = datasets.find(item => item.id === panel.dataset.chartId);
    const canvas = panel.querySelector('canvas');
    const wrap = panel.querySelector('.trade-canvas-wrap');
    const tooltip = panel.querySelector('.trade-tooltip');
    let years = 3;
    let visible = [];
    let hoverIndex = null;

    const filteredBars = () => {
      if (years === 'all') return dataset.bars;
      const latest = new Date(dataset.bars[dataset.bars.length - 1][0] + 'T00:00:00');
      const cutoff = new Date(latest);
      cutoff.setFullYear(cutoff.getFullYear() - Number(years));
      return dataset.bars.filter(bar => new Date(bar[0] + 'T00:00:00') >= cutoff);
    };

    const draw = () => {
      visible = filteredBars();
      const rect = wrap.getBoundingClientRect();
      const width = Math.max(320, rect.width);
      const height = 420;
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      const ctx = canvas.getContext('2d');
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      const palette = colors();
      const margin = { left:58, right:18, top:24, bottom:38 };
      const plotWidth = width - margin.left - margin.right;
      const plotHeight = height - margin.top - margin.bottom;
      const lows = visible.map(bar => bar[3]);
      const highs = visible.map(bar => bar[2]);
      let low = Math.min(...lows);
      let high = Math.max(...highs);
      const padding = Math.max((high - low) * 0.08, high * 0.01);
      low -= padding;
      high += padding;
      const xAt = index => margin.left + (index + 0.5) / visible.length * plotWidth;
      const yAt = value => margin.top + (high - value) / (high - low) * plotHeight;

      ctx.font = '12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif';
      ctx.textBaseline = 'middle';
      for (let index = 0; index < 5; index += 1) {
        const value = low + (high - low) * index / 4;
        const y = yAt(value);
        ctx.strokeStyle = palette.grid;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(width - margin.right, y); ctx.stroke();
        ctx.fillStyle = palette.muted;
        ctx.textAlign = 'right';
        ctx.fillText(value.toFixed(value < 10 ? 3 : 2), margin.left - 8, y);
      }

      const tickCount = Math.min(6, visible.length);
      for (let index = 0; index < tickCount; index += 1) {
        const barIndex = Math.round(index * (visible.length - 1) / Math.max(tickCount - 1, 1));
        ctx.fillStyle = palette.muted;
        ctx.textAlign = index === 0 ? 'left' : index === tickCount - 1 ? 'right' : 'center';
        ctx.fillText(visible[barIndex][0].slice(0, 7), xAt(barIndex), height - 16);
      }

      const candleWidth = Math.max(1, Math.min(9, plotWidth / visible.length * 0.68));
      visible.forEach((bar, index) => {
        const [, open, barHigh, barLow, close] = bar;
        const x = xAt(index);
        const color = close >= open ? palette.up : palette.down;
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, yAt(barHigh)); ctx.lineTo(x, yAt(barLow)); ctx.stroke();
        const bodyTop = Math.min(yAt(open), yAt(close));
        const bodyHeight = Math.max(1, Math.abs(yAt(open) - yAt(close)));
        ctx.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
      });

      const indexByDate = new Map(visible.map((bar, index) => [bar[0], index]));
      dataset.trades.forEach(trade => {
        const index = indexByDate.get(trade.date);
        if (index === undefined) return;
        const bar = visible[index];
        const buy = trade.side === 'BUY';
        const x = xAt(index);
        const y = buy ? yAt(bar[3]) + 12 : yAt(bar[2]) - 12;
        ctx.fillStyle = buy ? palette.buy : palette.sell;
        ctx.beginPath();
        if (buy) {
          ctx.moveTo(x, y - 6); ctx.lineTo(x - 6, y + 5); ctx.lineTo(x + 6, y + 5);
        } else {
          ctx.moveTo(x, y + 6); ctx.lineTo(x - 6, y - 5); ctx.lineTo(x + 6, y - 5);
        }
        ctx.closePath(); ctx.fill();
        if (visible.length < 800) {
          ctx.font = 'bold 10px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif';
          ctx.textAlign = 'center';
          ctx.fillText(buy ? '买' : '卖', x, buy ? y + 14 : y - 14);
        }
      });

      if (hoverIndex !== null && visible[hoverIndex]) {
        const x = xAt(hoverIndex);
        ctx.strokeStyle = palette.cross;
        ctx.setLineDash([4, 4]);
        ctx.beginPath(); ctx.moveTo(x, margin.top); ctx.lineTo(x, height - margin.bottom); ctx.stroke();
        ctx.setLineDash([]);
      }
      panel._chartGeometry = { margin, plotWidth, width };
    };

    panel.querySelectorAll('.range-button').forEach(button => {
      button.addEventListener('click', () => {
        years = button.dataset.years === 'all' ? 'all' : Number(button.dataset.years);
        panel.querySelectorAll('.range-button').forEach(item => item.classList.toggle('active', item === button));
        hoverIndex = null;
        tooltip.hidden = true;
        draw();
      });
    });

    canvas.addEventListener('mousemove', event => {
      const geometry = panel._chartGeometry;
      if (!geometry || !visible.length) return;
      const bounds = canvas.getBoundingClientRect();
      const x = event.clientX - bounds.left;
      hoverIndex = Math.max(0, Math.min(visible.length - 1,
        Math.floor((x - geometry.margin.left) / geometry.plotWidth * visible.length)));
      const bar = visible[hoverIndex];
      const dayTrades = dataset.trades.filter(trade => trade.date === bar[0]);
      const tradeLines = dayTrades.map(trade =>
        `<div class="tooltip-trade ${trade.side === 'BUY' ? 'buy-text' : 'sell-text'}">${trade.side === 'BUY' ? '买入' : '卖出'} ${dataset.code} ${dataset.name}<br>${dataset.strategy}<br>成交价 ${number(trade.price)} · 数量 ${number(trade.quantity)}<br>成交额 ${number(trade.notional)} · 成本 ${number(trade.cost)} · 信号日 ${trade.signal_date}</div>`
      ).join('');
      tooltip.innerHTML = `<strong>${bar[0]}</strong><div>开 ${number(bar[1])}　高 ${number(bar[2])}　低 ${number(bar[3])}　收 ${number(bar[4])}</div>${tradeLines}`;
      tooltip.hidden = false;
      const tooltipX = Math.min(Math.max(event.clientX - bounds.left + 14, 8), bounds.width - 280);
      tooltip.style.left = `${tooltipX}px`;
      tooltip.style.top = `${Math.max(8, event.clientY - bounds.top - 28)}px`;
      draw();
    });
    canvas.addEventListener('mouseleave', () => { hoverIndex = null; tooltip.hidden = true; draw(); });
    new ResizeObserver(draw).observe(wrap);
    draw();
  });
})();
"""


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _number(value: float) -> str:
    return f"{value:,.2f}"


def _trade_chart_payload(
    nav: pd.DataFrame,
    trades: pd.DataFrame,
    code: str,
    chart_id: str,
    strategy: str,
) -> dict[str, Any]:
    bar_columns = [f"{field}_{code}" for field in ("open", "high", "low", "close")]
    missing = [column for column in bar_columns if column not in nav]
    if missing:
        raise ValueError(f"Missing OHLC columns for trade chart: {', '.join(missing)}")
    bars = [
        [
            pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d"),
            round(float(row[f"open_{code}"]), 6),
            round(float(row[f"high_{code}"]), 6),
            round(float(row[f"low_{code}"]), 6),
            round(float(row[f"close_{code}"]), 6),
        ]
        for _, row in nav.iterrows()
    ]
    selected = trades[trades["fund_code"].astype(str) == code] if not trades.empty else trades
    trade_rows = [
        {
            "date": pd.Timestamp(row["trade_date"]).strftime("%Y-%m-%d"),
            "signal_date": pd.Timestamp(row["signal_date"]).strftime("%Y-%m-%d"),
            "side": str(row["side"]),
            "price": round(float(row["price"]), 6),
            "quantity": round(float(row["quantity"]), 4),
            "notional": round(float(row["notional"]), 2),
            "cost": round(float(row["cost"]), 2),
        }
        for _, row in selected.iterrows()
    ]
    return {
        "id": chart_id,
        "code": code,
        "name": ETF_LABELS[code],
        "strategy": strategy,
        "bars": bars,
        "trades": trade_rows,
    }


def _sample(frame: pd.DataFrame, max_points: int = 700) -> pd.DataFrame:
    if len(frame) <= max_points:
        return frame
    indices = np.linspace(0, len(frame) - 1, max_points, dtype=int)
    return frame.iloc[np.unique(indices)]


def _x_ticks(dates: pd.Series, count: int = 6) -> list[tuple[float, str]]:
    if dates.empty:
        return []
    indices = np.unique(np.linspace(0, len(dates) - 1, min(count, len(dates)), dtype=int))
    denominator = max(len(dates) - 1, 1)
    return [
        (MARGIN_LEFT + index / denominator * PLOT_WIDTH, pd.Timestamp(dates.iloc[index]).strftime("%Y-%m"))
        for index in indices
    ]


def _line_chart(
    frame: pd.DataFrame,
    series: list[tuple[str, str, str]],
    value_formatter: Callable[[float], str],
    aria_label: str,
) -> str:
    data = _sample(frame.reset_index(drop=True))
    values = np.concatenate([pd.to_numeric(data[column], errors="coerce").dropna().to_numpy() for column, _, _ in series])
    y_min = min(float(values.min()), 0.0)
    y_max = max(float(values.max()), 0.0)
    if y_max == y_min:
        y_max = y_min + 1.0
    padding = (y_max - y_min) * 0.06
    y_min -= padding
    y_max += padding

    def point(index: int, value: float) -> tuple[float, float]:
        x = MARGIN_LEFT + index / max(len(data) - 1, 1) * PLOT_WIDTH
        y = MARGIN_TOP + (y_max - value) / (y_max - y_min) * PLOT_HEIGHT
        return x, y

    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{html.escape(aria_label)}">']
    for tick in np.linspace(y_min, y_max, 5):
        y = point(0, float(tick))[1]
        parts.append(f'<line class="grid" x1="{MARGIN_LEFT}" y1="{y:.2f}" x2="{WIDTH-MARGIN_RIGHT}" y2="{y:.2f}"/>')
        parts.append(f'<text class="axis-label" x="{MARGIN_LEFT-10}" y="{y+4:.2f}" text-anchor="end">{html.escape(value_formatter(float(tick)))}</text>')
    for x, label in _x_ticks(data["trade_date"]):
        parts.append(f'<text class="axis-label" x="{x:.2f}" y="{HEIGHT-14}" text-anchor="middle">{label}</text>')
    for column, label, css_class in series:
        points = [point(index, float(value)) for index, value in enumerate(data[column]) if pd.notna(value)]
        path = " ".join(("M" if idx == 0 else "L") + f"{x:.2f},{y:.2f}" for idx, (x, y) in enumerate(points))
        parts.append(f'<path class="plot-line {css_class}" d="{path}"><title>{html.escape(label)}</title></path>')
    parts.append("</svg>")
    return "".join(parts)


def _weight_chart(frame: pd.DataFrame) -> str:
    data = _sample(frame.reset_index(drop=True))
    columns = [
        ("weight_513100", "纳指ETF", "area-nasdaq"),
        ("weight_159915", "创业板ETF", "area-chinext"),
        ("cash_weight", "现金", "area-cash"),
    ]
    x_values = [MARGIN_LEFT + index / max(len(data) - 1, 1) * PLOT_WIDTH for index in range(len(data))]
    base = np.zeros(len(data))
    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="每日持仓比例堆叠图">']
    for tick in np.linspace(0, 1, 5):
        y = MARGIN_TOP + (1 - tick) * PLOT_HEIGHT
        parts.append(f'<line class="grid" x1="{MARGIN_LEFT}" y1="{y:.2f}" x2="{WIDTH-MARGIN_RIGHT}" y2="{y:.2f}"/>')
        parts.append(f'<text class="axis-label" x="{MARGIN_LEFT-10}" y="{y+4:.2f}" text-anchor="end">{tick:.0%}</text>')
    for column, label, css_class in columns:
        values = pd.to_numeric(data[column], errors="coerce").fillna(0).clip(0, 1).to_numpy()
        top = np.clip(base + values, 0, 1)
        upper = [(x, MARGIN_TOP + (1 - value) * PLOT_HEIGHT) for x, value in zip(x_values, top)]
        lower = [(x, MARGIN_TOP + (1 - value) * PLOT_HEIGHT) for x, value in zip(reversed(x_values), reversed(base))]
        polygon = upper + lower
        path = " ".join(("M" if idx == 0 else "L") + f"{x:.2f},{y:.2f}" for idx, (x, y) in enumerate(polygon)) + " Z"
        parts.append(f'<path class="plot-area {css_class}" d="{path}"><title>{html.escape(label)}</title></path>')
        base = top
    for x, label in _x_ticks(data["trade_date"]):
        parts.append(f'<text class="axis-label" x="{x:.2f}" y="{HEIGHT-14}" text-anchor="middle">{label}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _annual_returns(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    values = frame.set_index(pd.to_datetime(frame["trade_date"]))[columns]
    year_end = values.groupby(values.index.year).last()
    prior = year_end.shift(1)
    first_base = pd.Series({column: float(values[column].iloc[0]) for column in values}, name=year_end.index[0] - 1)
    prior.iloc[0] = first_base
    returns = year_end / prior - 1
    returns.index.name = "year"
    return returns.reset_index()


def _bar_chart(frame: pd.DataFrame, columns: list[tuple[str, str, str]]) -> str:
    annual = _annual_returns(frame, [column for column, _, _ in columns])
    values = annual[[column for column, _, _ in columns]].to_numpy(dtype=float)
    y_min = min(float(np.nanmin(values)), 0.0)
    y_max = max(float(np.nanmax(values)), 0.0)
    padding = max((y_max - y_min) * 0.08, 0.02)
    y_min -= padding
    y_max += padding

    def y_pos(value: float) -> float:
        return MARGIN_TOP + (y_max - value) / (y_max - y_min) * PLOT_HEIGHT

    group_width = PLOT_WIDTH / max(len(annual), 1)
    bar_width = min(group_width * 0.72 / len(columns), 18)
    zero_y = y_pos(0)
    parts = [f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="各策略与ETF的年度收益对比柱状图">']
    for tick in np.linspace(y_min, y_max, 5):
        y = y_pos(float(tick))
        parts.append(f'<line class="grid" x1="{MARGIN_LEFT}" y1="{y:.2f}" x2="{WIDTH-MARGIN_RIGHT}" y2="{y:.2f}"/>')
        parts.append(f'<text class="axis-label" x="{MARGIN_LEFT-10}" y="{y+4:.2f}" text-anchor="end">{tick:.0%}</text>')
    for group_index, row in annual.iterrows():
        center = MARGIN_LEFT + (group_index + 0.5) * group_width
        parts.append(f'<text class="axis-label" x="{center:.2f}" y="{HEIGHT-14}" text-anchor="middle">{int(row["year"])}</text>')
        for series_index, (column, label, css_class) in enumerate(columns):
            value = float(row[column])
            x = center + (series_index - (len(columns) - 1) / 2) * bar_width - bar_width * 0.42
            value_y = y_pos(value)
            y = min(zero_y, value_y)
            height = max(abs(value_y - zero_y), 1.0)
            parts.append(
                f'<rect class="plot-bar {css_class}" x="{x:.2f}" y="{y:.2f}" width="{bar_width*0.84:.2f}" height="{height:.2f}">'
                f'<title>{int(row["year"])} {html.escape(label)}: {value:.2%}</title></rect>'
            )
    parts.append("</svg>")
    return "".join(parts)


def write_html_report(
    result: Any,
    output_path: str | Path,
    comparisons: dict[str, Any] | None = None,
) -> None:
    nav = result.nav.copy()
    nav["trade_date"] = pd.to_datetime(nav["trade_date"])
    trades = result.trades.copy()
    nasdaq_only = (comparisons or {}).get("nasdaq_only")
    if nasdaq_only is not None:
        comparison_nav = nasdaq_only.nav[["trade_date", "nav"]].rename(columns={"nav": "nav_nasdaq_only"})
        comparison_nav["trade_date"] = pd.to_datetime(comparison_nav["trade_date"])
        nav = nav.merge(comparison_nav, on="trade_date", how="left", validate="one_to_one")

    value_columns = ["nav", "benchmark_513100", "benchmark_159915"]
    if nasdaq_only is not None:
        value_columns.insert(1, "nav_nasdaq_only")
    for column in value_columns:
        nav[f"return_{column}"] = nav[column] / nav[column].iloc[0] - 1
        nav[f"drawdown_{column}"] = nav[column] / nav[column].cummax() - 1

    summary = result.summary
    profile = summary.get("parameters", {}).get("profile", "custom")
    benchmark_513100 = summary["benchmarks"]["513100"]
    benchmark_159915 = summary["benchmarks"]["159915"]
    trade_payloads = [
        _trade_chart_payload(nav, trades, "513100", "rotation_513100", "二选一轮动策略"),
        _trade_chart_payload(nav, trades, "159915", "rotation_159915", "二选一轮动策略"),
    ]
    if nasdaq_only is not None:
        trade_payloads.append(
            _trade_chart_payload(
                nasdaq_only.nav,
                nasdaq_only.trades,
                "513100",
                "nasdaq_only",
                "仅纳指ETF择时策略",
            )
        )
    trade_chart_data = json.dumps(
        trade_payloads,
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    line_series = [
        ("return_nav", "二选一轮动", "line-strategy"),
    ]
    drawdown_series = [
        ("drawdown_nav", "二选一轮动", "line-strategy"),
    ]
    annual_series = [
        ("nav", "二选一轮动", "bar-strategy"),
    ]
    if nasdaq_only is not None:
        line_series.append(("return_nav_nasdaq_only", "仅纳指择时", "line-timing"))
        drawdown_series.append(("drawdown_nav_nasdaq_only", "仅纳指择时", "line-timing"))
        annual_series.append(("nav_nasdaq_only", "仅纳指择时", "bar-timing"))
    line_series.extend(
        [
            ("return_benchmark_513100", "纳指ETF买入持有", "line-nasdaq"),
            ("return_benchmark_159915", "创业板ETF买入持有", "line-chinext"),
        ]
    )
    drawdown_series.extend(
        [
            ("drawdown_benchmark_513100", "纳指ETF买入持有", "line-nasdaq"),
            ("drawdown_benchmark_159915", "创业板ETF买入持有", "line-chinext"),
        ]
    )
    annual_series.extend(
        [
            ("benchmark_513100", "纳指买入持有", "bar-nasdaq"),
            ("benchmark_159915", "创业板买入持有", "bar-chinext"),
        ]
    )
    performance_chart = _line_chart(
        nav,
        line_series,
        _pct,
        "轮动、仅纳指择时及两只ETF买入持有的累计收益曲线",
    )
    drawdown_chart = _line_chart(
        nav,
        drawdown_series,
        _pct,
        "轮动、仅纳指择时及两只ETF买入持有的历史回撤曲线",
    )
    timing_legend = '<span><i class="swatch timing"></i>仅纳指择时</span>' if nasdaq_only else ""
    legend = f"""
      <div class="legend" aria-label="图例">
        <span><i class="swatch strategy"></i>二选一轮动</span>
        {timing_legend}
        <span><i class="swatch nasdaq"></i>纳指买入持有</span>
        <span><i class="swatch chinext"></i>创业板买入持有</span>
        <span><i class="swatch cash"></i>现金</span>
      </div>
    """

    def trade_panel(chart_id: str, code: str, title: str, chart_trades: pd.DataFrame) -> str:
        trade_count = int((chart_trades["fund_code"].astype(str) == code).sum()) if not chart_trades.empty else 0
        return f"""
        <div class="trade-chart-panel" data-chart-id="{html.escape(chart_id)}" data-code="{html.escape(code)}">
          <div class="trade-chart-toolbar"><div><div class="trade-chart-title">{html.escape(title)}</div><div class="trade-chart-meta">{trade_count} 笔成交</div></div>
            <div class="range-control" aria-label="{html.escape(title)} K线时间范围"><button class="range-button" data-years="1">1年</button><button class="range-button active" data-years="3">3年</button><button class="range-button" data-years="5">5年</button><button class="range-button" data-years="all">全部</button></div></div>
          <div class="trade-canvas-wrap"><canvas class="trade-canvas" aria-label="{html.escape(title)}成交标记K线图"></canvas><div class="trade-tooltip" hidden></div></div>
        </div>
        """

    trade_panels = trade_panel("rotation_513100", "513100", "二选一轮动 · 513100 国泰纳指ETF", trades)
    trade_panels += trade_panel("rotation_159915", "159915", "二选一轮动 · 159915 易方达创业板ETF", trades)
    if nasdaq_only is not None:
        trade_panels += trade_panel("nasdaq_only", "513100", "仅纳指择时 · 513100 国泰纳指ETF", nasdaq_only.trades)

    def comparison_row(label: str, stats: dict[str, Any], trade_count: str = "-") -> str:
        return (
            f"<tr><td>{html.escape(label)}</td><td>{_pct(stats['total_return'])}</td>"
            f"<td>{_pct(stats['annual_return'])}</td><td>{_pct(stats['max_drawdown'])}</td>"
            f"<td>{_pct(stats['annual_volatility'])}</td><td>{stats['sharpe_zero_rate']:.2f}</td>"
            f"<td>{trade_count}</td></tr>"
        )

    comparison_rows = comparison_row("二选一轮动策略", summary, str(summary["trade_count"]))
    if nasdaq_only is not None:
        comparison_rows += comparison_row(
            "仅纳指ETF择时",
            nasdaq_only.summary,
            str(nasdaq_only.summary["trade_count"]),
        )
    comparison_rows += comparison_row("513100买入持有（不计成本）", benchmark_513100)
    comparison_rows += comparison_row("159915买入持有（不计成本）", benchmark_159915)

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ETF轮动回测报告</title>
  <style>
    :root {{ color-scheme: light dark; --bg:#f5f6f8; --surface:#ffffff; --text:#18202a; --muted:#66717e; --border:#d9dee5; --grid:#dfe3e8; --strategy:#18794e; --timing:#8f3f68; --nasdaq:#2563a6; --chinext:#c46a16; --cash:#9aa3ad; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --surface:#191e24; --text:#eef1f4; --muted:#aab2bc; --border:#343b44; --grid:#303741; --strategy:#49b882; --timing:#d68db2; --nasdaq:#69a7e3; --chinext:#e9a35c; --cash:#747e89; }} }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; letter-spacing:0; }}
    main {{ width:min(1180px, calc(100% - 32px)); margin:0 auto; padding:28px 0 44px; }}
    header {{ display:flex; justify-content:space-between; align-items:end; gap:20px; margin-bottom:22px; }}
    h1 {{ margin:0; font-size:24px; font-weight:500; }} h2 {{ margin:0 0 4px; font-size:17px; font-weight:500; }}
    p {{ margin:0; color:var(--muted); }}
    .metrics {{ display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:10px; margin-bottom:28px; }}
    .metric {{ padding:14px 15px; background:var(--surface); border:1px solid var(--border); border-radius:6px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; }} .metric strong {{ display:block; margin-top:5px; font-size:20px; font-weight:500; }}
    section {{ margin-top:28px; }} .section-head {{ display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:8px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:14px; color:var(--muted); font-size:12px; }} .legend span {{ display:inline-flex; align-items:center; gap:5px; }}
    .swatch {{ width:12px; height:3px; display:inline-block; }} .strategy {{ background:var(--strategy); }} .timing {{ background:var(--timing); }} .nasdaq {{ background:var(--nasdaq); }} .chinext {{ background:var(--chinext); }} .cash {{ background:var(--cash); }}
    .chart {{ display:block; width:100%; height:auto; overflow:visible; }} .grid {{ stroke:var(--grid); stroke-width:1; }} .axis-label {{ fill:var(--muted); font-size:12px; }}
    .plot-line {{ fill:none; stroke-width:2.2; vector-effect:non-scaling-stroke; }} .line-strategy {{ stroke:var(--strategy); }} .line-timing {{ stroke:var(--timing); stroke-dasharray:7 4; }} .line-nasdaq {{ stroke:var(--nasdaq); }} .line-chinext {{ stroke:var(--chinext); }}
    .plot-area {{ stroke:none; opacity:.76; }} .area-nasdaq {{ fill:var(--nasdaq); }} .area-chinext {{ fill:var(--chinext); }} .area-cash {{ fill:var(--cash); }}
    .plot-bar {{ opacity:.88; }} .bar-strategy {{ fill:var(--strategy); }} .bar-timing {{ fill:var(--timing); }} .bar-nasdaq {{ fill:var(--nasdaq); }} .bar-chinext {{ fill:var(--chinext); }}
    .trade-chart-panel {{ margin-top:18px; }} .trade-chart-toolbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:8px; }}
    .trade-chart-title {{ font-size:14px; font-weight:500; }} .trade-chart-meta {{ color:var(--muted); font-size:12px; }}
    .range-control {{ display:inline-flex; border:1px solid var(--border); border-radius:6px; overflow:hidden; flex:none; }}
    .range-button {{ min-width:44px; height:30px; padding:0 10px; border:0; border-right:1px solid var(--border); background:var(--surface); color:var(--muted); cursor:pointer; }}
    .range-button:last-child {{ border-right:0; }} .range-button.active {{ background:var(--text); color:var(--surface); }}
    .trade-canvas-wrap {{ position:relative; width:100%; min-height:420px; border:1px solid var(--border); background:var(--surface); border-radius:6px; overflow:hidden; }}
    .trade-canvas {{ display:block; width:100%; height:420px; }}
    .trade-tooltip {{ position:absolute; z-index:2; width:272px; padding:9px 10px; border:1px solid var(--border); border-radius:6px; background:var(--surface); color:var(--text); box-shadow:0 5px 18px rgba(0,0,0,.16); font-size:12px; line-height:1.5; pointer-events:none; }}
    .trade-tooltip strong {{ display:block; margin-bottom:2px; }} .tooltip-trade {{ margin-top:7px; padding-top:7px; border-top:1px solid var(--border); }}
    .buy-text {{ color:#c43d3d; }} .sell-text {{ color:#18794e; }}
    @media (prefers-color-scheme: dark) {{ .buy-text {{ color:#ed6a6a; }} .sell-text {{ color:#49b882; }} }}
    .trade-legend {{ display:flex; gap:14px; color:var(--muted); font-size:12px; }} .trade-legend span {{ display:inline-flex; align-items:center; gap:5px; }}
    .trade-marker {{ width:0; height:0; border-left:5px solid transparent; border-right:5px solid transparent; }} .trade-marker.buy {{ border-bottom:9px solid #c43d3d; }} .trade-marker.sell {{ border-top:9px solid #18794e; }}
    .table-wrap {{ overflow-x:auto; }} .comparison {{ width:100%; min-width:760px; border-collapse:collapse; margin-top:8px; }} .comparison th,.comparison td {{ padding:10px 8px; border-bottom:1px solid var(--border); text-align:right; }} .comparison th:first-child,.comparison td:first-child {{ text-align:left; }} .comparison th {{ color:var(--muted); font-size:12px; font-weight:400; }}
    footer {{ margin-top:30px; padding-top:14px; border-top:1px solid var(--border); color:var(--muted); font-size:12px; }}
    @media (max-width:760px) {{ main {{ width:min(100% - 20px,1180px); padding-top:18px; }} header,.section-head {{ align-items:flex-start; flex-direction:column; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .chart {{ min-width:700px; }} .chart-wrap {{ overflow-x:auto; }} .trade-chart-toolbar {{ align-items:flex-start; flex-direction:column; }} .range-control {{ width:100%; }} .range-button {{ flex:1; min-width:0; }} }}
  </style>
</head>
<body>
<main>
  <header><div><h1>ETF轮动回测报告</h1><p>{summary['start_date']} 至 {summary['end_date']} · {html.escape(profile)}</p></div><p>513100 国泰纳指ETF · 159915 易方达创业板ETF</p></header>
  <div class="metrics">
    <div class="metric"><span>累计收益</span><strong>{_pct(summary['total_return'])}</strong></div>
    <div class="metric"><span>年化收益</span><strong>{_pct(summary['annual_return'])}</strong></div>
    <div class="metric"><span>最大回撤</span><strong>{_pct(summary['max_drawdown'])}</strong></div>
    <div class="metric"><span>年化波动</span><strong>{_pct(summary['annual_volatility'])}</strong></div>
    <div class="metric"><span>标准夏普</span><strong>{summary['sharpe_zero_rate']:.2f}</strong></div>
    <div class="metric"><span>期末权益</span><strong>{_number(summary['ending_equity'])}</strong></div>
  </div>
  <section><div class="section-head"><div><h2>累计收益</h2><p>统一从0%起算</p></div>{legend}</div><div class="chart-wrap">{performance_chart}</div></section>
  <section><div class="section-head"><div><h2>历史回撤</h2><p>相对各自历史净值高点</p></div>{legend}</div><div class="chart-wrap">{drawdown_chart}</div></section>
  <section><div class="section-head"><div><h2>年度收益</h2><p>首尾年份可能不是完整年度</p></div>{legend}</div><div class="chart-wrap">{_bar_chart(nav, annual_series)}</div></section>
  <section><div class="section-head"><div><h2>仓位变化</h2><p>收盘后的实际权重</p></div>{legend}</div><div class="chart-wrap">{_weight_chart(nav)}</div></section>
  <section><div class="section-head"><div><h2>成交点K线</h2><p>红色向上标记为买入，绿色向下标记为卖出；移动到日期上查看完整成交明细</p></div>
    <div class="trade-legend"><span><i class="trade-marker buy"></i>买入</span><span><i class="trade-marker sell"></i>卖出</span></div></div>
    {trade_panels}
  </section>
  <section><div class="section-head"><div><h2>策略与基准对比</h2><p>同一回测区间；择时策略计入交易成本，买入持有基准不计成本</p></div></div>
    <div class="table-wrap"><table class="comparison"><thead><tr><th>组合</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th><th>年化波动</th><th>夏普</th><th>交易笔数</th></tr></thead><tbody>
      {comparison_rows}
    </tbody></table></div>
  </section>
  <footer>交易成本 {_pct(summary['assumptions']['trading_cost_rate'])} · 调仓 {summary['rebalance_count']} 次 · 现金收益按0% · 信号收盘后生成，下一共同交易日开盘成交</footer>
</main>
<script id="trade-chart-data" type="application/json">{trade_chart_data}</script>
<script>{TRADE_CHART_SCRIPT}</script>
</body>
</html>"""
    Path(output_path).write_text(document, encoding="utf-8")
