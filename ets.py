"""
Lightweight seasonality-only module for SSE.

Retains Excel-like FORECAST.ETS.SEASONALITY behavior without Excel COM
or forecasting extras. Defaults to monthly timelines (1st of each month)
and fills missing months with zeros unless explicitly disabled.

Arguments (Excel-ish order used by SSE wiring):
1) values              - list of numeric values
2) timeline            - list of dates/serials/strings (optional; defaults to sequential months)
3) fill_missing        - bool or 0/1, fill gaps with zeros (default True)
4) aggregation         - numeric code for duplicate timestamps (default AVERAGE)
5) start_date          - optional lower bound for monthly grid
6) end_date            - optional upper bound for monthly grid
"""

from __future__ import annotations

import datetime as _dt
from typing import List, Optional, Iterable
import numpy as np
import pandas as pd

try:
    from statsmodels.tsa.stattools import acf as sm_acf  # type: ignore
    STATS_AVAILABLE = True
except Exception:
    STATS_AVAILABLE = False


class Aggregation:
    AVERAGE = 1
    COUNT = 2
    COUNTA = 3
    MAX = 4
    MEDIAN = 5
    MIN = 6
    SUM = 7


def _parse_timestamp(ts) -> _dt.date:
    """Convert supported timestamp formats to a month-start date."""
    if ts is None:
        raise ValueError("Timeline contains None")

    if isinstance(ts, _dt.datetime):
        return ts.date().replace(day=1)
    if isinstance(ts, _dt.date):
        return ts.replace(day=1)
    if isinstance(ts, np.datetime64):
        dt = np.datetime_as_string(ts, unit="D")
        return _dt.date.fromisoformat(dt).replace(day=1)
    if isinstance(ts, str):
        # Prefer ISO formats (e.g., 2024-01-31) first, then fallback to dd.mm.yyyy.
        try:
            return _dt.date.fromisoformat(ts[:10]).replace(day=1)
        except ValueError:
            return _dt.datetime.strptime(ts[:10], "%d.%m.%Y").date().replace(day=1)
    if isinstance(ts, (int, float)):
        # Heuristic: Excel serial date vs yyyymm integer.
        if ts > 200000:  # treat as yyyymm (e.g., 202401)
            year = int(ts) // 100
            month = int(ts) % 100
            return _dt.date(year, month, 1)
        base = _dt.date(1899, 12, 30)  # Excel serial day 1
        return (base + _dt.timedelta(days=float(ts))).replace(day=1)

    raise TypeError(f"Unsupported timeline type: {type(ts)}")


def _add_month(d: _dt.date) -> _dt.date:
    year = d.year + (d.month // 12)
    month = 1 if d.month == 12 else d.month + 1
    return _dt.date(year, month, 1)


def _aggregate(values: Iterable[float], mode: int) -> float:
    arr = list(values)
    if not arr:
        return 0.0
    if mode == Aggregation.AVERAGE:
        return float(np.mean(arr))
    if mode == Aggregation.COUNT:
        return float(len(arr))
    if mode == Aggregation.COUNTA:
        return float(len([x for x in arr if x is not None]))
    if mode == Aggregation.MAX:
        return float(np.max(arr))
    if mode == Aggregation.MEDIAN:
        return float(np.median(arr))
    if mode == Aggregation.MIN:
        return float(np.min(arr))
    if mode == Aggregation.SUM:
        return float(np.sum(arr))
    return float(np.mean(arr))


def _build_monthly_series(
    values: List[float],
    timeline: Optional[List],
    fill_missing: bool,
    aggregation: int,
    start_date: Optional[_dt.date],
    end_date: Optional[_dt.date],
) -> np.ndarray:
    # Default timeline: sequential months starting 2000-01-01.
    if timeline is None or len(timeline) == 0:
        base = _dt.date(2000, 1, 1)
        timeline = [base]
        for _ in range(1, len(values)):
            timeline.append(_add_month(timeline[-1]))

    if len(timeline) != len(values):
        raise ValueError("Values and timeline must have the same length")

    # Build dataframe and aggregate duplicates by month start.
    records = []
    for v, t in zip(values, timeline):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        m = _parse_timestamp(t)
        records.append((m, float(v)))

    if not records:
        raise ValueError("No valid data points")

    df = pd.DataFrame(records, columns=["month", "value"]).groupby("month")["value"]
    aggregated = {
        Aggregation.AVERAGE: df.mean(),
        Aggregation.COUNT: df.count(),
        Aggregation.COUNTA: df.count(),
        Aggregation.MAX: df.max(),
        Aggregation.MEDIAN: df.median(),
        Aggregation.MIN: df.min(),
        Aggregation.SUM: df.sum(),
    }.get(aggregation, df.mean())

    min_month = _parse_timestamp(start_date) if start_date is not None else aggregated.index.min()
    max_month = _parse_timestamp(end_date) if end_date is not None else aggregated.index.max()

    full_index = pd.date_range(min_month, max_month, freq="MS")
    series = aggregated.reindex(full_index, fill_value=0.0 if fill_missing else np.nan)

    series = series.fillna(0.0) if fill_missing else series.dropna()
    if series.empty:
        raise ValueError("Series empty after aggregation")

    return series.to_numpy(dtype=float)


def _autocorr_best_lag(series: np.ndarray, min_nonzero: int = 4, min_corr: float = 0.3) -> int:
    n = len(series)
    if n < 3:
        return 1

    # Guard against extremely sparse series.
    nonzero_count = int(np.count_nonzero(series))
    if nonzero_count < min_nonzero:
        return 1

    # Detrend to mimic Excel's internal handling.
    x = np.arange(n)
    slope, intercept = np.polyfit(x, series, 1)
    detrended = series - (slope * x + intercept)

    max_lag = min(24, n // 2)
    if STATS_AVAILABLE:
        acfs = sm_acf(detrended, nlags=max_lag, fft=True, missing="drop")
        acf_pairs = [(lag, acfs[lag]) for lag in range(2, max_lag + 1)]
    else:
        acf_pairs = []
        for lag in range(2, max_lag + 1):
            a = detrended[:-lag]
            b = detrended[lag:]
            if a.std() == 0 or b.std() == 0:
                continue
            acf_pairs.append((lag, float(np.corrcoef(a, b)[0, 1])))

    if not acf_pairs:
        return 1

    best_lag, best_corr = max(acf_pairs, key=lambda p: p[1])
    if best_corr <= 0 or best_corr < min_corr:
        return 1
    return best_lag


def forecast_ets_seasonality(
    values: List[float],
    timeline: Optional[List] = None,
    fill_missing: bool = True,
    aggregation: int = Aggregation.AVERAGE,
    start_date: Optional[_dt.date] = None,
    end_date: Optional[_dt.date] = None,
) -> int:
    """Return detected season length (>=1)."""
    series = _build_monthly_series(values, timeline, fill_missing, aggregation, start_date, end_date)
    return int(_autocorr_best_lag(series))


__all__ = [
    "Aggregation",
    "forecast_ets_seasonality",
]
