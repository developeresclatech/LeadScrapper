"""Persistent daily run policy."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ScraperConfig


@dataclass(frozen=True)
class DailyRunPolicy:
    run_number: int
    max_results_per_keyword: int
    page_limit: int
    recency_hours: float | None
    is_catch_up: bool = False

    @property
    def is_first_run(self) -> bool:
        return self.run_number == 1

    @classmethod
    def from_config(
        cls,
        config: ScraperConfig,
        run_number: int,
        hours_since_last_completed: float | None = None,
    ) -> "DailyRunPolicy":
        if config.recent_minutes is not None:
            wider_window = config.recent_minutes > 120
            return cls(
                run_number=run_number,
                max_results_per_keyword=(
                    (config.catch_up_max_results_per_keyword
                     if config.adaptive_daily_limits else config.max_results_per_keyword)
                    if wider_window else min(50, config.max_results_per_keyword)
                ),
                page_limit=config.catch_up_max_pages if wider_window else 1,
                recency_hours=config.recent_minutes / 60,
            )
        if not config.adaptive_daily_limits:
            return cls(
                run_number=run_number,
                max_results_per_keyword=config.max_results_per_keyword,
                page_limit=config.page_limit,
                recency_hours=config.later_daily_run_recency_hours,
            )
        needs_catch_up = (
            config.force_catch_up
            or run_number == 1
            or hours_since_last_completed is None
            or hours_since_last_completed >= config.catch_up_after_hours
        )
        if needs_catch_up:
            return cls(
                run_number=run_number,
                max_results_per_keyword=(
                    config.catch_up_max_results_per_keyword
                ),
                page_limit=config.catch_up_max_pages,
                recency_hours=config.first_daily_run_recency_hours,
                is_catch_up=True,
            )
        return cls(
            run_number=run_number,
            max_results_per_keyword=config.later_daily_run_results,
            page_limit=config.later_daily_run_pages,
            recency_hours=config.later_daily_run_recency_hours,
        )
