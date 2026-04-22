"""CLI entry point for tradekit."""

import logging
import os
import sys
from pathlib import Path

import click
from rich.console import Console

from tradekit.config import get_settings, now_et, shared_env_path

console = Console(force_terminal=True)
logger = logging.getLogger("tradekit")

source_option = click.option(
    "--source",
    type=click.Choice(["yahoo", "massive", "backtest"], case_sensitive=False),
    default=None,
    help="Data source: yahoo (default), massive, or backtest.",
)


def get_provider(source: str | None = None):
    """Return the appropriate data provider based on source name."""
    settings = get_settings()
    source = source or settings.data_source
    if source == "massive":
        from tradekit.data.massive import MassiveProvider

        return MassiveProvider()
    elif source == "backtest":
        from tradekit.data.backtest import BacktestProvider

        return BacktestProvider()
    else:
        from tradekit.data.yahoo import YahooProvider

        return YahooProvider()


def _market_session() -> str:
    """Return a description of the current market session in ET."""
    t = now_et()
    hour, minute = t.hour, t.minute
    mins = hour * 60 + minute

    if mins < 4 * 60:
        return "Overnight (market closed)"
    elif mins < 9 * 60 + 30:
        return "Pre-market"
    elif mins < 16 * 60:
        return "Market open"
    elif mins < 20 * 60:
        return "After-hours"
    else:
        return "Overnight (market closed)"


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging.")
def cli(verbose: bool):
    """tradekit — Pre-market screening and technical analysis toolkit."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    et = now_et()
    console.print(f"[dim]{et.strftime('%a %b %d, %I:%M %p')} ET — {_market_session()}[/dim]")


@cli.command()
@click.option("--preset", default="premarket_gap", help="Screener preset name.")
@click.option("--min-gap", type=float, default=None, help="Override minimum gap %.")
@click.option("--min-volume", type=int, default=None, help="Override minimum avg daily volume.")
@click.option("--max-price", type=float, default=None, help="Override maximum price.")
@source_option
def scan(
    preset: str,
    min_gap: float | None,
    min_volume: int | None,
    max_price: float | None,
    source: str | None,
):
    """Run the pre-market stock scanner."""
    from tradekit.reports.terminal import print_scan_results
    from tradekit.screener.premarket import scan_premarket

    settings = get_settings()
    provider = get_provider(source)

    # Apply CLI overrides to screener settings
    if min_gap is not None:
        settings.screener.min_gap_pct = min_gap
    if min_volume is not None:
        settings.screener.min_avg_volume = min_volume
    if max_price is not None:
        settings.screener.max_price = max_price

    console.print("[bold]Running pre-market scan...[/bold]")
    df = scan_premarket(settings=settings, preset=preset, provider=provider)
    print_scan_results(df)

    if not df.empty:
        tickers = df["ticker"].tolist()
        console.print(f"\n[dim]Tickers: {', '.join(tickers)}[/dim]")


@cli.command()
@click.argument("ticker")
@click.option("--period", default="3mo", help="History period (e.g. 1mo, 3mo, 6mo, 1y).")
@source_option
def analyze(ticker: str, period: str, source: str | None):
    """Run deep technical analysis on a ticker."""
    from tradekit.analysis.indicators import compute_all_indicators
    from tradekit.analysis.levels import find_support_resistance, get_nearest_levels
    from tradekit.analysis.scoring import compute_composite_score
    from tradekit.analysis.volume import add_volume_indicators
    from tradekit.reports.terminal import print_analysis

    settings = get_settings()
    presets = settings.load_indicator_presets()
    provider = get_provider(source)

    ticker = ticker.upper()
    console.print(f"[bold]Analyzing {ticker}...[/bold]")

    # Fetch data
    quote = provider.get_quote(ticker)
    df = provider.get_history(ticker, period=period)
    if df.empty:
        console.print(f"[red]No data available for {ticker}[/red]")
        return

    # Compute indicators
    df = compute_all_indicators(df, presets=presets)
    df = add_volume_indicators(df)

    # Score
    latest = df.iloc[-1]
    score = compute_composite_score(latest, presets.get("scoring_weights"))

    # Levels
    sr_levels = find_support_resistance(df)
    current_price = quote.get("price", latest.get("close", 0))
    nearest = get_nearest_levels(current_price, sr_levels)

    print_analysis(ticker, score, nearest, quote)

    # Print RVOL and ATR prominently
    console.print("[bold]Volatility & Volume:[/bold]")
    rvol = latest.get("relative_volume")
    if rvol is not None:
        rvol_style = "bold green" if rvol >= 2.0 else "green" if rvol >= 1.5 else "yellow" if rvol >= 1.0 else "red"
        console.print(f"  RVOL: [{rvol_style}]{rvol:.2f}x[/{rvol_style}]", end="")
        if rvol >= 3.0:
            console.print("  [bold magenta]EXTREME[/bold magenta]")
        elif rvol >= 2.0:
            console.print("  [bold green]HIGH[/bold green]")
        elif rvol >= 1.5:
            console.print("  [green]ABOVE AVG[/green]")
        elif rvol >= 1.0:
            console.print("  [yellow]NORMAL[/yellow]")
        else:
            console.print("  [red]BELOW AVG[/red]")

    atr = latest.get("atr")
    atr_pct = latest.get("atr_pct")
    if atr is not None:
        console.print(f"  ATR(14): ${atr:.2f}  ({atr_pct:.1f}% of price)")

    vwap = latest.get("vwap")
    if vwap is not None:
        current = quote.get("price", latest.get("close", 0))
        vwap_dist = ((current - vwap) / vwap * 100) if vwap else 0
        vwap_style = "green" if vwap_dist > 0 else "red"
        console.print(f"  VWAP: ${vwap:.2f}  [{vwap_style}]({vwap_dist:+.1f}%)[/{vwap_style}]")

    console.print()

    # Print indicator snapshot
    console.print("[bold]Indicator Snapshot:[/bold]")
    rsi = latest.get("rsi")
    if rsi is not None:
        rsi_style = "red" if rsi > 70 else "green" if rsi < 30 else "white"
        console.print(f"  RSI(14): [{rsi_style}]{rsi:.1f}[/{rsi_style}]")

    macd_h = latest.get("macd_histogram")
    if macd_h is not None:
        macd_style = "green" if macd_h > 0 else "red"
        console.print(f"  MACD Hist: [{macd_style}]{macd_h:.3f}[/{macd_style}]")

    stoch_k = latest.get("stoch_k")
    stoch_d = latest.get("stoch_d")
    if stoch_k is not None:
        console.print(f"  Stochastic: %K={stoch_k:.1f}  %D={stoch_d:.1f}")

    console.print()


@cli.command()
@click.argument("ticker")
@click.option("--period", default="3mo", help="History period for level detection.")
@source_option
def levels(ticker: str, period: str, source: str | None):
    """Show support and resistance levels for a ticker."""
    from tradekit.analysis.levels import find_support_resistance, get_nearest_levels
    from tradekit.analysis.volume import find_high_volume_nodes

    provider = get_provider(source)
    ticker = ticker.upper()

    console.print(f"[bold]Support/Resistance for {ticker}...[/bold]")
    quote = provider.get_quote(ticker)
    df = provider.get_history(ticker, period=period)

    if df.empty:
        console.print(f"[red]No data for {ticker}[/red]")
        return

    sr = find_support_resistance(df)
    current_price = quote.get("price", df["close"].iloc[-1])
    nearest = get_nearest_levels(current_price, sr)

    console.print(f"\n  Current: [bold]${current_price:.2f}[/bold]\n")

    for r in nearest.get("resistance", []):
        dist = (r["level"] - current_price) / current_price * 100
        console.print(f"  [red]R ${r['level']:.2f}[/red]  (+{dist:.1f}%)  strength: {r['strength']}")

    console.print(f"  [bold cyan]→ ${current_price:.2f}[/bold cyan]")

    for s in nearest.get("support", []):
        dist = (current_price - s["level"]) / current_price * 100
        console.print(f"  [green]S ${s['level']:.2f}[/green]  (-{dist:.1f}%)  strength: {s['strength']}")

    # High volume nodes
    hvn = find_high_volume_nodes(df["close"], df["volume"])
    if hvn:
        console.print("\n  [yellow]High Volume Nodes:[/yellow] " + "  ".join(f"${p:.2f}" for p in hvn))

    console.print()


@cli.command()
@click.option("--name", default="default", help="Watchlist name from config.")
@source_option
def watchlist(name: str, source: str | None):
    """Review watchlist tickers with pre-market data."""
    from tradekit.reports.terminal import print_scan_results
    from tradekit.screener.premarket import scan_watchlist

    settings = get_settings()
    provider = get_provider(source)
    console.print(f"[bold]Scanning watchlist '{name}'...[/bold]")

    df = scan_watchlist(settings=settings, watchlist_name=name, provider=provider)
    print_scan_results(df, title=f"Watchlist: {name}")


@cli.command()
@source_option
def regime(source: str | None):
    """Show market regime summary: SPY/QQQ/VIX + sector breadth."""
    from rich.table import Table

    from tradekit.analysis.indicators import compute_all_indicators
    from tradekit.analysis.levels import find_support_resistance, get_nearest_levels

    provider = get_provider(source)

    # --- Index + VIX overview ---
    index_table = Table(title="Market Regime", show_lines=True, pad_edge=True)
    index_table.add_column("Symbol", style="bold cyan", width=6)
    index_table.add_column("Price", justify="right", width=10)
    index_table.add_column("Chg%", justify="right", width=8)
    index_table.add_column("vs SMA20", justify="right", width=9)
    index_table.add_column("vs SMA50", justify="right", width=9)
    index_table.add_column("vs SMA200", justify="right", width=9)
    index_table.add_column("RSI", justify="right", width=6)
    index_table.add_column("Label", width=12)

    for sym in ("SPY", "QQQ", "^VIX"):
        display = sym.replace("^", "")
        try:
            quote = provider.get_quote(sym)
            price = quote.get("price", 0)
            prev = quote.get("prev_close", 0)
            chg_pct = ((price - prev) / prev * 100) if prev else 0
            chg_style = "green" if chg_pct >= 0 else "red"

            df = provider.get_history(sym, period="1y")
            if df.empty:
                index_table.add_row(
                    display,
                    f"${price:.2f}",
                    f"[{chg_style}]{chg_pct:+.2f}%[/{chg_style}]",
                    "-",
                    "-",
                    "-",
                    "-",
                    "-",
                )
                continue

            df = compute_all_indicators(df)
            latest = df.iloc[-1]
            rsi = latest.get("rsi", 0)

            sma20 = latest.get("ema_20") or latest.get("sma_20", 0)
            sma50 = latest.get("sma_50", 0)
            sma200 = latest.get("sma_200", 0)

            def _vs(sma):
                if not sma:
                    return "-"
                d = (price - sma) / sma * 100
                s = "green" if d >= 0 else "red"
                return f"[{s}]{d:+.1f}%[/{s}]"

            # Label
            if display == "VIX":
                if price < 15:
                    label = "[green]Low[/green]"
                elif price < 20:
                    label = "[green]Normal[/green]"
                elif price < 25:
                    label = "[yellow]Elevated[/yellow]"
                elif price < 30:
                    label = "[red]High[/red]"
                else:
                    label = "[bold red]Extreme[/bold red]"
            else:
                if price > sma50 and price > sma200:
                    label = "[green]Uptrend[/green]"
                elif price > sma200:
                    label = "[yellow]Pullback[/yellow]"
                else:
                    label = "[red]Downtrend[/red]"

            rsi_style = "red" if rsi > 70 else "green" if rsi < 30 else "white"
            index_table.add_row(
                display,
                f"${price:.2f}",
                f"[{chg_style}]{chg_pct:+.2f}%[/{chg_style}]",
                _vs(sma20),
                _vs(sma50),
                _vs(sma200),
                f"[{rsi_style}]{rsi:.0f}[/{rsi_style}]",
                label,
            )
        except Exception as e:
            index_table.add_row(display, "-", "-", "-", "-", "-", "-", "[red]err[/red]")
            logger.warning("Failed to fetch %s: %s", sym, e)

    console.print(index_table)

    # --- SPY key levels ---
    try:
        spy_df = provider.get_history("SPY", period="3mo")
        if not spy_df.empty:
            sr = find_support_resistance(spy_df)
            spy_price = provider.get_quote("SPY").get("price", spy_df["close"].iloc[-1])
            nearest = get_nearest_levels(spy_price, sr, n=2)
            r_str = "  ".join(f"${r['level']:.2f}" for r in nearest.get("resistance", []))
            s_str = "  ".join(f"${s['level']:.2f}" for s in nearest.get("support", []))
            console.print(
                f"\n[bold]SPY Key Levels:[/bold]  [red]R: {r_str}[/red]  "
                f">  ${spy_price:.2f}  >  [green]S: {s_str}[/green]"
            )
    except Exception as e:
        logger.warning("Failed to compute SPY levels: %s", e)

    # --- Sector breadth ---
    sector_etfs = {
        "XLK": "Tech",
        "XLF": "Financials",
        "XLE": "Energy",
        "XLV": "Health",
        "XLI": "Industrials",
        "XLC": "Comms",
        "XLY": "Cons Disc",
        "XLP": "Cons Staples",
        "XLU": "Utilities",
        "XLRE": "Real Estate",
        "XLB": "Materials",
    }

    console.print("\n[bold]Sector Breadth:[/bold]")
    breadth_table = Table(show_lines=False, pad_edge=True)
    breadth_table.add_column("ETF", style="bold", width=5)
    breadth_table.add_column("Sector", width=12)
    breadth_table.add_column("Chg%", justify="right", width=8)

    green_count = 0
    sector_data = []
    for etf, name in sector_etfs.items():
        try:
            q = provider.get_quote(etf)
            price = q.get("price", 0)
            prev = q.get("prev_close", 0)
            chg = ((price - prev) / prev * 100) if prev else 0
            sector_data.append((etf, name, chg))
            if chg >= 0:
                green_count += 1
        except Exception:
            sector_data.append((etf, name, 0))

    sector_data.sort(key=lambda x: x[2], reverse=True)
    for etf, name, chg in sector_data:
        style = "green" if chg >= 0 else "red"
        breadth_table.add_row(etf, name, f"[{style}]{chg:+.2f}%[/{style}]")

    console.print(breadth_table)

    total = len(sector_etfs)
    pct_green = green_count / total * 100 if total else 0
    breadth_style = "green" if pct_green >= 60 else "yellow" if pct_green >= 40 else "red"
    console.print(f"\n  [{breadth_style}]Breadth: {green_count}/{total} green ({pct_green:.0f}%)[/{breadth_style}]")
    if sector_data:
        console.print(
            f"  [green]Strongest:[/green] {sector_data[0][0]} ({sector_data[0][1]}) {sector_data[0][2]:+.2f}%"
        )
        console.print(f"  [red]Weakest:[/red]   {sector_data[-1][0]} ({sector_data[-1][1]}) {sector_data[-1][2]:+.2f}%")
    console.print()


@cli.command("second-day")
@click.option("--min-change", type=float, default=5.0, help="Minimum yesterday move %.")
@click.option("--min-vol-ratio", type=float, default=1.5, help="Minimum volume ratio vs avg.")
@source_option
def second_day(min_change: float, min_vol_ratio: float, source: str | None):
    """Find 2nd-day play candidates from yesterday's big movers."""
    from rich.table import Table

    from tradekit.screener.premarket import scan_previous_movers

    provider = get_provider(source)

    console.print("[bold]Scanning for 2nd-day play candidates...[/bold]")
    df = scan_previous_movers(
        provider=provider,
        min_change_pct=min_change,
        min_volume_ratio=min_vol_ratio,
    )

    if df.empty:
        console.print("[yellow]No 2nd-day candidates found. Try lowering --min-change or --min-vol-ratio.[/yellow]")
        return

    table = Table(title="2nd Day Play Candidates", show_lines=False, pad_edge=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Ticker", style="bold cyan", width=8)
    table.add_column("Name", width=18, no_wrap=True)
    table.add_column("Price", justify="right", width=8)
    table.add_column("Prev Chg%", justify="right", width=10)
    table.add_column("Vol Ratio", justify="right", width=10)
    table.add_column("Pre-Mkt", justify="right", width=8)
    table.add_column("Pre Gap%", justify="right", width=9)

    for i, row in df.iterrows():
        chg = row["prev_change_pct"]
        chg_style = "green" if chg > 0 else "red"
        pre_gap = row.get("pre_gap_pct", 0)
        pre_style = "green" if pre_gap > 0 else "red" if pre_gap < 0 else "white"

        vol_ratio = row.get("volume_ratio", 0)
        vol_style = "bold green" if vol_ratio >= 3 else "green" if vol_ratio >= 2 else "yellow"

        pre_price = row.get("pre_price", 0)
        pre_str = f"${pre_price:.2f}" if pre_price else "-"

        table.add_row(
            str(int(i) + 1),
            row["ticker"],
            str(row.get("name", ""))[:18],
            f"${row['price']:.2f}",
            f"[{chg_style}]{chg:+.1f}%[/{chg_style}]",
            f"[{vol_style}]{vol_ratio:.1f}x[/{vol_style}]",
            pre_str,
            f"[{pre_style}]{pre_gap:+.1f}%[/{pre_style}]" if pre_price else "-",
        )

    console.print(table)
    tickers = df["ticker"].tolist()
    console.print(f"\n[dim]Tickers: {', '.join(tickers)}[/dim]")
    console.print()


@cli.command()
@click.option("--limit", default=25, help="Max headlines to show.")
@click.option(
    "--sentiment",
    type=click.Choice(["all", "bullish", "bearish", "neutral"], case_sensitive=False),
    default="all",
    help="Filter by sentiment.",
)
@click.option("--ticker", default=None, help="Filter by ticker symbol.")
@click.option("--save", "save_flag", is_flag=True, help="Save news to Trade_Review day directory.")
def news(limit: int, sentiment: str, ticker: str | None, save_flag: bool):
    """Show market news with sentiment from Finviz Elite."""
    from rich.table import Table

    from tradekit.data.finviz import FinvizProvider, save_news

    settings = get_settings()
    provider = FinvizProvider()
    items = provider.get_market_news(api_key=settings.data.finviz_api_key)

    if not items:
        console.print("[yellow]No news items returned. Check FINVIZ_API_KEY.[/yellow]")
        return

    if save_flag:
        saved_path = save_news(items)
        console.print(f"[green]News saved: {saved_path}[/green]")

    # Filter by sentiment
    sentiment_map = {"bullish": "BULL", "bearish": "BEAR", "neutral": "NEUT"}
    if sentiment != "all":
        target = sentiment_map[sentiment.lower()]
        items = [i for i in items if i["sentiment"] == target]

    # Filter by ticker
    if ticker:
        ticker_upper = ticker.upper()
        items = [i for i in items if ticker_upper in i["tickers"]]

    items = items[:limit]

    if not items:
        console.print("[yellow]No news items match filters.[/yellow]")
        return

    table = Table(title="Market Pulse News", show_lines=False, pad_edge=True)
    table.add_column("Time", style="dim", width=14)
    table.add_column("Sent", width=4)
    table.add_column("Tickers", style="cyan", width=14)
    table.add_column("Headline", width=60, no_wrap=True)
    table.add_column("Source", style="dim", width=12)

    for item in items:
        sent = item["sentiment"]
        if sent == "BULL":
            sent_style = "[bold green]BULL[/bold green]"
        elif sent == "BEAR":
            sent_style = "[bold red]BEAR[/bold red]"
        else:
            sent_style = "[dim]NEUT[/dim]"

        tickers_str = ", ".join(item["tickers"][:4])
        headline = item["headline"][:60]

        table.add_row(
            item["timestamp"],
            sent_style,
            tickers_str,
            headline,
            item["source"][:12],
        )

    console.print(table)
    console.print(f"[dim]{len(items)} items shown[/dim]")


@cli.command()
@click.option("--preset", default="premarket_gap", help="Screener preset.")
@click.option("--top-n", default=5, help="Number of top picks to analyze in detail.")
@source_option
def morning(preset: str, top_n: int, source: str | None):
    """Full morning pre-market workflow: scan + analyze top picks."""
    from tradekit.analysis.indicators import compute_all_indicators
    from tradekit.analysis.levels import find_support_resistance, get_nearest_levels
    from tradekit.analysis.scoring import compute_composite_score
    from tradekit.analysis.volume import add_volume_indicators
    from tradekit.reports.terminal import print_analysis, print_scan_results
    from tradekit.screener.premarket import scan_premarket

    settings = get_settings()
    presets = settings.load_indicator_presets()
    provider = get_provider(source)

    # Step 1: Scan
    console.print("[bold]Step 1: Pre-Market Scan[/bold]")
    scan_df = scan_premarket(settings=settings, preset=preset, provider=provider)
    print_scan_results(scan_df)

    if scan_df.empty:
        console.print("[yellow]No candidates found. Check back closer to market open.[/yellow]")
        return

    # Step 2: Analyze top picks
    tickers = scan_df["ticker"].tolist()[:top_n]
    console.print(f"\n[bold]Step 2: Analyzing Top {len(tickers)} Picks[/bold]")

    for ticker in tickers:
        try:
            quote = provider.get_quote(ticker)
            df = provider.get_history(ticker, period="3mo")
            if df.empty:
                console.print(f"[yellow]  Skipping {ticker} — no data[/yellow]")
                continue

            df = compute_all_indicators(df, presets=presets)
            df = add_volume_indicators(df)
            latest = df.iloc[-1]
            score = compute_composite_score(latest, presets.get("scoring_weights"))

            sr = find_support_resistance(df)
            price = quote.get("price", latest.get("close", 0))
            nearest = get_nearest_levels(price, sr)

            print_analysis(ticker, score, nearest, quote)
        except Exception as e:
            console.print(f"[red]  Error analyzing {ticker}: {e}[/red]")

    console.print("[bold green]Morning workflow complete.[/bold green]")


@cli.command()
@click.option("--preset", default="premarket_gap", help="Screener preset.")
@click.option("--output-dir", default=None, help="Report output directory.")
@source_option
def report(preset: str, output_dir: str | None, source: str | None):
    """Generate and save a daily report."""
    from pathlib import Path

    from tradekit.reports.markdown import generate_daily_report, save_report
    from tradekit.screener.premarket import scan_premarket
    from tradekit.screener.ranking import rank_candidates

    settings = get_settings()
    provider = get_provider(source)

    console.print("[bold]Generating daily report...[/bold]")

    scan_df = scan_premarket(settings=settings, preset=preset, provider=provider)

    ranked_df = None
    if not scan_df.empty:
        tickers = scan_df["ticker"].tolist()[:10]
        console.print(f"  Ranking top {len(tickers)} candidates...")
        presets = settings.load_indicator_presets()
        ranked_df = rank_candidates(
            tickers,
            weights=presets.get("scoring_weights"),
            indicator_presets=presets,
            provider=provider,
        )

    content = generate_daily_report(scan_df, ranked_df)
    out_dir = Path(output_dir) if output_dir else None
    path = save_report(content, output_dir=out_dir)

    console.print(f"[green]Report saved: {path}[/green]")
    console.print(content)


def _collect_regime_data(provider) -> dict:
    """Collect market regime data as a structured dict for reuse."""
    from tradekit.analysis.indicators import compute_all_indicators
    from tradekit.analysis.levels import find_support_resistance, get_nearest_levels

    indices = []
    for sym in ("SPY", "QQQ", "^VIX"):
        display = sym.replace("^", "")
        try:
            quote = provider.get_quote(sym)
            price = quote.get("price", 0)
            prev = quote.get("prev_close", 0)
            chg_pct = ((price - prev) / prev * 100) if prev else 0

            df = provider.get_history(sym, period="1y")
            rsi = 0.0
            sma50 = sma200 = 0.0
            label = "N/A"
            if not df.empty:
                df = compute_all_indicators(df)
                latest = df.iloc[-1]
                rsi = latest.get("rsi", 0)
                sma50 = latest.get("sma_50", 0)
                sma200 = latest.get("sma_200", 0)

                if display == "VIX":
                    if price < 15:
                        label = "Low"
                    elif price < 20:
                        label = "Normal"
                    elif price < 25:
                        label = "Elevated"
                    elif price < 30:
                        label = "High"
                    else:
                        label = "Extreme"
                else:
                    if price > sma50 and price > sma200:
                        label = "Uptrend"
                    elif price > sma200:
                        label = "Pullback"
                    else:
                        label = "Downtrend"

            indices.append(
                {
                    "symbol": display,
                    "price": price,
                    "change_pct": round(chg_pct, 2),
                    "rsi": round(rsi, 1),
                    "label": label,
                }
            )
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", sym, e)
            indices.append(
                {
                    "symbol": display,
                    "price": 0,
                    "change_pct": 0,
                    "rsi": 0,
                    "label": "Error",
                }
            )

    # SPY key levels
    spy_levels = {"resistance": [], "support": []}
    try:
        spy_df = provider.get_history("SPY", period="3mo")
        if not spy_df.empty:
            sr = find_support_resistance(spy_df)
            spy_price = provider.get_quote("SPY").get("price", spy_df["close"].iloc[-1])
            spy_levels = get_nearest_levels(spy_price, sr, n=3)
    except Exception as e:
        logger.warning("Failed to compute SPY levels: %s", e)

    # Sector breadth
    sector_etfs = {
        "XLK": "Tech",
        "XLF": "Financials",
        "XLE": "Energy",
        "XLV": "Health",
        "XLI": "Industrials",
        "XLC": "Comms",
        "XLY": "Cons Disc",
        "XLP": "Cons Staples",
        "XLU": "Utilities",
        "XLRE": "Real Estate",
        "XLB": "Materials",
    }
    green_count = 0
    sector_data = []
    for etf, name in sector_etfs.items():
        try:
            q = provider.get_quote(etf)
            price = q.get("price", 0)
            prev = q.get("prev_close", 0)
            chg = ((price - prev) / prev * 100) if prev else 0
            sector_data.append((etf, name, chg))
            if chg >= 0:
                green_count += 1
        except Exception:
            sector_data.append((etf, name, 0.0))

    sector_data.sort(key=lambda x: x[2], reverse=True)
    total = len(sector_etfs)
    pct_green = green_count / total * 100 if total else 0

    # Energy futures
    energy_futures_map = {
        "BZ=F": "Brent Crude",
        "CL=F": "WTI Crude",
        "NG=F": "Natural Gas",
    }
    energy_futures = []
    for sym, name in energy_futures_map.items():
        try:
            q = provider.get_quote(sym)
            price = q.get("price", 0)
            prev = q.get("prev_close", 0)
            chg_pct = ((price - prev) / prev * 100) if prev else 0

            df = provider.get_history(sym, period="3mo")
            rsi = 0.0
            label = "N/A"
            if not df.empty:
                df = compute_all_indicators(df)
                latest = df.iloc[-1]
                rsi = latest.get("rsi", 0)
                sma50 = latest.get("sma_50", 0)
                sma200 = latest.get("sma_200", 0)
                if price > sma50 and price > sma200:
                    label = "Uptrend"
                elif price > sma200:
                    label = "Pullback"
                else:
                    label = "Downtrend"

            energy_futures.append(
                {
                    "symbol": name,
                    "price": price,
                    "change_pct": round(chg_pct, 2),
                    "rsi": round(rsi, 1),
                    "label": label,
                }
            )
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", sym, e)
            energy_futures.append(
                {
                    "symbol": name,
                    "price": 0,
                    "change_pct": 0,
                    "rsi": 0,
                    "label": "Error",
                }
            )

    return {
        "indices": indices,
        "spy_levels": spy_levels,
        "energy_futures": energy_futures,
        "sector_breadth": {
            "green": green_count,
            "total": total,
            "pct_green": round(pct_green, 0),
            "strongest": sector_data[0] if sector_data else None,
            "weakest": sector_data[-1] if sector_data else None,
            "sectors": sector_data,
        },
    }


def _build_news_rows(
    news_items: list[dict],
    provider,
    top_n: int = 4,
    yt_context: dict | None = None,
) -> list[dict]:
    """Convert Finviz news items into game plan table rows, ranked by avg volume.

    Only the top_n tickers by average daily volume are included.
    StockedUp context (yt_context) enriches notes when a ticker is mentioned in the video.
    """
    from tradekit.analysis.levels import find_support_resistance, get_nearest_levels

    yt_ticker_notes = (yt_context or {}).get("ticker_notes", {})

    # Group headlines by ticker (skip futures symbols like @PL, @DY)
    ticker_news: dict[str, list[str]] = {}
    ticker_sentiment: dict[str, str] = {}
    for item in news_items:
        for t in item.get("tickers", []):
            if t.startswith("@") or len(t) > 5:
                continue
            ticker_news.setdefault(t, []).append(item.get("headline", ""))
            sent = item.get("sentiment", "NEUT")
            if sent != "NEUT" or t not in ticker_sentiment:
                ticker_sentiment[t] = sent

    # Fetch avg volume for each ticker, filter out OTC/untradable, then rank
    min_price = 5.0
    min_avg_vol = 500_000
    ticker_vol: list[tuple[str, int]] = []
    for ticker in ticker_news:
        try:
            quote = provider.get_quote(ticker)
            price = quote.get("price", 0) or 0
            avg_vol = quote.get("avg_volume", 0) or 0
            if price < min_price or avg_vol < min_avg_vol:
                continue
            ticker_vol.append((ticker, avg_vol))
        except Exception:
            pass

    ticker_vol.sort(key=lambda x: x[1], reverse=True)
    top_tickers = [t for t, _ in ticker_vol[:top_n]]

    # Build rows for the top tickers, enriched with levels
    rows = []
    for ticker in top_tickers:
        headlines = ticker_news[ticker]
        support_str = ""
        resistance_str = ""
        try:
            df = provider.get_history(ticker, period="3mo")
            if not df.empty:
                sr = find_support_resistance(df)
                quote = provider.get_quote(ticker)
                price = quote.get("price", df["close"].iloc[-1])
                nearest = get_nearest_levels(price, sr, n=3)
                support_str = ", ".join(f"{s['level']:.2f}" for s in nearest.get("support", []))
                resistance_str = ", ".join(f"{r['level']:.2f}" for r in nearest.get("resistance", []))
        except Exception as e:
            logger.warning("Failed levels for %s: %s", ticker, e)

        # Combine Finviz headline with StockedUp insight if available
        note = headlines[0][:80] if headlines else ""
        yt_note = yt_ticker_notes.get(ticker, "")
        if yt_note:
            note = f"{note} | StockedUp: {yt_note}"

        rows.append(
            {
                "ticker": ticker,
                "support": support_str,
                "resistance": resistance_str,
                "inflexion": "",
                "notes": note,
                "bias": ticker_sentiment.get(ticker, "NEUT"),
                "setup": "",
                "trading_plan": "",
            }
        )

    return rows


def _build_second_day_rows(
    second_day_df,
    provider,
    yt_context: dict | None = None,
) -> list[dict]:
    """Convert second-day DataFrame into game plan table rows with levels."""
    from tradekit.analysis.levels import find_support_resistance, get_nearest_levels

    yt_ticker_notes = (yt_context or {}).get("ticker_notes", {})

    if second_day_df is None or second_day_df.empty:
        return []

    rows = []
    for _, row in second_day_df.iterrows():
        ticker = row["ticker"]
        support_str = ""
        resistance_str = ""
        try:
            df = provider.get_history(ticker, period="3mo")
            if not df.empty:
                sr = find_support_resistance(df)
                price = row.get("price", df["close"].iloc[-1])
                nearest = get_nearest_levels(price, sr, n=3)
                support_str = ", ".join(f"{s['level']:.2f}" for s in nearest.get("support", []))
                resistance_str = ", ".join(f"{r['level']:.2f}" for r in nearest.get("resistance", []))
        except Exception as e:
            logger.warning("Failed levels for %s: %s", ticker, e)

        chg = row.get("prev_change_pct", 0)
        pre_gap = row.get("pre_gap_pct", 0)
        notes_parts = []
        if chg:
            notes_parts.append(f"prev {chg:+.1f}%")
        vol_ratio = row.get("volume_ratio", 0)
        if vol_ratio:
            notes_parts.append(f"vol {vol_ratio:.1f}x")
        if pre_gap:
            notes_parts.append(f"pre-mkt {pre_gap:+.1f}%")

        yt_note = yt_ticker_notes.get(ticker, "")
        if yt_note:
            notes_parts.append(f"StockedUp: {yt_note}")

        bias = "BULL" if chg > 0 and pre_gap > 0 else "BEAR" if chg < 0 and pre_gap < 0 else "NEUT"

        rows.append(
            {
                "ticker": ticker,
                "support": support_str,
                "resistance": resistance_str,
                "inflexion": "",
                "notes": ", ".join(notes_parts),
                "bias": bias,
                "setup": "",
                "trading_plan": "",
            }
        )

    return rows


_STOCKED_UP_CHANNEL = "https://www.youtube.com/channel/UC-m6zNItyoDk5lSykDlhE4Q"
_FABRIC_MODEL = "us.amazon.nova-lite-v1:0"
_FABRIC_VENDOR = "Bedrock"


def _fetch_stocked_up_context() -> dict:
    """Fetch latest StockedUp video via yt-dlp + fabric extract_wisdom.

    Returns dict with:
        summary: str — market summary from the video
        ticker_notes: dict[str, str] — ticker -> relevant insight from the video
    Returns empty dict on failure.
    """
    import re
    import shutil
    import subprocess

    yt_dlp = shutil.which("yt-dlp")
    fabric = shutil.which("fabric")
    if not yt_dlp or not fabric:
        logger.warning("yt-dlp or fabric not found in PATH — skipping StockedUp")
        return {}

    # Get latest video URL
    try:
        result = subprocess.run(
            [
                yt_dlp,
                "--flat-playlist",
                "--print",
                "%(url)s\t%(title)s",
                "--playlist-items",
                "1",
                f"{_STOCKED_UP_CHANNEL}/videos",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.warning("yt-dlp failed to get latest video: %s", result.stderr[:200])
            return {}

        line = result.stdout.strip().split("\n")[0]
        parts = line.split("\t", 1)
        video_url = parts[0]
        video_title = parts[1] if len(parts) > 1 else "StockedUp Latest"
    except Exception as e:
        logger.warning("Failed to get StockedUp video: %s", e)
        return {}

    # Run fabric extract_wisdom on the video
    try:
        logger.debug(
            "Running: %s -y %s -p extract_wisdom -m %s -V %s",
            fabric,
            video_url,
            _FABRIC_MODEL,
            _FABRIC_VENDOR,
        )
        result = subprocess.run(
            [
                fabric,
                "-y",
                video_url,
                "-p",
                "extract_wisdom",
                "-m",
                _FABRIC_MODEL,
                "-V",
                _FABRIC_VENDOR,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        logger.debug(
            "fabric rc=%d stdout_len=%d stderr_len=%d",
            result.returncode,
            len(result.stdout),
            len(result.stderr),
        )
        if not result.stdout.strip():
            logger.warning("fabric returned no output: %s", result.stderr[:300])
            return {}

        output = result.stdout.strip()
    except Exception as e:
        logger.warning("fabric extract_wisdom failed: %s", e)
        return {}

    # Parse fabric output into sections
    sections: dict[str, str] = {}
    current_section = ""
    current_content: list[str] = []

    for fline in output.split("\n"):
        if re.match(r"^#{1,4}\s+\S", fline):
            if current_section and current_content:
                sections[current_section] = "\n".join(current_content).strip()
            current_section = fline.lstrip("#").strip().rstrip(":")
            current_content = []
        else:
            current_content.append(fline)

    if current_section and current_content:
        sections[current_section] = "\n".join(current_content).strip()

    # Extract summary for Big Picture
    summary = sections.get("SUMMARY", "")
    if video_title:
        summary = f"[StockedUp: {video_title}] {summary}"

    # Extract ticker mentions from REFERENCES, IDEAS, FACTS, RECOMMENDATIONS
    # Match 1-5 char uppercase words that look like tickers
    ticker_pattern = re.compile(r"\b([A-Z]{1,5})\b")
    # Common words to exclude
    _NOT_TICKERS = {
        "THE",
        "AND",
        "FOR",
        "ARE",
        "BUT",
        "NOT",
        "YOU",
        "ALL",
        "CAN",
        "HAS",
        "HER",
        "WAS",
        "ONE",
        "OUR",
        "OUT",
        "DAY",
        "GET",
        "HIS",
        "HOW",
        "ITS",
        "MAY",
        "NEW",
        "NOW",
        "OLD",
        "SEE",
        "WAY",
        "WHO",
        "DID",
        "LET",
        "SAY",
        "SHE",
        "TOO",
        "USE",
        "WITH",
        "THAT",
        "THIS",
        "FROM",
        "THEY",
        "BEEN",
        "HAVE",
        "MANY",
        "SOME",
        "THEM",
        "THAN",
        "EACH",
        "MAKE",
        "LIKE",
        "LONG",
        "LOOK",
        "MUCH",
        "THEN",
        "WHAT",
        "WHEN",
        "WILL",
        "MORE",
        "ALSO",
        "BACK",
        "BEEN",
        "CALL",
        "COME",
        "ONLY",
        "OVER",
        "SUCH",
        "TAKE",
        "YEAR",
        "YOUR",
        "HIGH",
        "LOW",
        "BUY",
        "SELL",
        "PUT",
        "ETF",
        "IPO",
        "CEO",
        "GDP",
        "FED",
        "SEC",
        "LNG",
        "ATH",
        "ATR",
        "RSI",
        "EPS",
        "P&L",
        "SUMMARY",
        "IDEAS",
        "FACTS",
        "QUOTES",
        "HABITS",
        "REFERENCES",
        "RECOMMENDATIONS",
        "INSIGHTS",
    }

    ticker_notes: dict[str, str] = {}
    for section_name in ("REFERENCES", "IDEAS", "FACTS", "RECOMMENDATIONS"):
        content = sections.get(section_name, "")
        for bullet_line in content.split("\n"):
            bullet_line = bullet_line.strip()
            if not bullet_line:
                continue
            found_tickers = [t for t in ticker_pattern.findall(bullet_line) if t not in _NOT_TICKERS and len(t) >= 2]
            for t in found_tickers:
                # Keep the first mention as the note
                if t not in ticker_notes:
                    # Clean the bullet prefix
                    note = re.sub(r"^[-•*\d.)\s]+", "", bullet_line).strip()
                    ticker_notes[t] = note[:80]

    return {"summary": summary, "ticker_notes": ticker_notes}


@cli.command()
@click.option("--no-open", is_flag=True, help="Don't auto-open HTML in browser.")
@click.option("--no-youtube", is_flag=True, help="Skip StockedUp YouTube integration.")
@source_option
def gameplan(no_open: bool, no_youtube: bool, source: str | None):
    """Generate an SMB-style HTML game plan dashboard."""
    import webbrowser
    from datetime import timedelta

    from tradekit.data.finviz import FinvizProvider, load_news, save_news
    from tradekit.reports.html import generate_gameplan_html, save_gameplan_html
    from tradekit.screener.premarket import scan_premarket, scan_previous_movers

    settings = get_settings()
    provider = get_provider(source)
    today = now_et().date()
    yesterday = today - timedelta(days=1)
    # Skip weekends
    if yesterday.weekday() == 6:  # Sunday
        yesterday -= timedelta(days=2)
    elif yesterday.weekday() == 5:  # Saturday
        yesterday -= timedelta(days=1)

    # --- Step 1: Regime ---
    console.print("[bold]Step 1/7: Market Regime[/bold]")
    regime_data = _collect_regime_data(provider)
    console.print("  [green]Done[/green]")

    # --- Step 2: News ---
    console.print("[bold]Step 2/7: News[/bold]")
    finviz_prov = FinvizProvider()
    today_news = finviz_prov.get_market_news(api_key=settings.data.finviz_api_key)
    if today_news:
        save_news(today_news)
        console.print(f"  [green]Fetched & saved {len(today_news)} news items[/green]")
    else:
        console.print("  [yellow]No news (check FINVIZ_API_KEY)[/yellow]")

    yesterday_news = load_news(yesterday)
    if yesterday_news:
        console.print(f"  [dim]Loaded {len(yesterday_news)} items from {yesterday}[/dim]")

    all_news = today_news + yesterday_news

    # --- Step 3: StockedUp YouTube ---
    yt_context: dict = {}
    if not no_youtube:
        console.print("[bold]Step 3/7: StockedUp YouTube[/bold]")
        yt_context = _fetch_stocked_up_context()
        if yt_context:
            n_tickers = len(yt_context.get("ticker_notes", {}))
            console.print(f"  [green]Summary loaded, {n_tickers} ticker mentions extracted[/green]")
        else:
            console.print("  [yellow]No StockedUp content available[/yellow]")
    else:
        console.print("[bold]Step 3/7: StockedUp YouTube[/bold] [dim]skipped[/dim]")

    # --- Step 4: Second-day scan ---
    console.print("[bold]Step 4/7: Second-Day Plays[/bold]")
    second_day_df = scan_previous_movers(provider=provider)
    n_2d = len(second_day_df) if not second_day_df.empty else 0
    console.print(f"  [green]{n_2d} candidates[/green]")

    # --- Step 5: Pre-market scan ---
    console.print("[bold]Step 5/7: Pre-Market Scan[/bold]")
    scan_df = scan_premarket(settings=settings, provider=provider)
    n_scan = len(scan_df) if not scan_df.empty else 0
    console.print(f"  [green]{n_scan} candidates[/green]")

    # --- Step 6: Build enriched rows with levels ---
    console.print("[bold]Step 6/7: Computing Levels[/bold]")

    # Fresh news rows: top 4 tickers by avg volume from news
    fresh_news_rows = _build_news_rows(all_news, provider, yt_context=yt_context)

    second_day_rows = _build_second_day_rows(second_day_df, provider, yt_context=yt_context)
    console.print(f"  [green]{len(fresh_news_rows)} news rows, {len(second_day_rows)} 2nd-day rows[/green]")

    # --- Step 7: Generate HTML ---
    console.print("[bold]Step 7/7: Generating HTML[/bold]")
    date_display = now_et().strftime("%a, %b %d, %Y")
    html = generate_gameplan_html(
        regime_data=regime_data,
        fresh_news=fresh_news_rows,
        second_day_plays=second_day_rows,
        big_picture_narrative=yt_context.get("summary", ""),
        date_str=date_display,
    )

    path = save_gameplan_html(html, today)
    console.print(f"\n[bold green]Game plan saved: {path}[/bold green]")

    if not no_open:
        webbrowser.open(path.as_uri())
        console.print("[dim]Opened in browser[/dim]")


# Ordered (key, prompt, sensitive) tuples for the init wizard.
_INIT_KEYS: list[tuple[str, str, bool]] = [
    ("FINVIZ_API_KEY", "Finviz Elite API key (optional — enables news + export)", True),
    ("ALPHAVANTAGE_API_KEY", "Alpha Vantage API key (optional — fundamentals)", True),
    ("MASSIVE_API_KEY", "Massive.com API key (optional — tick data)", True),
    ("BACKTEST_ACCESS_KEY", "Backtest S3 access key (optional)", True),
    ("BACKTEST_SECRET_KEY", "Backtest S3 secret key (optional)", True),
    ("SLACK_WEBHOOK_URL", "Slack incoming webhook URL (optional)", False),
]


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict. Preserves only KEY=VALUE lines."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        values[key.strip()] = val.strip()
    return values


def _upsert_env_file(path: Path, updates: dict[str, str]) -> None:
    """Insert or update KEY=VALUE lines in a .env file without destroying comments.

    Creates parent dirs if needed. Appends a `# tradekit` section when adding
    new keys to a file that doesn't already contain them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text().splitlines() if path.exists() else []

    keys_to_update = set(updates.keys())
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in keys_to_update:
                new_lines.append(f"{key}={updates[key]}")
                keys_to_update.discard(key)
                continue
        new_lines.append(line)

    if keys_to_update:
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        new_lines.append("# --- tradekit ---")
        for key in updates:
            if key in keys_to_update:
                new_lines.append(f"{key}={updates[key]}")

    path.write_text("\n".join(new_lines) + "\n")


@cli.command()
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Target .env file (defaults to PAI-aware auto-detect).",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Print detected target and current values, but don't prompt.",
)
def init(env_file: Path | None, non_interactive: bool):
    """Interactive setup wizard — populate the shared .env with API keys.

    Detects your PAI environment and writes to the appropriate shared .env:
      1. $PAI_DIR/.env  (if PAI_DIR is set)
      2. ~/.claude/.env (if that directory exists)
      3. ./.env         (fallback, standalone mode)

    Missing keys are prompted interactively. Existing keys are preserved
    unless you choose to overwrite them.
    """
    target = env_file or shared_env_path()
    target = target.expanduser()

    console.print("[bold]tradekit init[/bold]")
    console.print(f"  Target .env: [cyan]{target}[/cyan]")

    pai_dir = os.environ.get("PAI_DIR")
    if pai_dir:
        expanded = Path(os.path.expandvars(pai_dir)).expanduser()
        if str(expanded) != pai_dir:
            console.print(f"  Detected PAI_DIR: [green]{pai_dir}[/green] → [cyan]{expanded}[/cyan]")
        else:
            console.print(f"  Detected PAI_DIR: [green]{pai_dir}[/green]")
    elif (Path.home() / ".claude").exists():
        console.print("  Detected Claude Code directory: [green]~/.claude[/green]")
    else:
        console.print("  [yellow]No PAI or Claude Code install detected — standalone mode[/yellow]")

    current = _read_env_file(target)
    console.print("\n[bold]Current values[/bold] (masked):")
    for key, _prompt, _sensitive in _INIT_KEYS:
        val = current.get(key, "")
        display = "[dim]unset[/dim]" if not val else f"[green]set[/green] ({len(val)} chars)"
        console.print(f"  {key}: {display}")

    if non_interactive:
        console.print("\n[dim]--non-interactive: skipping prompts.[/dim]")
        return

    console.print("\n[bold]Enter values[/bold] (press Enter to keep current):")
    updates: dict[str, str] = {}
    for key, prompt, sensitive in _INIT_KEYS:
        has_current = bool(current.get(key))
        default_hint = " [keep current]" if has_current else ""
        value = click.prompt(
            f"  {prompt}{default_hint}",
            default="",
            show_default=False,
            hide_input=sensitive,
        )
        if value:
            updates[key] = value

    if not updates:
        console.print("\n[yellow]No changes made.[/yellow]")
        return

    _upsert_env_file(target, updates)
    console.print(f"\n[bold green]✓ Wrote {len(updates)} key(s) to {target}[/bold green]")
    console.print("[dim]Restart any running tradekit processes to pick up the new values.[/dim]")
