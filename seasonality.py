from ets import forecast_ets_seasonality

def SEASONALITY(
    values,
    timeline=None,
    data_completion=1,
    aggregation=1,
    use_statsmodels=False,
):
    """
    Alias for Excel-compatible FORECAST.ETS.SEASONALITY.
    """
    return forecast_ets_seasonality(
        values=values,
        timeline=timeline,
        data_completion=data_completion,
        aggregation=aggregation,
        use_statsmodels=use_statsmodels
    )
