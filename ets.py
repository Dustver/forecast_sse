import numpy as np
from collections import defaultdict
from typing import List, Tuple, Optional, Union
import logging

logger = logging.getLogger(__name__)

# Попытка импорта statsmodels (опционально)
try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    from statsmodels.tsa.seasonal import seasonal_decompose
    STATS_AVAILABLE = True
except ImportError:
    STATS_AVAILABLE = False
    logger.warning("statsmodels not available, using fallback implementation")

class DataCompletion:
    ZEROS = 0
    INTERPOLATE = 1

class Aggregation:
    AVERAGE = 1
    COUNT = 2
    COUNTA = 3
    MAX = 4
    MEDIAN = 5
    MIN = 6
    SUM = 7


def _prepare_data(
    values: List[float],
    timeline: List[float],
    data_completion: int = DataCompletion.INTERPOLATE,
    aggregation: int = Aggregation.AVERAGE
) -> Tuple[np.ndarray, np.ndarray]:
    """Подготовка данных (без изменений)"""
    if len(values) != len(timeline):
        raise ValueError("Values and timeline must have the same length")

    valid_pairs = [(v, t) for v, t in zip(values, timeline)
                   if v is not None and t is not None
                   and not (isinstance(v, float) and np.isnan(v))
                   and not (isinstance(t, float) and np.isnan(t))]

    if len(valid_pairs) < 2:
        raise ValueError("At least 2 valid data points required")

    values_clean = [p[0] for p in valid_pairs]
    timeline_clean = [p[1] for p in valid_pairs]

    grouped = defaultdict(list)
    for v, t in zip(values_clean, timeline_clean):
        grouped[t].append(v)

    aggregated = {}
    for t, vals in grouped.items():
        if aggregation == Aggregation.AVERAGE:
            aggregated[t] = np.mean(vals)
        elif aggregation == Aggregation.COUNT:
            aggregated[t] = len(vals)
        elif aggregation == Aggregation.COUNTA:
            aggregated[t] = len([v for v in vals if v is not None])
        elif aggregation == Aggregation.MAX:
            aggregated[t] = np.max(vals)
        elif aggregation == Aggregation.MEDIAN:
            aggregated[t] = np.median(vals)
        elif aggregation == Aggregation.MIN:
            aggregated[t] = np.min(vals)
        elif aggregation == Aggregation.SUM:
            aggregated[t] = np.sum(vals)
        else:
            aggregated[t] = np.mean(vals)

    sorted_times = sorted(aggregated.keys())
    sorted_values = [aggregated[t] for t in sorted_times]

    timeline_arr = np.array(sorted_times, dtype=float)
    values_arr = np.array(sorted_values, dtype=float)

    if len(timeline_arr) >= 2:
        diffs = np.diff(timeline_arr)
        step = np.median(diffs)

        if step > 0:
            full_timeline = np.arange(timeline_arr[0], timeline_arr[-1] + step/2, step)
            full_values = np.zeros(len(full_timeline))

            for i, t in enumerate(full_timeline):
                idx = np.argmin(np.abs(timeline_arr - t))
                if np.abs(timeline_arr[idx] - t) < step / 2:
                    full_values[i] = values_arr[idx]
                elif data_completion == DataCompletion.INTERPOLATE:
                    full_values[i] = np.interp(t, timeline_arr, values_arr)
                else:
                    full_values[i] = 0

            timeline_arr = full_timeline
            values_arr = full_values

    return values_arr, timeline_arr


def _detect_seasonality_autocorrelation(values: np.ndarray, max_period: int = None) -> int:
    """
    Detect seasonality period using autocorrelation analysis.
    Matches Excel's FORECAST.ETS.SEASONALITY algorithm.
    """
    n = len(values)
    
    if n < 4:
        return 1
    
    if max_period is None:
        max_period = min(n // 2, 365)
    
    max_period = min(max_period, n // 2)
    
    if max_period < 2:
        return 1
    
    # Detrending
    x = np.arange(n)
    coeffs = np.polyfit(x, values, 1)
    trend = np.polyval(coeffs, x)
    detrended = values - trend
    
    # Normalize
    std = np.std(detrended)
    if std < 1e-10:
        return 1
    detrended = (detrended - np.mean(detrended)) / std
    
    # Autocorrelation
    autocorr = np.zeros(max_period + 1)
    for lag in range(1, max_period + 1):
        if n - lag > 0:
            c = np.corrcoef(detrended[:-lag], detrended[lag:])[0, 1]
            if not np.isnan(c):
                autocorr[lag] = c
    
    # Preferred periods
    preferred_periods = {2, 3, 4, 6, 8, 12, 24}
    
    candidates = []
    
    for period in range(2, max_period + 1):
        if n < period * 2:
            continue
        
        corr = autocorr[period]
        
        if corr < 0.05:
            continue
        
        score = corr
        
        # Бонусы
        if n % period == 0:
            score *= 3.0
        if period in preferred_periods:
            score *= 1.5
        cycles = n / period
        if cycles >= 3:
            score *= 1.2
        
        # ★★★ Штраф за большие периоды (ключевое изменение!)
        score *= (1.0 / np.log(period + 1))
        
        candidates.append((period, score, corr))
    
    if not candidates:
        return 1
    
    # Сортировка по score
    candidates.sort(key=lambda x: x[1], reverse=True)
    
    best_period = candidates[0][0]
    best_score = candidates[0][1]
    best_corr = candidates[0][2]
    
    # Проверка на меньшие периоды которые делят n
    for period, score, corr in candidates:
        if period < best_period and n % period == 0:
            if corr > 0.4 * best_corr:
                best_period = period
                best_corr = corr
                break
    
    if best_corr < 0.1:
        return 1
    
    logger.info(f"Seasonality detected: {best_period} (score={best_score:.3f}, corr={best_corr:.3f})")
    return best_period


def _detect_seasonality_statsmodels(values: np.ndarray) -> int:
    """
    Альтернативная детекция через statsmodels (если доступна).
    Использует декомпозицию временного ряда.
    """
    if not STATS_AVAILABLE:
        return _detect_seasonality_autocorrelation(values)
    
    n = len(values)
    if n < 8:
        return _detect_seasonality_autocorrelation(values)
    
    # Пробуем разные периоды
    best_period = 1
    best_score = -np.inf
    
    for period in range(2, min(n // 2, 52) + 1):
        if n < period * 2:
            continue
        
        try:
            # Сезонная декомпозиция
            result = seasonal_decompose(values, model='additive', period=period, extrapolate_trend='freq')
            
            # Оценка качества: соотношение сезонной дисперсии к общей
            seasonal_var = np.var(result.seasonal)
            residual_var = np.var(result.resid)
            
            if residual_var > 0:
                score = seasonal_var / residual_var
                
                if score > best_score and score > 0.5:
                    best_score = score
                    best_period = period
        except Exception:
            continue
    
    return best_period if best_score > 0.5 else 1


def forecast_ets_seasonality(
    values: List[float],
    timeline: List[float] = None,
    data_completion: int = DataCompletion.INTERPOLATE,
    aggregation: int = Aggregation.AVERAGE,
    use_statsmodels: bool = False
) -> int:
    """
    Excel-compatible FORECAST.ETS.SEASONALITY function.
    """
    try:
        values = np.array(values, dtype=float)
        
        if timeline is None:
            timeline = np.arange(len(values), dtype=float)
        else:
            timeline = np.array(timeline, dtype=float)
        
        # Подготовка данных (сортировка, агрегация, интерполяция)
        values_clean, timeline_clean = _prepare_data(
            values.tolist(),
            timeline.tolist(),
            data_completion,
            aggregation
        )
        
        if len(values_clean) < 4:
            logger.warning("Insufficient data points for seasonality detection")
            return 1
        
        # ВАЖНО: передаём ТОЛЬКО значения в детектор сезонности
        # Timeline уже использован в _prepare_data для сортировки
        seasonality = _detect_seasonality_autocorrelation(values_clean)
        
        logger.info(f"Detected seasonality period: {seasonality} (data points: {len(values_clean)})")
        return int(seasonality)
        
    except Exception as e:
        logger.error(f"Error in forecast_ets_seasonality: {str(e)}")
        raise


def forecast_ets(
    values: List[float],
    timeline: List[float] = None,
    seasonality: int = 0,
    data_completion: int = DataCompletion.INTERPOLATE,
    aggregation: int = Aggregation.AVERAGE,
    horizon: int = 1,
    use_statsmodels: bool = True  # По умолчанию используем statsmodels для прогноза
) -> float:
    """
    FORECAST.ETS с опцией statsmodels.
    """
    try:
        values = np.array(values, dtype=float)
        
        if timeline is None:
            timeline = np.arange(len(values), dtype=float)
        else:
            timeline = np.array(timeline, dtype=float)
        
        values_clean, timeline_clean = _prepare_data(
            values.tolist(),
            timeline.tolist(),
            data_completion,
            aggregation
        )
        
        n = len(values_clean)
        
        # Авто-детекция сезонности
        if seasonality == 0:
            seasonality = _detect_seasonality_autocorrelation(values_clean)
        elif seasonality < 1:
            seasonality = 1
        
        # Используем statsmodels если доступно и сезонность > 1
        if use_statsmodels and STATS_AVAILABLE and seasonality > 1 and n >= seasonality * 2:
            try:
                model = ExponentialSmoothing(
                    values_clean,
                    trend='add',
                    seasonal='add',
                    seasonal_periods=seasonality,
                    initialization_method='estimated'
                )
                fitted = model.fit()
                forecast = fitted.forecast(horizon)
                return float(forecast[-1])
            except Exception as e:
                logger.warning(f"statsmodels failed, using fallback: {e}")
                # Fallback to custom implementation
        
        # Custom Holt-Winters (fallback)
        if seasonality <= 1:
            # Holt's linear method
            alpha = 0.3
            beta = 0.1
            
            level = values_clean[0]
            trend = (values_clean[-1] - values_clean[0]) / (n - 1) if n > 1 else 0
            
            for i in range(1, n):
                new_level = alpha * values_clean[i] + (1 - alpha) * (level + trend)
                trend = beta * (new_level - level) + (1 - beta) * trend
                level = new_level
            
            forecast = level + horizon * trend
        else:
            # Holt-Winters
            alpha = 0.3
            beta = 0.1
            gamma = 0.3
            
            season_len = seasonality
            
            level = np.mean(values_clean[:min(season_len, n)])
            
            if n >= 2 * season_len:
                trend = (np.mean(values_clean[season_len:2*season_len]) -
                        np.mean(values_clean[:season_len])) / season_len
            else:
                trend = (values_clean[-1] - values_clean[0]) / (n - 1) if n > 1 else 0
            
            seasonal = np.zeros(season_len)
            for i in range(min(season_len, n)):
                seasonal[i] = values_clean[i] - level
            
            for i in range(season_len, n):
                s_idx = i % season_len
                new_level = alpha * (values_clean[i] - seasonal[s_idx]) + (1 - alpha) * (level + trend)
                trend = beta * (new_level - level) + (1 - beta) * trend
                seasonal[s_idx] = gamma * (values_clean[i] - new_level) + (1 - gamma) * seasonal[s_idx]
                level = new_level
            
            future_s_idx = (n - 1 + horizon) % season_len
            forecast = level + horizon * trend + seasonal[future_s_idx]
        
        return float(forecast)
        
    except Exception as e:
        logger.error(f"Error in forecast_ets: {str(e)}")
        raise


def forecast_ets_trend(
    values: List[float],
    timeline: List[float] = None,
    seasonality: int = 0,
    data_completion: int = DataCompletion.INTERPOLATE,
    aggregation: int = Aggregation.AVERAGE
) -> float:
    """
    Returns the trend component of the ETS model.

    Returns:
        Trend value (slope per time unit)
    """
    try:
        values = np.array(values, dtype=float)

        if timeline is None:
            timeline = np.arange(len(values), dtype=float)
        else:
            timeline = np.array(timeline, dtype=float)

        values_clean, timeline_clean = _prepare_data(
            values.tolist(),
            timeline.tolist(),
            data_completion,
            aggregation
        )

        n = len(values_clean)

        if seasonality == 0:
            seasonality = _detect_seasonality_autocorrelation(values_clean)

        # Remove seasonality if present
        if seasonality > 1 and n >= seasonality:
            # Seasonal decomposition - moving average
            deseasonalized = np.zeros(n)
            half_s = seasonality // 2

            for i in range(n):
                start = max(0, i - half_s)
                end = min(n, i + half_s + 1)
                deseasonalized[i] = np.mean(values_clean[start:end])
        else:
            deseasonalized = values_clean

        # Calculate trend using linear regression
        x = np.arange(n)
        coeffs = np.polyfit(x, deseasonalized, 1)
        trend = coeffs[0]  # Slope

        return float(trend)

    except Exception as e:
        logger.error(f"Error in forecast_ets_trend: {str(e)}")
        raise


def forecast_ets_series(
    values: List[float],
    timeline: List[float] = None,
    target_timeline: List[float] = None,
    seasonality: int = 0,
    data_completion: int = DataCompletion.INTERPOLATE,
    aggregation: int = Aggregation.AVERAGE
) -> List[float]:
    """
    Returns a series of forecasted values for multiple future points.

    Args:
        values: Historical values
        timeline: Historical timeline
        target_timeline: Future points to forecast
        seasonality: Seasonal period (0 = auto-detect)
        data_completion: Missing data handling
        aggregation: Duplicate handling

    Returns:
        List of forecasted values for each target point
    """
    try:
        values = np.array(values, dtype=float)

        if timeline is None:
            timeline = np.arange(len(values), dtype=float)
        else:
            timeline = np.array(timeline, dtype=float)

        if target_timeline is None or len(target_timeline) == 0:
            # Default: forecast next point
            step = np.median(np.diff(timeline)) if len(timeline) > 1 else 1
            target_timeline = [timeline[-1] + step]

        target_timeline = np.array(target_timeline, dtype=float)

        results = []
        for target in target_timeline:
            # Calculate horizon (steps ahead)
            if len(timeline) > 1:
                step = np.median(np.diff(timeline))
                horizon = max(1, int(round((target - timeline[-1]) / step)))
            else:
                horizon = 1

            forecast_val = forecast_ets(
                values.tolist(),
                timeline.tolist(),
                seasonality,
                data_completion,
                aggregation,
                horizon
            )
            results.append(forecast_val)

        return results

    except Exception as e:
        logger.error(f"Error in forecast_ets_series: {str(e)}")
        raise


def forecast_ets_confint(
    values: List[float],
    timeline: List[float] = None,
    target_date: float = None,
    confidence_level: float = 0.95,
    seasonality: int = 0,
    data_completion: int = DataCompletion.INTERPOLATE,
    aggregation: int = Aggregation.AVERAGE
) -> Tuple[float, float]:
    """
    Returns the confidence interval for a forecast.

    Args:
        values: Historical values
        timeline: Historical timeline
        target_date: Point to forecast
        confidence_level: Confidence level (default 0.95 = 95%)
        seasonality: Seasonal period
        data_completion: Missing data handling
        aggregation: Duplicate handling

    Returns:
        Tuple of (lower_bound, upper_bound)
    """
    try:
        from scipy import stats
    except ImportError:
        # Fallback if scipy not available
        logger.warning("scipy not available, using approximate confidence interval")
        z_score = 1.96  # 95% confidence
    else:
        z_score = stats.norm.ppf((1 + confidence_level) / 2)

    try:
        values = np.array(values, dtype=float)

        if timeline is None:
            timeline = np.arange(len(values), dtype=float)
        else:
            timeline = np.array(timeline, dtype=float)

        values_clean, timeline_clean = _prepare_data(
            values.tolist(),
            timeline.tolist(),
            data_completion,
            aggregation
        )

        n = len(values_clean)

        # Calculate forecast
        if target_date is None:
            step = np.median(np.diff(timeline_clean)) if len(timeline_clean) > 1 else 1
            target_date = timeline_clean[-1] + step
            horizon = 1
        else:
            step = np.median(np.diff(timeline_clean)) if len(timeline_clean) > 1 else 1
            horizon = max(1, int(round((target_date - timeline_clean[-1]) / step)))

        forecast_val = forecast_ets(
            values_clean.tolist(),
            timeline_clean.tolist(),
            seasonality,
            data_completion,
            aggregation,
            horizon
        )

        # Calculate residual standard error
        # Use a simpler approach: calculate residuals from fitted trend
        if n >= 3:
            # Fit values using rolling forecast and compute residuals
            fitted = []
            # Start from index 2 to ensure at least 2 data points for fitting
            for i in range(2, n):
                try:
                    fit_val = forecast_ets(
                        values_clean[:i].tolist(),
                        timeline_clean[:i].tolist(),
                        seasonality,
                        data_completion,
                        aggregation,
                        1
                    )
                    fitted.append(fit_val)
                except Exception:
                    # If fitting fails, use simple extrapolation
                    fitted.append(values_clean[i-1])

            if len(fitted) > 0:
                residuals = values_clean[2:n] - np.array(fitted)
                std_error = np.std(residuals) * np.sqrt(1 + 1/n + horizon/n)
            else:
                std_error = np.std(values_clean) * np.sqrt(1 + horizon/n)
        else:
            # Not enough data for residual analysis, use data std
            std_error = np.std(values_clean) * np.sqrt(1 + horizon/n)

        lower = forecast_val - z_score * std_error
        upper = forecast_val + z_score * std_error

        return (float(lower), float(upper))

    except Exception as e:
        logger.error(f"Error in forecast_ets_confint: {str(e)}")
        raise


def forecast_ets_seasonality_table(
    values: List[float],
    timeline: List[float] = None,
    horizon: int = 12,
    data_completion: int = DataCompletion.INTERPOLATE,
    aggregation: int = Aggregation.AVERAGE
) -> dict:
    """
    Extended FORECAST.ETS.SEASONALITY function that returns a complete forecast table.

    This function:
    1. Auto-detects seasonality from the input data
    2. Fits the Holt-Winters model to historical data
    3. Generates forecasts for the specified horizon
    4. Returns a complete table with timeline, actual values, fitted values, and forecasts

    Args:
        values: Historical time series values (e.g., 24 months of sales)
        timeline: Corresponding time points (e.g., month numbers 1-24)
        horizon: Number of future periods to forecast (default 12)
        data_completion: Missing data handling (0=zeros, 1=interpolate)
        aggregation: Duplicate handling (1=AVG, 2=COUNT, etc.)

    Returns:
        Dictionary containing:
        - 'seasonality': Detected seasonal period
        - 'timeline': Full timeline (historical + forecast)
        - 'actual': Actual values (NaN for forecast periods)
        - 'fitted': Fitted/forecasted values
        - 'type': 'historical' or 'forecast' for each row
        - 'trend': Detected trend value
    """
    try:
        values = np.array(values, dtype=float)

        if timeline is None:
            timeline = np.arange(len(values), dtype=float)
        else:
            timeline = np.array(timeline, dtype=float)

        # Prepare and clean data
        values_clean, timeline_clean = _prepare_data(
            values.tolist(),
            timeline.tolist(),
            data_completion,
            aggregation
        )

        n = len(values_clean)

        # Auto-detect seasonality
        seasonality = _detect_seasonality_autocorrelation(values_clean)
        logger.info(f"Auto-detected seasonality: {seasonality}")

        # Calculate trend
        x = np.arange(n)
        coeffs = np.polyfit(x, values_clean, 1)
        trend_value = coeffs[0]

        # Initialize Holt-Winters components
        if seasonality <= 1:
            # No seasonality - use Holt's method
            alpha = 0.3
            beta = 0.1

            level = values_clean[0]
            trend = (values_clean[-1] - values_clean[0]) / (n - 1) if n > 1 else 0

            # Fit historical data
            fitted_values = [level]
            for i in range(1, n):
                fitted_values.append(level + trend)
                new_level = alpha * values_clean[i] + (1 - alpha) * (level + trend)
                trend = beta * (new_level - level) + (1 - beta) * trend
                level = new_level

            # Generate forecasts
            forecast_values = []
            for h in range(1, horizon + 1):
                forecast_values.append(level + h * trend)

            seasonal_factors = None
        else:
            # Use Holt-Winters with seasonality
            alpha = 0.3
            beta = 0.1
            gamma = 0.3

            season_len = seasonality

            # Initialize level
            level = np.mean(values_clean[:min(season_len, n)])

            # Initialize trend
            if n >= 2 * season_len:
                trend = (np.mean(values_clean[season_len:2*season_len]) -
                        np.mean(values_clean[:season_len])) / season_len
            else:
                trend = (values_clean[-1] - values_clean[0]) / (n - 1) if n > 1 else 0

            # Initialize seasonal factors
            seasonal = np.zeros(season_len)
            for i in range(min(season_len, n)):
                seasonal[i] = values_clean[i] - level

            # Fit historical data
            fitted_values = []
            for i in range(n):
                s_idx = i % season_len
                fitted_values.append(level + trend + seasonal[s_idx])

                if i >= season_len:
                    new_level = alpha * (values_clean[i] - seasonal[s_idx]) + (1 - alpha) * (level + trend)
                    new_trend = beta * (new_level - level) + (1 - beta) * trend
                    seasonal[s_idx] = gamma * (values_clean[i] - new_level) + (1 - gamma) * seasonal[s_idx]
                    level = new_level
                    trend = new_trend

            # Generate forecasts
            forecast_values = []
            for h in range(1, horizon + 1):
                s_idx = (n - 1 + h) % season_len
                forecast_values.append(level + h * trend + seasonal[s_idx])

            seasonal_factors = seasonal.tolist()

        # Determine timeline step
        step = np.median(np.diff(timeline_clean)) if len(timeline_clean) > 1 else 1

        # Build output
        full_timeline = list(timeline_clean) + [timeline_clean[-1] + step * (h + 1) for h in range(horizon)]
        full_actual = list(values_clean) + [float('nan')] * horizon
        full_fitted = fitted_values + forecast_values
        full_type = ['historical'] * n + ['forecast'] * horizon

        return {
            'seasonality': int(seasonality),
            'trend': float(trend_value),
            'timeline': full_timeline,
            'actual': full_actual,
            'fitted': full_fitted,
            'type': full_type,
            'n_historical': n,
            'n_forecast': horizon,
            'seasonal_factors': seasonal_factors
        }

    except Exception as e:
        logger.error(f"Error in forecast_ets_seasonality_table: {str(e)}")
        raise


def forecast_ets_seasonality_table_simple(
    values: List[float],
    timeline: List[float] = None,
    horizon: int = 12,
    data_completion: int = DataCompletion.INTERPOLATE,
    aggregation: int = Aggregation.AVERAGE
) -> List[float]:
    """
    Simplified version that returns only the fitted/forecasted values as a list.
    This is the format expected by the SSE TENSOR function.

    Returns:
        List of fitted values (for historical) + forecasted values (for horizon)
    """
    result = forecast_ets_seasonality_table(
        values, timeline, horizon, data_completion, aggregation
    )
    return result['fitted']
