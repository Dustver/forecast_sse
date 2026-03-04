"""
Compatibility wrapper for legacy `SEASONALITY` SSE function name.

Historically the project exposed both:
- `SEASONALITY(...)`
- `FORECAST_ETS_SEASONALITY(...)`

To avoid divergence and maintenance drift, this module forwards all calls to
the single canonical implementation in `ets.forecast_ets_seasonality`.
"""

from ets import forecast_ets_seasonality

def SEASONALITY(
    values,
    timeline=None,
    data_completion=1,
    aggregation=1,
    use_statsmodels=False,
):
    """
    Legacy alias of `FORECAST.ETS.SEASONALITY`.

    Parameters are passed through unchanged:
    - `values`: source series,
    - `timeline`: timeline points,
    - `data_completion`: missing-period handling mode,
    - `aggregation`: duplicate-timestamp aggregation mode.

    Returns:
    - Integer seasonal period where `1` means "no seasonality".

    Notes:
    - Exact Excel parity is delegated to the core function in `ets.py`.
    - Keeping this thin wrapper ensures both SSE names always behave identically.
    """
    return forecast_ets_seasonality(
        values=values,
        timeline=timeline,
        data_completion=data_completion,
        aggregation=aggregation,
        use_statsmodels=use_statsmodels
    )
