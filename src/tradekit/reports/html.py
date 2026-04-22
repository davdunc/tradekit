"""HTML game plan report generator — SMB Capital dashboard style."""

import datetime
from html import escape
from pathlib import Path

from tradekit.config import now_et

_CSS = """\
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: #0d1117; color: #c9d1d9;
  font-family: -apple-system, 'Segoe UI', system-ui, sans-serif;
  padding: 24px; min-height: 100vh;
}
header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 24px; padding: 16px 0;
}
header .nav { display: flex; align-items: center; gap: 16px; }
header .date-badge {
  background: #21262d; border: 1px solid #30363d; border-radius: 6px;
  padding: 6px 16px; font-size: 14px; color: #c9d1d9;
}
header .today-btn {
  background: #238636; border: none; border-radius: 6px;
  padding: 6px 16px; color: #fff; font-size: 13px; font-weight: 600;
  cursor: pointer;
}
.card {
  background: #161b22; border: 1px solid #21262d; border-radius: 8px;
  padding: 24px; margin-bottom: 20px;
}
.card h2 {
  font-size: 18px; font-weight: 700; margin-bottom: 16px; color: #f0f6fc;
}
.card h2 .icon { margin-right: 8px; }
.card .narrative { color: #8b949e; font-size: 14px; line-height: 1.6; }
table {
  width: 100%; border-collapse: collapse; font-size: 14px;
}
thead th {
  text-align: left; padding: 10px 12px; color: #8b949e;
  font-weight: 600; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.5px; border-bottom: 1px solid #21262d;
}
tbody td {
  padding: 12px; border-bottom: 1px solid #21262d1a;
  vertical-align: top;
}
tbody tr:hover { background: #1c2128; }
.ticker-badge {
  display: inline-flex; align-items: center; gap: 6px;
  background: #0d419d22; border: 1px solid #1f6feb44;
  border-radius: 4px; padding: 3px 10px; color: #58a6ff;
  font-weight: 600; font-size: 13px; text-decoration: none;
}
.ticker-badge svg { width: 14px; height: 14px; fill: #58a6ff; }
.bias-bull { color: #3fb950; font-weight: 600; }
.bias-bear { color: #f85149; font-weight: 600; }
.bias-neut { color: #8b949e; }
.chg-pos { color: #3fb950; }
.chg-neg { color: #f85149; }
.empty-cell { color: #484f58; }
.regime-table { margin-bottom: 16px; }
.regime-table td, .regime-table th { padding: 8px 12px; }
.breadth-bar {
  display: flex; align-items: center; gap: 12px;
  margin-top: 12px; font-size: 13px; color: #8b949e;
}
.breadth-fill {
  height: 6px; border-radius: 3px; background: #21262d; flex: 1; overflow: hidden;
}
.breadth-fill .inner { height: 100%; border-radius: 3px; }
.notes-section { margin-top: 8px; }
.notes-section .note {
  background: #1c2128; border-left: 3px solid #1f6feb;
  padding: 12px 16px; margin-bottom: 8px; border-radius: 0 4px 4px 0;
  font-size: 13px; line-height: 1.5;
}
.section-empty {
  color: #484f58; font-style: italic; padding: 16px 0; font-size: 14px;
}
"""

_CHART_ICON_SVG = (
    '<svg viewBox="0 0 16 16"><path d="M1 14h14V2h-1v11H1v1zm1-2h1V8H2v4zm'
    "3 0h1V5H5v7zm3 0h1V7H8v5zm3 0h1V3h-1v9z"
    '"/></svg>'
)


def _ticker_link(ticker: str) -> str:
    t = escape(ticker)
    return (
        f'<a class="ticker-badge" href="https://finviz.com/quote.ashx?t={t}" target="_blank">{_CHART_ICON_SVG} {t}</a>'
    )


def _fmt_levels(levels: list[dict], n: int = 3) -> str:
    if not levels:
        return '<span class="empty-cell">-</span>'
    return ", ".join(f"${l['level']:.2f}" for l in levels[:n])


def _bias_class(sentiment: str) -> str:
    if sentiment in ("BULL", "bullish"):
        return "bias-bull"
    if sentiment in ("BEAR", "bearish"):
        return "bias-bear"
    return "bias-neut"


def _bias_label(sentiment: str) -> str:
    if sentiment in ("BULL", "bullish"):
        return "Bullish"
    if sentiment in ("BEAR", "bearish"):
        return "Bearish"
    return "-"


def _watchlist_rows_html(items: list[dict]) -> str:
    if not items:
        return '<tr><td colspan="8" class="section-empty">No items found.</td></tr>'

    rows = []
    for item in items:
        ticker = item.get("ticker", "")
        support = item.get("support", "")
        resistance = item.get("resistance", "")
        inflexion = item.get("inflexion", "")
        notes = escape(str(item.get("notes", "")))
        bias = item.get("bias", "")
        setup = escape(str(item.get("setup", ""))) or '<span class="empty-cell">-</span>'
        plan = escape(str(item.get("trading_plan", ""))) or '<span class="empty-cell">-</span>'

        rows.append(f"""<tr>
  <td>{_ticker_link(ticker)}</td>
  <td>{escape(str(support))}</td>
  <td>{escape(str(resistance))}</td>
  <td>{escape(str(inflexion)) if inflexion else '<span class="empty-cell">-</span>'}</td>
  <td>{notes}</td>
  <td><span class="{_bias_class(bias)}">{_bias_label(bias)}</span></td>
  <td>{setup}</td>
  <td>{plan}</td>
</tr>""")
    return "\n".join(rows)


def _regime_html(regime_data: dict) -> str:
    indices = regime_data.get("indices", [])
    spy_levels = regime_data.get("spy_levels", {})
    energy_futures = regime_data.get("energy_futures", [])
    breadth = regime_data.get("sector_breadth", {})

    # Index table
    idx_rows = []
    for idx in indices:
        sym = escape(idx["symbol"])
        price = idx.get("price", 0)
        chg = idx.get("change_pct", 0)
        chg_cls = "chg-pos" if chg >= 0 else "chg-neg"
        rsi = idx.get("rsi", 0)
        label = escape(str(idx.get("label", "")))
        idx_rows.append(
            f"<tr><td><strong>{sym}</strong></td>"
            f"<td>${price:.2f}</td>"
            f'<td class="{chg_cls}">{chg:+.2f}%</td>'
            f"<td>{rsi:.0f}</td>"
            f"<td>{label}</td></tr>"
        )

    # Energy futures table
    energy_rows = []
    for ef in energy_futures:
        sym = escape(ef["symbol"])
        price = ef.get("price", 0)
        chg = ef.get("change_pct", 0)
        chg_cls = "chg-pos" if chg >= 0 else "chg-neg"
        rsi = ef.get("rsi", 0)
        label = escape(str(ef.get("label", "")))
        energy_rows.append(
            f"<tr><td><strong>{sym}</strong></td>"
            f"<td>${price:.2f}</td>"
            f'<td class="{chg_cls}">{chg:+.2f}%</td>'
            f"<td>{rsi:.0f}</td>"
            f"<td>{label}</td></tr>"
        )

    # SPY levels
    r_levels = spy_levels.get("resistance", [])
    s_levels = spy_levels.get("support", [])
    spy_r = ", ".join(f"${r['level']:.2f}" for r in r_levels[:3]) or "-"
    spy_s = ", ".join(f"${s['level']:.2f}" for s in s_levels[:3]) or "-"

    # Breadth
    green = breadth.get("green", 0)
    total = breadth.get("total", 11)
    pct = breadth.get("pct_green", 0)
    bar_color = "#3fb950" if pct >= 60 else "#d29922" if pct >= 40 else "#f85149"

    strongest = breadth.get("strongest")
    weakest = breadth.get("weakest")
    strongest_str = ""
    if strongest:
        strongest_str = f'Strongest: <span class="chg-pos">{escape(strongest[0])} ({escape(strongest[1])}) {strongest[2]:+.2f}%</span>'
    weakest_str = ""
    if weakest:
        weakest_str = (
            f'Weakest: <span class="chg-neg">{escape(weakest[0])} ({escape(weakest[1])}) {weakest[2]:+.2f}%</span>'
        )

    energy_section = ""
    if energy_rows:
        energy_section = f"""
<h3 style="font-size:14px;color:#f0f6fc;margin:16px 0 8px">⛽ Energy Futures</h3>
<table class="regime-table">
  <thead><tr><th>Contract</th><th>Price</th><th>Change</th><th>RSI</th><th>Trend</th></tr></thead>
  <tbody>{"".join(energy_rows)}</tbody>
</table>"""

    return f"""
<table class="regime-table">
  <thead><tr><th>Index</th><th>Price</th><th>Change</th><th>RSI</th><th>Trend</th></tr></thead>
  <tbody>{"".join(idx_rows)}</tbody>
</table>
{energy_section}
<p class="narrative" style="margin-top:12px">
  <strong>SPY Levels</strong> &mdash;
  <span class="chg-neg">R: {spy_r}</span> &gt;
  <span class="chg-pos">S: {spy_s}</span>
</p>
<div class="breadth-bar">
  Breadth: {green}/{total} green ({pct:.0f}%)
  <div class="breadth-fill"><div class="inner" style="width:{pct:.0f}%;background:{bar_color}"></div></div>
</div>
<p class="narrative" style="margin-top:8px">{strongest_str} &nbsp; {weakest_str}</p>
"""


def generate_gameplan_html(
    *,
    regime_data: dict,
    fresh_news: list[dict],
    second_day_plays: list[dict],
    big_picture_narrative: str = "",
    date_str: str = "",
) -> str:
    """Generate a self-contained HTML game plan matching SMB Capital dashboard style.

    Args:
        regime_data: Dict with 'indices', 'spy_levels', 'sector_breadth'.
        fresh_news: List of dicts with ticker/support/resistance/inflexion/notes/bias/setup/trading_plan.
        second_day_plays: Same format as fresh_news.
        big_picture_narrative: Optional narrative text for big picture section.
        date_str: Display date string, defaults to today ET.

    Returns:
        Complete HTML string.
    """
    if not date_str:
        date_str = now_et().strftime("%a, %b %d, %Y")

    regime_section = _regime_html(regime_data)
    news_rows = _watchlist_rows_html(fresh_news)
    second_day_rows = _watchlist_rows_html(second_day_plays)

    narrative_html = ""
    if big_picture_narrative:
        narrative_html = f'<p class="narrative" style="margin-top:16px">{escape(big_picture_narrative)}</p>'

    table_header = """<thead><tr>
  <th>Ticker</th><th>Support</th><th>Resistance</th><th>Inflexion</th>
  <th>Notes</th><th>Bias</th><th>Setup</th><th>Trading Plan</th>
</tr></thead>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Game Plan — {escape(date_str)}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <div class="nav">
    <span class="date-badge">📅 {escape(date_str)}</span>
    <span style="color:#484f58;font-size:13px">Game Plan</span>
  </div>
</header>

<section class="card" id="big-picture">
  <h2>Big Picture</h2>
  {regime_section}
  {narrative_html}
</section>

<section class="card" id="fresh-news">
  <h2><span class="icon">📈</span> Fresh News</h2>
  <table>
    {table_header}
    <tbody>{news_rows}</tbody>
  </table>
</section>

<section class="card" id="second-day">
  <h2><span class="icon">📉</span> Second Day Plays, Technical Setups and Trading Ideas</h2>
  <table>
    {table_header}
    <tbody>{second_day_rows}</tbody>
  </table>
</section>


</body>
</html>"""


def save_gameplan_html(html: str, date: datetime.date | None = None) -> Path:
    """Save HTML game plan to Trade_Review day directory."""
    from tradekit.config import now_et
    from tradekit.data.finviz import _trade_review_day_dir

    day_dir = _trade_review_day_dir(date)
    day_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now_et().strftime("%H%M%S")
    path = day_dir / f"gameplan-{timestamp}.html"
    path.write_text(html, encoding="utf-8")
    return path
