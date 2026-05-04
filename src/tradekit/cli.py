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
    type=click.Choice(["yahoo", "massive", "finviz", "backtest"], case_sensitive=False),
    default=None,
    help="Data source: yahoo (default), massive, finviz, or backtest.",
)


def get_provider(source: str | None = None):
    """Return the appropriate data provider based on source name."""
    settings = get_settings()
    source = source or settings.data_source
    if source == "massive":
        from tradekit.data.massive import MassiveProvider

        return MassiveProvider()
    elif source == "finviz":
        from tradekit.data.finviz_elite import FinvizEliteProvider

        return FinvizEliteProvider()
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


@cli.command()
@click.option("--group", "group_type", type=click.Choice(["sector", "industry", "country", "all"]),
              default="all", help="Which group(s) to pull.")
@click.option("--top", default=12, help="Top/bottom N for industry leaders/laggards.")
@click.option("--push-notion", is_flag=True, help="Mirror summary to Notion TeamJaDaDa hub.")
@click.option("--save", is_flag=True, default=True, help="Save full data to ~/market_data/groups_<DATE>.json.")
@click.option("--diff", "diff_against", default=None, metavar="DATE_OR_AUTO",
              help="Compare today vs prior snapshot. Pass 'auto' for most-recent prior file, or YYYY-MM-DD.")
@click.option("--diff-metric", default="perf_w", help="Metric to diff: perf_w, perf_m, perf_q, perf_ytd.")
def groups(group_type: str, top: int, push_notion: bool, save: bool,
           diff_against: str | None, diff_metric: str):
    """Pull Finviz Elite group performance — sector/industry/country rotation snapshot.

    Sector view shows 11 GICS-style sectors. Industry view has ~144 sub-industries.
    Country view shows international rotation. All include weekly/monthly/quarterly/YTD
    performance with relative volume — perfect weekly-review input.
    """
    import json
    from datetime import datetime
    from pathlib import Path

    from tradekit.data.finviz_elite import FinvizEliteProvider

    fp = FinvizEliteProvider()
    targets = ["sector", "industry", "country"] if group_type == "all" else [group_type]
    out: dict[str, list[dict]] = {}

    for g in targets:
        console.print(f"[bold]Fetching {g} groups...[/bold]")
        df = fp.get_group(g)
        out[g] = df.to_dict("records")

        if g == "sector":
            console.print(f"\n[bold cyan]SECTORS — ranked by weekly performance[/bold cyan]")
            ranked = df.sort_values("perf_w", ascending=False)
            for r in ranked.itertuples():
                style = "green" if r.perf_w > 0 else "red"
                console.print(
                    f"  [{style}]{r.perf_w:>+7.2f}%[/{style}] "
                    f"W   {r.perf_m:>+6.2f}% M   {r.perf_q:>+6.2f}% Q   {r.perf_ytd:>+7.2f}% YTD   "
                    f"RVol {r.rvol:.2f}   {r.name} ({r.stocks} stocks)"
                )

        elif g == "industry":
            console.print(f"\n[bold green]TOP {top} INDUSTRIES — by weekly performance[/bold green]")
            top_df = df.sort_values("perf_w", ascending=False).head(top)
            for r in top_df.itertuples():
                console.print(
                    f"  [green]{r.perf_w:>+7.2f}%[/green] W   "
                    f"{r.perf_m:>+6.2f}% M   {r.perf_ytd:>+7.2f}% YTD   "
                    f"RVol {r.rvol:.2f}   {r.name} (#{r.stocks})"
                )
            console.print(f"\n[bold red]BOTTOM {top} INDUSTRIES — by weekly performance[/bold red]")
            bot_df = df.sort_values("perf_w", ascending=True).head(top)
            for r in bot_df.itertuples():
                console.print(
                    f"  [red]{r.perf_w:>+7.2f}%[/red] W   "
                    f"{r.perf_m:>+6.2f}% M   {r.perf_ytd:>+7.2f}% YTD   "
                    f"RVol {r.rvol:.2f}   {r.name} (#{r.stocks})"
                )

        elif g == "country":
            console.print(f"\n[bold cyan]COUNTRIES — top 10 by weekly[/bold cyan]")
            for r in df.sort_values("perf_w", ascending=False).head(10).itertuples():
                style = "green" if r.perf_w > 0 else "red"
                console.print(
                    f"  [{style}]{r.perf_w:>+7.2f}%[/{style}] W   "
                    f"{r.perf_m:>+6.2f}% M   {r.perf_ytd:>+7.2f}% YTD   "
                    f"{r.name} ({r.stocks} stocks)"
                )

    # Save snapshot
    today_iso = datetime.now().strftime("%Y-%m-%d")
    if save:
        out_path = Path.home() / "market_data" / f"groups_{today_iso}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out, indent=2, default=str))
        console.print(f"\n[dim]Saved: {out_path}[/dim]")

    # Diff against a prior snapshot
    if diff_against:
        from tradekit.data.finviz_elite import diff_groups

        market_data = Path.home() / "market_data"
        if diff_against == "auto":
            files = sorted(market_data.glob("groups_*.json"))
            files = [f for f in files if today_iso not in f.name]
            if not files:
                console.print("[yellow]No prior snapshots found for --diff auto[/yellow]")
            else:
                prior_path = files[-1]
                console.print(f"\n[bold magenta]DIFF vs {prior_path.name}[/bold magenta]")
                prior = json.loads(prior_path.read_text())
                _print_diff(out, prior, diff_metric, top)
        else:
            prior_path = market_data / f"groups_{diff_against}.json"
            if not prior_path.exists():
                console.print(f"[yellow]No snapshot at {prior_path}[/yellow]")
            else:
                console.print(f"\n[bold magenta]DIFF vs {prior_path.name}[/bold magenta]")
                prior = json.loads(prior_path.read_text())
                _print_diff(out, prior, diff_metric, top)

    # Notion push
    if push_notion:
        try:
            _push_groups_to_notion(out)
            console.print("[green]Pushed to Notion ✓[/green]")
        except Exception as e:
            console.print(f"[yellow]Notion push failed: {e}[/yellow]")


def _push_groups_to_notion(data: dict[str, list[dict]]) -> None:
    """Build a markdown summary and push to the TeamJaDaDa Document Hub."""
    from datetime import datetime
    import subprocess
    import json as _json

    date = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# Group Rotation Snapshot — {date}\n"]

    if "sector" in data:
        lines.append("## Sectors (ranked weekly)\n")
        lines.append("| Sector | Week | Month | Quarter | YTD | RVol | # |")
        lines.append("|--------|------|-------|---------|-----|------|---|")
        for r in sorted(data["sector"], key=lambda x: -x["perf_w"]):
            lines.append(
                f"| {r['name']} | {r['perf_w']:+.2f}% | {r['perf_m']:+.2f}% | {r['perf_q']:+.2f}% | "
                f"{r['perf_ytd']:+.2f}% | {r['rvol']:.2f} | {r['stocks']} |"
            )
        lines.append("")

    if "industry" in data:
        lines.append("## Top 12 Industries (weekly)\n")
        lines.append("| Industry | Week | Month | YTD | RVol | # |")
        lines.append("|----------|------|-------|-----|------|---|")
        for r in sorted(data["industry"], key=lambda x: -x["perf_w"])[:12]:
            lines.append(
                f"| {r['name']} | {r['perf_w']:+.2f}% | {r['perf_m']:+.2f}% | "
                f"{r['perf_ytd']:+.2f}% | {r['rvol']:.2f} | {r['stocks']} |"
            )
        lines.append("\n## Bottom 12 Industries (weekly)\n")
        lines.append("| Industry | Week | Month | YTD | RVol | # |")
        lines.append("|----------|------|-------|-----|------|---|")
        for r in sorted(data["industry"], key=lambda x: x["perf_w"])[:12]:
            lines.append(
                f"| {r['name']} | {r['perf_w']:+.2f}% | {r['perf_m']:+.2f}% | "
                f"{r['perf_ytd']:+.2f}% | {r['rvol']:.2f} | {r['stocks']} |"
            )

    content = "\n".join(lines)

    # Use the Notion MCP via Claude CLI (parent has the auth)
    cmd = [
        "claude", "--print", "--model", "haiku",
        "--allowed-tools", "mcp__claude_ai_Notion__notion-create-pages",
        "--system-prompt", (
            "You have access to the Notion MCP. Create ONE page in the TeamJaDaDa Document Hub "
            "(data_source_id: 2d0973b9-8fae-80f1-860c-000b23ffd54e) with the markdown content "
            "from the user. Properties: 'Doc name' = the H1 from the markdown, 'Document Type' = 'Watchlist', "
            "'date:Date:start' = today's ISO date. Reply with just the page URL on success."
        ),
    ]
    subprocess.run(cmd, input=content, text=True, timeout=60)


@cli.command("weekly-review")
@click.option("--tickers", default="", help="Comma-separated watchlist tickers (e.g. 'AAPL,MSFT,NVDA').")
@click.option("--tickers-file", type=click.Path(exists=True),
              help="Path to a newline-delimited ticker file.")
@click.option("--from-falcon", is_flag=True, default=False,
              help="Pull tickers from the Falcon screener DB (~/.falcon/falcon.db).")
@click.option("--falcon-strategy", default=None,
              help="Filter Falcon DB by strategy name (e.g. 'momentum_long'). Default: all strategies.")
@click.option("--falcon-mode", type=click.Choice(["top", "recent"]), default="top",
              help="Falcon source: 'top' = most-frequent symbols over window, 'recent' = last N runs.")
@click.option("--falcon-days", default=7, help="Lookback days for --falcon-mode top.")
@click.option("--falcon-limit", default=20, help="Max symbols to pull from Falcon.")
@click.option("--falcon-db", type=click.Path(), default=None,
              help="Override path to falcon.db (default: ~/.falcon/falcon.db).")
@click.option("--with-debates", "n_debates", default=0,
              help="Run bull/bear debates on top-N tickers by |RS spread|. 0 = skip.")
@click.option("--debate-level", type=click.Choice(["fast", "standard", "smart"]), default="fast",
              help="Inference level for debates if --with-debates > 0.")
@click.option("--out-dir", type=click.Path(),
              default=None, help="Output dir (default: ~/.claude/MEMORY/WORK/<DATE>_weekly-review/).")
@click.option("--push-notion", is_flag=True,
              help="After generating, also push the markdown to Notion. Skip if you want to edit first.")
def weekly_review(tickers: str, tickers_file: str | None,
                  from_falcon: bool, falcon_strategy: str | None,
                  falcon_mode: str, falcon_days: int, falcon_limit: int,
                  falcon_db: str | None,
                  n_debates: int,
                  debate_level: str, out_dir: str | None, push_notion: bool):
    """Generate the full weekly trading review as editable markdown.

    Composes: sector/industry rotation (with W-over-W diff), watchlist RS spreads,
    optional bull/bear debates on top RS leaders. Writes a single REVIEW.md to
    ~/.claude/MEMORY/WORK/<DATE>_weekly-review/ — edit it freely, then re-run
    with --push-notion (or use 'tradekit publish-review' separately).
    """
    import asyncio
    import json
    from datetime import datetime
    from pathlib import Path

    from tradekit.data.finviz_elite import FinvizEliteProvider, compute_rs_spread, diff_groups

    # Resolve tickers — priority: --from-falcon > --tickers-file > --tickers > default
    ticker_list: list[str] = []
    ticker_source = "default"
    if from_falcon:
        from tradekit.data.falcon import FalconReader, FalconDBNotFound, DEFAULT_DB_PATH
        try:
            reader = FalconReader(db_path=Path(falcon_db) if falcon_db else DEFAULT_DB_PATH)
            if falcon_mode == "top":
                top = reader.get_top_symbols(
                    strategy_name=falcon_strategy,
                    days_back=falcon_days,
                    limit=falcon_limit,
                )
                ticker_list = [sym for sym, _count in top]
                console.print(
                    f"[bold]Falcon: pulled {len(ticker_list)} top symbols "
                    f"(strategy={falcon_strategy or 'all'}, last {falcon_days}d)[/bold]"
                )
                if top:
                    counts_str = ", ".join(f"{s}({n})" for s, n in top[:10])
                    console.print(f"  [dim]top frequency: {counts_str}[/dim]")
            else:
                ticker_list = reader.get_recent_run_symbols(
                    strategy_name=falcon_strategy,
                    n_runs=3,
                    max_symbols=falcon_limit,
                )
                console.print(
                    f"[bold]Falcon: pulled {len(ticker_list)} symbols "
                    f"from last 3 runs (strategy={falcon_strategy or 'all'})[/bold]"
                )
            ticker_source = f"falcon ({falcon_mode})"
        except FalconDBNotFound as e:
            console.print(f"[red]{e}[/red]")
            return
        if not ticker_list:
            console.print("[yellow]Falcon DB had no matching symbols. Falling back to defaults.[/yellow]")
    if not ticker_list and tickers_file:
        ticker_list = [t.strip().upper() for t in Path(tickers_file).read_text().splitlines() if t.strip()]
        ticker_source = f"file:{tickers_file}"
    if not ticker_list and tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        ticker_source = "--tickers"
    if not ticker_list:
        ticker_list = ["AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "META", "AMZN", "TSLA", "AVGO", "PLTR"]
        console.print("[yellow]No tickers provided; using default mega-cap watchlist.[/yellow]")

    today_iso = datetime.now().strftime("%Y-%m-%d")
    out_path = Path(out_dir) if out_dir else (
        Path.home() / ".claude" / "MEMORY" / "WORK" / f"{today_iso.replace('-', '')}_weekly-review"
    )
    out_path.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Generating weekly review → {out_path}[/bold]")

    # 1) Groups (sector + industry + country) + diff
    fp = FinvizEliteProvider()
    console.print("  [1/4] Fetching group rotation data...")
    groups_data = {
        "sector": fp.get_group("sector").to_dict("records"),
        "industry": fp.get_group("industry").to_dict("records"),
        "country": fp.get_group("country").to_dict("records"),
    }
    snap_path = Path.home() / "market_data" / f"groups_{today_iso}.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(json.dumps(groups_data, indent=2, default=str))

    market_data = Path.home() / "market_data"
    prior_files = sorted([f for f in market_data.glob("groups_*.json") if today_iso not in f.name])
    sector_diff: list[dict] = []
    industry_diff: list[dict] = []
    diff_label = ""
    if prior_files:
        prior_data = json.loads(prior_files[-1].read_text())
        diff_label = prior_files[-1].stem.replace("groups_", "")
        sector_diff = diff_groups(groups_data, prior_data, "sector", "perf_w", 99)
        industry_diff = diff_groups(groups_data, prior_data, "industry", "perf_w", 99)

    # 2) RS spread for watchlist
    console.print(f"  [2/4] Computing RS spreads for {len(ticker_list)} tickers...")
    quotes_df = fp.get_quotes(ticker_list, cols=[
        "ticker", "company", "industry", "perf_w", "perf_m", "price",
        "ah_close", "ah_change", "rsi", "atr",
    ])
    rs_df = compute_rs_spread(quotes_df, groups_data["industry"])

    # 3) Optional debates on top RS movers
    debates: list[dict] = []
    if n_debates > 0 and not rs_df.empty:
        from tradekit.agents.debate import bull_bear_debate

        console.print(f"  [3/4] Running bull/bear debates on top {n_debates} RS movers ({debate_level})...")
        top_rs = rs_df.head(n_debates)["ticker"].tolist()

        def build_ctx(tk: str) -> dict | None:
            try:
                q = fp.get_quote(tk)
                if not q:
                    return None
                hist = fp.get_history(tk, period="1mo")
                recent = hist.tail(5).reset_index().to_dict("records") if not hist.empty else []
                rs_row = rs_df[rs_df["ticker"] == tk].iloc[0]
                return {
                    "current_price": q.get("price"),
                    "ah_price": q.get("ah_price"),
                    "ah_change_pct": q.get("ah_change"),
                    "industry": q.get("industry"),
                    "ticker_perf_week": float(rs_row["ticker_w"]),
                    "industry_perf_week": float(rs_row["industry_w"]),
                    "rs_spread_pp": float(rs_row["rs_spread"]),
                    "atr": q.get("atr"),
                    "rsi": q.get("rsi"),
                    "recent_5_bars": recent,
                    "setup_note": "Weekly review RS-leader debate — high |spread| means idiosyncratic move not industry beta.",
                }
            except Exception as e:
                console.print(f"    [yellow]ctx err for {tk}: {e}[/yellow]")
                return None

        async def run_all():
            tasks = []
            for tk in top_rs:
                ctx = build_ctx(tk)
                if ctx:
                    tasks.append((tk, ctx, bull_bear_debate(tk, ctx, level=debate_level, persist=True)))
            results = []
            for tk, ctx, coro in tasks:
                try:
                    res = await coro
                    results.append((tk, res))
                except Exception as e:
                    console.print(f"    [yellow]debate err {tk}: {e}[/yellow]")
            return results

        debate_results = asyncio.run(run_all())
        for tk, res in debate_results:
            j = res.judge
            debates.append({
                "ticker": tk,
                "verdict": j.verdict if j else "ERR",
                "confidence": j.confidence if j else 0.0,
                "stronger": j.stronger_side if j else "n/a",
                "rationale": j.rationale if j else (res.error or ""),
                "key_levels": j.key_levels if j else {},
                "bull_thesis": (res.bull.parsed or {}).get("thesis", "") if res.bull.parsed else "",
                "bear_thesis": (res.bear.parsed or {}).get("thesis", "") if res.bear.parsed else "",
            })

    # 4) Render markdown
    console.print("  [4/4] Rendering REVIEW.md...")
    md = _render_review_md(today_iso, groups_data, sector_diff, industry_diff,
                           diff_label, rs_df, debates)
    review_path = out_path / "REVIEW.md"
    review_path.write_text(md)
    console.print(f"\n[bold green]✓ Review written to:[/bold green] {review_path}")
    console.print(f"[dim]Edit freely, then run: tradekit publish-review {review_path}[/dim]")

    if push_notion:
        _publish_review_to_notion(md, today_iso)
        console.print("[green]✓ Pushed to Notion[/green]")


@cli.command("publish-review")
@click.argument("review_path", type=click.Path(exists=True))
def publish_review(review_path: str):
    """Push an existing REVIEW.md (possibly edited) to Notion."""
    from datetime import datetime
    from pathlib import Path
    md = Path(review_path).read_text()
    today_iso = datetime.now().strftime("%Y-%m-%d")
    _publish_review_to_notion(md, today_iso)
    console.print(f"[green]✓ Published {review_path} to Notion[/green]")


def _render_review_md(date_iso: str, groups_data: dict,
                       sector_diff: list[dict], industry_diff: list[dict],
                       diff_label: str,
                       rs_df, debates: list[dict]) -> str:
    """Pure markdown renderer — keep all formatting in one place so it's easy to tune."""
    out: list[str] = []
    out.append(f"# Weekly Trading Review — {date_iso}\n")
    out.append("> Generated by `tradekit weekly-review`. Edit freely. Push with "
               "`tradekit publish-review REVIEW.md` when ready.\n")
    out.append("---\n")

    # Headline read (you fill this in)
    out.append("## Headline Read\n")
    out.append("_TODO: 2-3 sentences capturing the week's tape. Edit before publishing._\n")
    out.append("---\n")

    # Sectors
    out.append("## Sector Rotation\n")
    out.append("| Sector | Week | Month | Quarter | YTD | RVol | # |")
    out.append("|--------|-----:|------:|--------:|----:|-----:|--:|")
    for r in sorted(groups_data["sector"], key=lambda x: -x["perf_w"]):
        out.append(
            f"| {r['name']} | {r['perf_w']:+.2f}% | {r['perf_m']:+.2f}% | "
            f"{r['perf_q']:+.2f}% | {r['perf_ytd']:+.2f}% | {r['rvol']:.2f} | {r['stocks']} |"
        )
    out.append("")

    if sector_diff:
        out.append(f"### Sector W-over-W Δ (vs {diff_label})\n")
        out.append("| Sector | Now | Was | Δpp |")
        out.append("|--------|----:|----:|----:|")
        for d in sector_diff:
            arrow = "▲" if d["delta"] > 0 else "▼"
            out.append(f"| {d['name']} | {d['today']:+.2f}% | {d['prior']:+.2f}% | {arrow} {d['delta']:+.2f} |")
        out.append("")

    out.append("---\n")

    # Industries
    out.append("## Industry Leaders & Laggards\n")
    industries = sorted(groups_data["industry"], key=lambda x: -x["perf_w"])
    out.append("### Top 12 (weekly)\n")
    out.append("| Industry | Week | Month | YTD | RVol | # |")
    out.append("|----------|-----:|------:|----:|-----:|--:|")
    for r in industries[:12]:
        out.append(
            f"| {r['name']} | {r['perf_w']:+.2f}% | {r['perf_m']:+.2f}% | "
            f"{r['perf_ytd']:+.2f}% | {r['rvol']:.2f} | {r['stocks']} |"
        )
    out.append("\n### Bottom 12 (weekly)\n")
    out.append("| Industry | Week | Month | YTD | RVol | # |")
    out.append("|----------|-----:|------:|----:|-----:|--:|")
    for r in industries[-12:]:
        out.append(
            f"| {r['name']} | {r['perf_w']:+.2f}% | {r['perf_m']:+.2f}% | "
            f"{r['perf_ytd']:+.2f}% | {r['rvol']:.2f} | {r['stocks']} |"
        )
    out.append("")

    if industry_diff:
        out.append(f"### Industry W-over-W movers (vs {diff_label})\n")
        out.append("**Top improvers:**\n")
        out.append("| Industry | Now | Was | Δpp |")
        out.append("|----------|----:|----:|----:|")
        for d in industry_diff[:8]:
            out.append(f"| {d['name']} | {d['today']:+.2f}% | {d['prior']:+.2f}% | ▲ {d['delta']:+.2f} |")
        out.append("\n**Top deteriorating:**\n")
        out.append("| Industry | Now | Was | Δpp |")
        out.append("|----------|----:|----:|----:|")
        for d in industry_diff[-8:][::-1]:
            out.append(f"| {d['name']} | {d['today']:+.2f}% | {d['prior']:+.2f}% | ▼ {d['delta']:+.2f} |")
        out.append("")

    out.append("---\n")

    # Watchlist RS
    if not rs_df.empty:
        out.append("## Watchlist — Relative Strength Spread\n")
        out.append("Spread = ticker weekly perf − industry weekly perf. "
                   "**>+10pp = idiosyncratic strength**, **<-10pp = idiosyncratic weakness**.\n")
        out.append("| Ticker | Industry | Tkr W | Ind W | Spread |")
        out.append("|--------|----------|------:|------:|-------:|")
        for r in rs_df.itertuples():
            out.append(
                f"| {r.ticker} | {r.industry} | {r.ticker_w:+.2f}% | "
                f"{r.industry_w:+.2f}% | **{r.rs_spread:+.2f}pp** |"
            )
        out.append("")
        out.append("---\n")

    # Debates
    if debates:
        out.append("## Bull/Bear Debates — Top RS Leaders\n")
        for d in debates:
            badge = {"long": "🟢", "short": "🔴", "skip": "⚪", "ERR": "❌"}.get(d["verdict"], "⚪")
            out.append(f"### {badge} {d['ticker']} — {d['verdict'].upper()} (conf {d['confidence']:.2f}, stronger {d['stronger']})\n")
            out.append(f"**Bull:** {d['bull_thesis']}\n")
            out.append(f"**Bear:** {d['bear_thesis']}\n")
            out.append(f"**Judge:** {d['rationale']}\n")
            kl = d.get("key_levels") or {}
            if any(kl.values()):
                out.append(f"**Levels:** entry {kl.get('entry')} / stop {kl.get('stop')} / target {kl.get('target')}\n")
            out.append("")
        out.append("---\n")

    # Action plan placeholder
    out.append("## Action Plan for Monday\n")
    out.append("_TODO: distill the above into 3-5 actionable bullets. Edit freely._\n\n")
    out.append("- \n- \n- \n")
    out.append("\n---\n")
    out.append("## Risk-Off Triggers\n")
    out.append("_TODO: SPY breakdown level, VIX threshold, NFP miss, etc._\n")

    return "\n".join(out)


def _publish_review_to_notion(md: str, date_iso: str) -> None:
    """Push markdown to TeamJaDaDa Document Hub via the Notion MCP."""
    import subprocess
    cmd = [
        "claude", "--print", "--model", "haiku",
        "--allowed-tools", "mcp__claude_ai_Notion__notion-create-pages",
        "--system-prompt", (
            "You have access to the Notion MCP. Create ONE page in the TeamJaDaDa Document Hub "
            "(data_source_id: 2d0973b9-8fae-80f1-860c-000b23ffd54e). Use the H1 from the markdown "
            "as 'Doc name', set 'Document Type' = 'Watchlist', set 'date:Date:start' to today's "
            "ISO date. Reply with the page URL."
        ),
    ]
    subprocess.run(cmd, input=md, text=True, timeout=90)


def _print_diff(today: dict, prior: dict, metric: str, top_n: int) -> None:
    """Print W-over-W deltas for sectors and industries."""
    from tradekit.data.finviz_elite import diff_groups
    label = {"perf_w": "Week", "perf_m": "Month", "perf_q": "Quarter", "perf_ytd": "YTD"}.get(metric, metric)

    if "sector" in today and "sector" in prior:
        console.print(f"\n[bold green]SECTOR {label}-perf improvement (today vs prior)[/bold green]")
        deltas = diff_groups(today, prior, "sector", metric, top_n)
        for d in deltas:
            arrow = "▲" if d["delta"] > 0 else "▼"
            style = "green" if d["delta"] > 0 else "red"
            console.print(
                f"  [{style}]{arrow} {d['delta']:>+6.2f}pp[/{style}]   "
                f"{d['name']:<25} now {d['today']:>+6.2f}%  was {d['prior']:>+6.2f}%"
            )
    if "industry" in today and "industry" in prior:
        deltas = diff_groups(today, prior, "industry", metric, top_n)
        console.print(f"\n[bold green]TOP {top_n} INDUSTRY {label}-perf IMPROVERS[/bold green]")
        for d in deltas[:top_n]:
            console.print(
                f"  [green]▲ {d['delta']:>+6.2f}pp[/green]   "
                f"{d['name']:<42} now {d['today']:>+6.2f}%  was {d['prior']:>+6.2f}%"
            )
        console.print(f"\n[bold red]TOP {top_n} INDUSTRY {label}-perf DETERIORATING[/bold red]")
        for d in deltas[-top_n:][::-1]:
            console.print(
                f"  [red]▼ {d['delta']:>+6.2f}pp[/red]   "
                f"{d['name']:<42} now {d['today']:>+6.2f}%  was {d['prior']:>+6.2f}%"
            )


@cli.command()
@click.argument("tickers", nargs=-1, required=True)
@click.option("--top", default=20, help="Show top N by absolute spread.")
def rs(tickers: tuple[str, ...], top: int):
    """Compute relative-strength spread for tickers vs their industry's weekly perf.

    A high positive spread = ticker is leading its industry (idiosyncratic strength).
    A high negative spread = ticker is dragging its industry (idiosyncratic weakness).
    Both are signal — high-conviction trades happen at the extremes.

    Uses today's industry snapshot (live-fetches if missing).
    """
    import json
    from datetime import datetime
    from pathlib import Path

    from tradekit.data.finviz_elite import FinvizEliteProvider, compute_rs_spread

    fp = FinvizEliteProvider()

    # Get today's industry snapshot (cached or live)
    today_iso = datetime.now().strftime("%Y-%m-%d")
    snap_path = Path.home() / "market_data" / f"groups_{today_iso}.json"
    if snap_path.exists():
        groups_data = json.loads(snap_path.read_text())
        if "industry" not in groups_data:
            console.print("[bold]Cached snapshot missing industry, fetching...[/bold]")
            groups_data["industry"] = fp.get_group("industry").to_dict("records")
    else:
        console.print("[bold]Fetching industry snapshot...[/bold]")
        groups_data = {"industry": fp.get_group("industry").to_dict("records")}

    console.print(f"[bold]Pulling quotes for {len(tickers)} tickers...[/bold]")
    quotes_df = fp.get_quotes(list(tickers), cols=[
        "ticker", "company", "industry", "perf_w", "price", "ah_close", "ah_change",
    ])

    rs_df = compute_rs_spread(quotes_df, groups_data["industry"])
    if rs_df.empty:
        console.print("[red]No RS data computed — check ticker symbols[/red]")
        return

    console.print(f"\n[bold cyan]RELATIVE STRENGTH SPREAD — week, ranked by |spread|[/bold cyan]")
    console.print(f"  {'TICKER':<7}{'INDUSTRY':<42}{'TKR_W':>9}{'IND_W':>9}{'SPREAD':>10}")
    console.print("  " + "-" * 77)
    for r in rs_df.head(top).itertuples():
        style = "green" if r.rs_spread > 0 else "red"
        console.print(
            f"  {r.ticker:<7}{r.industry[:40]:<42}"
            f"[{style}]{r.ticker_w:>+8.2f}%[/{style}]"
            f"{r.industry_w:>+8.2f}%"
            f"[bold {style}]{r.rs_spread:>+9.2f}pp[/bold {style}]"
        )

    console.print(f"\n[dim]Tip: spread > +10pp = idiosyncratic strength | spread < -10pp = idiosyncratic weakness[/dim]")


@cli.command()
@click.argument("ticker")
@click.option("--period", default="1mo", help="History period for context.")
@click.option("--level", type=click.Choice(["fast", "standard", "smart"]), default="standard",
              help="Inference level: fast=haiku, standard=sonnet, smart=opus.")
@click.option("--no-persist", is_flag=True, help="Skip writing transcript to disk.")
@source_option
def debate(ticker: str, period: str, level: str, no_persist: bool, source: str | None):
    """Run a bull/bear LLM debate on a ticker — TradingAgents-style decision layer.

    Fetches real context (price, ATR, RVOL, levels, recent OHLC) from the configured
    data provider, then runs parallel bull/bear analyst agents and a judge agent.
    Persists full transcript to ~/market_data/debates/ unless --no-persist.
    """
    import asyncio

    from tradekit.agents.debate import bull_bear_debate
    from tradekit.analysis.indicators import compute_all_indicators
    from tradekit.analysis.levels import find_support_resistance, get_nearest_levels
    from tradekit.analysis.volume import add_volume_indicators

    settings = get_settings()
    presets = settings.load_indicator_presets()
    provider = get_provider(source)
    ticker = ticker.upper()

    console.print(f"[bold]Building context for {ticker}...[/bold]")
    quote = provider.get_quote(ticker)
    df = provider.get_history(ticker, period=period)
    if df.empty:
        console.print(f"[red]No data for {ticker}[/red]")
        return

    df = compute_all_indicators(df, presets=presets)
    df = add_volume_indicators(df)
    latest = df.iloc[-1]
    sr_levels = find_support_resistance(df)
    current_price = quote.get("price", float(latest.get("close", 0)))
    nearest = get_nearest_levels(current_price, sr_levels)

    recent_bars = df.tail(5)[["open", "high", "low", "close", "volume"]].to_dict("records")

    context = {
        "current_price": current_price,
        "session": _market_session(),
        "atr_14": float(latest.get("atr") or 0),
        "atr_pct": float(latest.get("atr_pct") or 0),
        "rvol": float(latest.get("relative_volume") or 0),
        "vwap": float(latest.get("vwap") or 0),
        "rsi_14": float(latest.get("rsi") or 0),
        "macd_histogram": float(latest.get("macd_histogram") or 0),
        "stoch_k": float(latest.get("stoch_k") or 0),
        "nearest_support": nearest.get("support"),
        "nearest_resistance": nearest.get("resistance"),
        "recent_5_bars": recent_bars,
        "premarket": provider.get_premarket(ticker) if hasattr(provider, "get_premarket") else None,
    }

    console.print(f"[bold]Running bull/bear debate ({level})...[/bold]")
    result = asyncio.run(bull_bear_debate(ticker, context, level=level, persist=not no_persist))

    if result.error:
        console.print(f"[red]Debate failed: {result.error}[/red]")
        if result.bull.parsed:
            console.print(f"[yellow]Bull case (raw):[/yellow] {result.bull.parsed}")
        if result.bear.parsed:
            console.print(f"[yellow]Bear case (raw):[/yellow] {result.bear.parsed}")
        return

    bull_p = result.bull.parsed or {}
    bear_p = result.bear.parsed or {}
    j = result.judge

    console.print()
    console.print(f"[bold green]BULL ({result.bull.latency_ms}ms, conviction {bull_p.get('conviction', 'n/a')}):[/bold green]")
    console.print(f"  Thesis: {bull_p.get('thesis', '')}")
    for e in bull_p.get("evidence", []):
        console.print(f"  - {e}")
    console.print(f"  Invalidation: {bull_p.get('invalidation', '')}")

    console.print()
    console.print(f"[bold red]BEAR ({result.bear.latency_ms}ms, conviction {bear_p.get('conviction', 'n/a')}):[/bold red]")
    console.print(f"  Thesis: {bear_p.get('thesis', '')}")
    for e in bear_p.get("evidence", []):
        console.print(f"  - {e}")
    console.print(f"  Invalidation: {bear_p.get('invalidation', '')}")

    if j:
        console.print()
        verdict_style = {"long": "bold green", "short": "bold red", "skip": "yellow"}.get(j.verdict, "white")
        console.print(f"[{verdict_style}]VERDICT: {j.verdict.upper()}  (confidence {j.confidence:.2f}, stronger side: {j.stronger_side})[/{verdict_style}]")
        console.print(f"  Rationale: {j.rationale}")
        kl = j.key_levels or {}
        console.print(f"  Entry {kl.get('entry')}  /  Stop {kl.get('stop')}  /  Target {kl.get('target')}")
        console.print(f"  [dim]judge latency {j.latency_ms}ms[/dim]")

    if not no_persist:
        console.print(f"\n[dim]Transcript saved to ~/market_data/debates/[/dim]")
