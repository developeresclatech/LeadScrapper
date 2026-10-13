#!/usr/bin/env python3
"""
Multi-Platform Lead Generator — CLI entry point.

Usage:
    python main.py                                         # all platforms
    python main.py -p upwork freelancer guru               # specific platforms
    python main.py -p upwork vollna -k "python" "django"  # custom keywords
    python main.py -k "web scraping" -r 20                 # 20 results per keyword
    python main.py -k "ai" --format json                   # JSON output
"""

import argparse
import logging
import sys
import time

from rich.console import Console
from rich.logging import RichHandler

from upwork_scraper.config import ScraperConfig, ALL_PLATFORMS
from upwork_scraper.engine import LeadEngine
from upwork_scraper.platforms.upwork_session import UpworkBrowserSession

console = Console()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Collect and qualify leads from Upwork, Vollna, Freelancer, "
            "Guru, and Bark."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
platforms:
  upwork          = Upwork via Wayback Machine (title, budget, skills)
  upwork_selenium = Upwork via Selenium (authenticated, needs Upwork login)
  vollna          = Vollna.com (Upwork client data: rank, spending, rating)
  freelancer      = Freelancer.com (title, budget, skills)
  guru            = Guru.com (title, budget, skills)
  bark            = temporarily disabled
  all             = All platforms (default)

examples:
  python main.py                                         # 10 runs, 30s apart
  python main.py --runs 1                                # one run only
  python main.py --continuous                            # until Ctrl+C
  python main.py -p upwork freelancer                   # Upwork + Freelancer only
  python main.py -p guru -k "python" "django"           # Guru only, custom keywords
  python main.py -k "web scraping" "lead gen" -r 20     # all platforms, 20 each
  python main.py --format json                          # JSON output
        """,
    )
    p.add_argument(
        "-k", "--keywords",
        nargs="+",
        help="Search keywords (default: configured keyword catalog)",
    )
    p.add_argument(
        "-p", "--platforms",
        nargs="+",
        choices=ALL_PLATFORMS + ["all"],
        default=None,
        help="Platforms to scrape (default: all)",
    )
    p.add_argument(
        "-r", "--results",
        type=int,
        default=None,
        help="Override results/keyword and disable adaptive daily limits",
    )
    p.add_argument(
        "--format",
        choices=["csv", "json"],
        default=None,
        help="Output format (default: csv)",
    )
    p.add_argument(
        "--delay",
        nargs=2,
        type=float,
        metavar=("MIN", "MAX"),
        help="Delay range in seconds between requests",
    )
    p.add_argument(
        "--no-adaptive-daily",
        action="store_true",
        help="Disable first/later daily limits and use configured CLI limits",
    )
    p.add_argument(
        "--catch-up",
        action="store_true",
        help="Force only the first session run into 14-hour catch-up mode",
    )
    p.add_argument(
        "--runs",
        type=int,
        default=10,
        metavar="N",
        help="Runs in this session before stopping (default: 10)",
    )
    p.add_argument(
        "--continuous",
        action="store_true",
        help="Run indefinitely until stopped with Ctrl+C",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Wait between completed runs (default: 30)",
    )
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = p.parse_args(argv)
    if args.runs < 1:
        p.error("--runs must be at least 1")
    if args.interval < 0:
        p.error("--interval cannot be negative")
    return args


def build_config(
    args: argparse.Namespace,
    *,
    force_catch_up: bool = False,
) -> ScraperConfig:
    config = ScraperConfig()
    if args.keywords:
        config.keywords = args.keywords
    if args.platforms:
        config.platforms = args.platforms
    if args.results is not None:
        config.max_results_per_keyword = args.results
        config.adaptive_daily_limits = False
    if args.format:
        config.output_format = args.format
    if args.delay:
        config.min_delay, config.max_delay = args.delay
    if args.no_adaptive_daily:
        config.adaptive_daily_limits = False
    if force_catch_up:
        config.force_catch_up = True
        config.adaptive_daily_limits = True
    return config


def run_session(args: argparse.Namespace) -> int:
    browsers = UpworkBrowserSession(build_config(args).upwork_keyword_workers)
    try:
        return _run_session(args, browsers)
    finally:
        browsers.close()


def _run_session(args: argparse.Namespace, browsers) -> int:
    max_runs = None if args.continuous else args.runs
    attempted = 0
    completed = 0

    while max_runs is None or attempted < max_runs:
        attempted += 1
        run_label = (
            f"{attempted}/unlimited"
            if max_runs is None
            else f"{attempted}/{max_runs}"
        )
        console.rule(f"[bold blue]Session run {run_label}")
        engine = None
        try:
            config = build_config(
                args,
                force_catch_up=args.catch_up and attempted == 1,
            )
            engine = LeadEngine(config, upwork_session=browsers)
            engine.run()
            completed += 1
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped manually by user.[/yellow]")
            return 130
        except Exception:
            logging.exception("Session run %d failed.", attempted)
        finally:
            if engine is not None:
                engine.close()

        if max_runs is not None and attempted >= max_runs:
            break

        console.print(
            f"[cyan]Next run starts in {args.interval:g} seconds. "
            "Press Ctrl+C to stop.[/cyan]"
        )
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped manually by user.[/yellow]")
            return 130

    console.print(
        f"[bold green]Session finished: {completed}/{attempted} "
        "runs completed successfully.[/bold green]"
    )
    return 0 if completed else 1


def main():
    args = parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )

    sys.exit(run_session(args))


if __name__ == "__main__":
    main()
