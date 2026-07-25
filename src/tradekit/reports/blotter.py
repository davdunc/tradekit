"""Round-trip blotter — one candlestick PNG per closed round-trip.

Pulls round-trips from the ``falcon-trades`` DynamoDB table and 1-minute bars
(all sessions incl. premarket) from Massive.com flat files, then renders a
static PNG per round-trip with entry/exit markers. Designed to be embedded
inline in the Notion end-of-day review (which cannot render interactive HTML).

Data sources
------------
- Round-trips: DynamoDB ``falcon-trades`` (``PK=TRADE#YYYY-MM-DD``). Trade
  ``entry_time`` / ``exit_time`` are naive **ET** strings.
- Bars: Massive minute flat file
  ``us_stocks_sip/minute_aggs_v1/YYYY/MM/YYYY-MM-DD.csv.gz`` — CSV columns
  ``ticker,volume,open,close,high,low,window_start(ns UTC),transactions``.
  Includes premarket, RTH, and postmarket, so no TradingView fallback needed.
"""

from __future__ import annotations

import gzip
import io
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

from tradekit.config import get_settings  # noqa: E402

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# dark trading-terminal palette (matches the HTML blotter)
BG = "#0b0e11"
PANEL = "#12161c"
UP = "#3fb950"
DOWN = "#f4645c"
INK = "#e6edf3"
DIM = "#8b98a5"
CYAN = "#4aa8c0"
GRID = "#232b34"

WINDOW_MIN = 10  # minutes of context on each side of the round-trip


@dataclass
class RoundTrip:
    idx: int
    symbol: str
    side: str
    shares: float
    avg_entry: float
    avg_exit: float
    entry_time: datetime  # ET-aware
    exit_time: datetime  # ET-aware
    pnl: float
    r_multiple: float | None
    is_winner: bool


def _to_et(iso_naive: str) -> datetime:
    """Parse a naive ET timestamp string into an ET-aware datetime."""
    return datetime.fromisoformat(iso_naive).replace(tzinfo=ET)


def fetch_round_trips(
    date: str,
    *,
    table: str = "falcon-trades",
    region: str = "us-east-2",
    profile: str | None = None,
) -> list[RoundTrip]:
    """Query DynamoDB for every round-trip on ``date`` (YYYY-MM-DD), sorted."""
    profile = profile or os.environ.get("AWS_PROFILE") or os.environ.get("PAI_AWS_PROFILE")
    session = boto3.session.Session(profile_name=profile, region_name=region)
    tbl = session.resource("dynamodb").Table(table)
    resp = tbl.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": f"TRADE#{date}"},
    )
    items = resp.get("Items", [])
    rts: list[RoundTrip] = []
    for it in items:
        r = it.get("r_multiple")
        rts.append(
            RoundTrip(
                idx=0,
                symbol=str(it["symbol"]),
                side=str(it.get("side", "")),
                shares=float(it.get("shares", 0)),
                avg_entry=float(it["avg_entry"]),
                avg_exit=float(it["avg_exit"]),
                entry_time=_to_et(str(it["entry_time"])),
                exit_time=_to_et(str(it["exit_time"])),
                pnl=float(it.get("pnl", 0)),
                r_multiple=None if r is None else float(r),
                is_winner=bool(it.get("is_winner", float(it.get("pnl", 0)) >= 0)),
            )
        )
    rts.sort(key=lambda x: (x.symbol, x.entry_time))
    for i, rt in enumerate(rts, 1):
        rt.idx = i
    return rts


def fetch_minute_bars(symbols: set[str], date: str, *, settings=None):
    """Return {symbol: list[(dt_et, o, h, l, c)]} from the Massive minute flat file."""
    settings = settings or get_settings()
    d = settings.data
    ak = d.backtest_access_key or os.environ.get("MASSIVE_S3_ACCESS_KEY", "")
    sk = d.backtest_secret_key or os.environ.get("MASSIVE_S3_SECRET_KEY", "")
    s3 = boto3.client(
        "s3",
        endpoint_url=d.backtest_endpoint,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
    )
    y, m, _ = date.split("-")
    key = f"us_stocks_sip/minute_aggs_v1/{y}/{m}/{date}.csv.gz"
    logger.info("Fetching s3://%s/%s", d.backtest_bucket, key)
    body = s3.get_object(Bucket=d.backtest_bucket, Key=key)["Body"].read()
    out: dict[str, list] = {s: [] for s in symbols}
    with gzip.open(io.BytesIO(body), "rt") as fh:
        for line in fh:
            tk, _, rest = line.partition(",")
            if tk not in symbols:
                continue
            f = line.rstrip("\n").split(",")
            # ticker,volume,open,close,high,low,window_start,transactions
            o, c, h, low = float(f[2]), float(f[3]), float(f[4]), float(f[5])
            t = datetime.fromtimestamp(int(f[6]) / 1e9, tz=UTC).astimezone(ET)
            out[tk].append((t, o, h, low, c))
    for s in out:
        out[s].sort(key=lambda b: b[0])
    return out


def _render_one(rt: RoundTrip, bars: list, out_path: Path) -> Path:
    lo = rt.entry_time - timedelta(minutes=WINDOW_MIN)
    hi = rt.exit_time + timedelta(minutes=WINDOW_MIN)
    win = [b for b in bars if lo <= b[0] <= hi]

    fig, ax = plt.subplots(figsize=(4.2, 2.6), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)
    edge = UP if rt.is_winner else DOWN

    if len(win) >= 2:
        width = 0.0004  # candle width in day-units (~35s)
        for t, o, h, low, c in win:
            x = mdates.date2num(t)
            col = UP if c >= o else DOWN
            ax.plot([x, x], [low, h], color=col, linewidth=0.7, zorder=2)
            ax.add_patch(
                Rectangle(
                    (x - width / 2, min(o, c)),
                    width,
                    max(abs(c - o), 0.001),
                    facecolor=col, edgecolor=col, alpha=0.9, zorder=3,
                )
            )
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ET))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    else:
        ax.text(0.5, 0.5, "no bars in window", color=DIM, ha="center",
                va="center", transform=ax.transAxes, fontsize=8)

    # entry / exit markers + level lines
    ax.axhline(rt.avg_entry, color=CYAN, lw=0.7, alpha=0.5, zorder=1)
    ax.axhline(rt.avg_exit, color=edge, lw=0.7, alpha=0.5, zorder=1)
    ex = mdates.date2num(rt.entry_time)
    xx = mdates.date2num(rt.exit_time)
    ax.scatter([ex], [rt.avg_entry], marker="^", s=42, color=CYAN, zorder=5)
    ax.scatter([xx], [rt.avg_exit], marker="v", s=42, color=edge, zorder=5)

    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.tick_params(colors=DIM, labelsize=6.5, length=2)
    ax.grid(color=GRID, alpha=0.35, linewidth=0.4)
    ax.margins(x=0.02, y=0.14)

    r_txt = "R n/a" if rt.r_multiple is None else f"{rt.r_multiple:+.2f}R"
    pnl_txt = f"{'-$' if rt.pnl < 0 else '+$'}{abs(rt.pnl):,.2f}"
    ax.set_title(
        f"#{rt.idx}  {rt.symbol}  {rt.side}  {rt.shares:g}sh   "
        f"{pnl_txt}  {r_txt}",
        color=edge if not rt.is_winner else UP, fontsize=8, fontweight="bold",
        loc="left", pad=6,
    )
    fig.text(0.99, 0.02,
             f"{rt.entry_time:%H:%M:%S} → {rt.exit_time:%H:%M:%S} ET  "
             f"{rt.avg_entry:g}→{rt.avg_exit:g}",
             color=DIM, fontsize=6, ha="right")
    fig.tight_layout(pad=0.6)
    fig.savefig(out_path, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return out_path


def render_blotter(date: str, out_dir: str | Path, *, settings=None) -> list[Path]:
    """Render one PNG per round-trip for ``date`` into ``out_dir``. Returns paths."""
    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    rts = fetch_round_trips(date)
    if not rts:
        logger.warning("No round-trips found for %s", date)
        return []
    bars = fetch_minute_bars({rt.symbol for rt in rts}, date, settings=settings)
    paths: list[Path] = []
    for rt in rts:
        p = out / f"{date}_{rt.idx:02d}_{rt.symbol}_{rt.side.lower()}.png"
        _render_one(rt, bars.get(rt.symbol, []), p)
        paths.append(p)
    logger.info("Rendered %d round-trip charts → %s", len(paths), out)
    return paths
