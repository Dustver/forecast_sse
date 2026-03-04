"""
Core implementation of Excel-like FORECAST.ETS family for SSE.

This module contains:
- Data preparation (sorting, aggregation, gap handling),
- Seasonality detection (Excel COM exact mode + fallback heuristics),
- Point forecast and helper outputs (trend, intervals, forecast tables).

Important behavior notes:
- In exact mode (`SSE_USE_EXCEL_COM_SEASONALITY=true`) seasonality is taken
  directly from Excel via COM, which is the closest possible match to
  `FORECAST.ETS.SEASONALITY`.
- If COM is unavailable, the module falls back to a conservative
  autocorrelation-based detector.
- In all error/edge cases around seasonality detection, the safe default is `1`
  (Excel semantic: "no seasonality").
"""

import numpy as np
from collections import defaultdict
from typing import List, Tuple, Optional, Union
import logging
import os

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
    """
    Strategy for filling missing timeline points after regularization.

    Values:
    - ZEROS (0): absent periods are treated as zero values.
    - INTERPOLATE (1): absent periods are filled by linear interpolation.
    """
    ZEROS = 0
    INTERPOLATE = 1

class Aggregation:
    """
    Strategy for collapsing duplicate timeline points.

    Numeric codes mirror Excel's optional `aggregation` argument used in
    FORECAST.ETS functions.
    """
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
    aggregation: int = Aggregation.AVERAGE,
    min_valid_points: int = 2
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Prepare time series for model routines.

    Processing steps:
    1. Validate equal lengths for values/timeline.
    2. Drop pairs with missing value or missing timestamp.
    3. Aggregate duplicates on the same timestamp using selected method.
    4. Sort by timeline.
    5. Build a regular grid using median timeline step.
    6. Fill gaps using either interpolation or zeros.

    Notes:
    - This function is intended for model stability, not for exact Excel parity.
      Exact parity for seasonality uses raw inputs through COM.
    - `min_valid_points` allows callers to define strictness for short series.
    """
    if len(values) != len(timeline):
        raise ValueError("Values and timeline must have the same length")

    valid_pairs = [(v, t) for v, t in zip(values, timeline)
                   if v is not None and t is not None
                   and not (isinstance(v, float) and np.isnan(v))
                   and not (isinstance(t, float) and np.isnan(t))]

    if len(valid_pairs) < min_valid_points:
        raise ValueError(f"At least {min_valid_points} valid data points required")

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


def _detect_seasonality_excel_com(
    values: List[Optional[float]],
    timeline: List[float],
    data_completion: int,
    aggregation: int
) -> Optional[int]:
    """
    Detect seasonality by delegating calculation to Microsoft Excel via COM.

    This path gives the highest possible compatibility with Excel formula:
    `FORECAST.ETS.SEASONALITY(values, timeline, [data_completion], [aggregation])`.

    Requirements:
    - Windows OS,
    - Installed Microsoft Excel,
    - `pywin32` package available.

    Returns:
    - `int >= 1` when calculation succeeded,
    - `None` when COM/Excel is unavailable or call failed.

    Implementation detail:
    - Inputs are written into a temporary workbook and passed as worksheet ranges
      to avoid conversion quirks and to mimic normal sheet evaluation behavior.
    """
    if os.name != "nt":
        return None

    try:
        import win32com.client  # type: ignore
    except Exception:
        return None

    app = None
    wb = None
    try:
        app = win32com.client.DispatchEx("Excel.Application")
        app.Visible = False
        app.DisplayAlerts = False
        wb = app.Workbooks.Add()
        ws = wb.Worksheets(1)

        values_list = [None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v) for v in values]
        timeline_list = []
        for t in timeline:
            if t is None:
                timeline_list.append(None)
            elif isinstance(t, np.generic):
                timeline_list.append(t.item())
            else:
                timeline_list.append(t)

        n = len(values_list)
        if n == 0:
            return 1

        # Write data to worksheet for robust COM call.
        for i, (v, t) in enumerate(zip(values_list, timeline_list), start=1):
            if v is None:
                ws.Cells(i, 1).Value = None
            else:
                ws.Cells(i, 1).Value = v
            ws.Cells(i, 2).Value = t

        values_range = ws.Range(ws.Cells(1, 1), ws.Cells(n, 1))
        timeline_range = ws.Range(ws.Cells(1, 2), ws.Cells(n, 2))

        result = app.WorksheetFunction.Forecast_ETS_Seasonality(
            values_range,
            timeline_range,
            int(data_completion),
            int(aggregation)
        )

        seasonality = int(round(float(result)))
        return seasonality if seasonality >= 1 else 1
    except Exception:
        return None
    finally:
        try:
            if wb is not None:
                wb.Close(SaveChanges=False)
        except Exception:
            pass
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass


def _detect_seasonality_autocorrelation(values: np.ndarray, max_period: int = None) -> int:
    """
    Fallback seasonality detector based on autocorrelation.

    This routine is used only when exact Excel COM mode is unavailable.
    It is intentionally conservative and biased to return `1` unless
    periodicity signal is sufficiently strong and consistent.

    High-level logic:
    1. Reject too-short series and invalid search range.
    2. Reject sparse/low-information series (too many zeros, too few events).
    3. Remove linear trend and normalize values.
    4. Compute autocorrelation over candidate lags.
    5. Score periods with domain heuristics and strict thresholds.
    6. Validate consistency across cycles.
    7. Return best period or `1`.
    """
    n = len(values)
    
    if n < 4:
        return 1
    
    if max_period is None:
        max_period = min(n // 2, 365)
    
    max_period = min(max_period, n // 2)
    
    if max_period < 2:
        return 1
    
    # ==========================================
    # Шаг 1: Проверка на слишком много нулей/пустот
    # ==========================================
    non_zero_count = np.sum(values != 0)
    zero_ratio = 1 - (non_zero_count / n)
    
    if zero_ratio > 0.5:  # Более 50% нулей = нет сезонности
        logger.info(f"Too many zeros ({zero_ratio:.1%}), returning 1")
        return 1
    
    if non_zero_count < 8:  # Минимум 8 ненулевых значений
        return 1
    
    # ==========================================
    # Шаг 2: Проверка вариации данных (CV - коэффициент вариации)
    # ==========================================
    mean_val = np.mean(values[values != 0])
    std_val = np.std(values[values != 0])
    
    if mean_val > 0:
        cv = std_val / mean_val
        if cv < 0.1:  # Слишком мало вариации = нет сезонности
            logger.info(f"Low variation (CV={cv:.3f}), returning 1")
            return 1
    
    # ==========================================
    # Шаг 3: Detrending (линейный тренд)
    # ==========================================
    x = np.arange(n)
    coeffs = np.polyfit(x, values, 1)
    trend = np.polyval(coeffs, x)
    detrended = values - trend
    
    # ==========================================
    # Шаг 4: Нормализация
    # ==========================================
    std = np.std(detrended)
    if std < 1e-10:
        return 1
    detrended = (detrended - np.mean(detrended)) / std
    
    # ==========================================
    # Шаг 5: Расчёт автокорреляции
    # ==========================================
    autocorr = np.zeros(max_period + 1)
    
    for lag in range(1, max_period + 1):
        if n - lag > 0:
            c = np.corrcoef(detrended[:-lag], detrended[lag:])[0, 1]
            if not np.isnan(c):
                autocorr[lag] = c
    
    # ==========================================
    # Шаг 6: Поиск пиков автокорреляции
    # ==========================================
    peaks = []
    
    for i in range(2, max_period):
        # Пик: больше обоих соседей
        if autocorr[i] > autocorr[i-1] and autocorr[i] > autocorr[i+1]:
            peaks.append((i, autocorr[i]))
    
    # ==========================================
    # Шаг 7: Оценка кандидатов с ОЧЕНЬ строгими порогами
    # ==========================================
    
    # Предпочтительные периоды для месячных данных
    preferred_periods = {2, 3, 4, 6, 8, 9, 10, 11, 12, 24}
    
    candidates = []
    
    for period in range(2, max_period + 1):
        if n < period * 2:
            continue
        
        corr = autocorr[period]
        
        # ★★★ СТРОГИЙ ПОРОГ: минимум 0.15 для не-предпочтительных, 0.1 для предпочтительных
        min_corr = 0.10 if period in preferred_periods else 0.15
        
        if corr < min_corr:
            continue
        
        score = corr
        
        # Бонус 1: Период делит длину данных нацело (24 месяца)
        if n % period == 0:
            score *= 1.5  # Уменьшил с 2.5 до 1.5
        
        # Бонус 2: Предпочтительный период
        if period in preferred_periods:
            score *= 1.2  # Уменьшил с 1.5 до 1.2
        
        # Бонус 3: Много полных циклов
        cycles = n / period
        if cycles >= 3:
            score *= 1.1  # Уменьшил с 1.2 до 1.1
        
        candidates.append((period, score, corr))
    
    if not candidates:
        logger.info(f"No candidates passed threshold, returning 1")
        return 1
    
    # ==========================================
    # Шаг 8: Выбор лучшего периода
    # ==========================================
    
    # Сортируем по score (убывание)
    candidates.sort(key=lambda x: x[1], reverse=True)
    
    best_period = candidates[0][0]
    best_score = candidates[0][1]
    best_corr = candidates[0][2]
    
    # ★★★ ПРОВЕРКА: Если лучшая корреляция слишком низкая - возвращаем 1
    if best_corr < 0.15:
        logger.info(f"Best correlation too low ({best_corr:.3f}), returning 1")
        return 1
    
    # Проверка: если меньший период делит n и имеет сравнимую корреляцию
    for period, score, corr in candidates[1:]:
        if period < best_period and n % period == 0:
            if corr > 0.6 * best_corr:  # Увеличил с 0.4 до 0.6
                best_period = period
                best_corr = corr
                break
    
    # ★★★ ПРОВЕРКА КОНСИСТЕНТНОСТИ: Проверяем что паттерн повторяется
    if n >= best_period * 2:
        # Сравниваем первые два цикла
        cycle1 = values[:best_period]
        cycle2 = values[best_period:best_period*2]
        
        if len(cycle1) == len(cycle2):
            cycle_corr = np.corrcoef(cycle1, cycle2)[0, 1]
            if not np.isnan(cycle_corr) and cycle_corr < 0.1:
                logger.info(f"Low cycle consistency ({cycle_corr:.3f}), returning 1")
                return 1
    
    logger.info(f"Seasonality detected: {best_period} (score={best_score:.3f}, corr={best_corr:.3f})")
    return best_period




def _detect_seasonality_statsmodels(values: np.ndarray) -> int:
    """
    Alternative seasonality detector via `statsmodels` decomposition.

    This function is currently auxiliary and not used in the exact Excel path.
    It is preserved as an optional fallback strategy for experimentation.
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
    Excel-compatible seasonality detection entry point.

    Priority:
    1. Try exact Excel COM evaluation on raw inputs (if enabled/available).
    2. Fall back to internal autocorrelation detector on prepared series.

    Input semantics are aligned to Excel:
    - `values`: historical series.
    - `timeline`: matching timeline points (numeric or date-like in COM path).
    - `data_completion`: 0 zeros, 1 interpolation.
    - `aggregation`: duplicate timestamp aggregator.

    Returns:
    - Seasonal period as integer (`1` means no seasonality).

    Robustness:
    - Function never raises for user-data issues; it returns `1` on errors.
      This matches SSE aggregate-friendly behavior.
    """
    try:
        if values is None:
            return 1

        raw_values = list(values)
        if timeline is None:
            raw_timeline = [float(i) for i in range(len(raw_values))]
        else:
            raw_timeline = list(timeline)

        if len(raw_values) != len(raw_timeline):
            logger.error("Values and timeline must have the same length")
            return 1

        # Try exact Excel behavior first, if available, with raw inputs.
        use_excel_com = os.environ.get("SSE_USE_EXCEL_COM_SEASONALITY", "true").lower() == "true"
        if use_excel_com:
            excel_period = _detect_seasonality_excel_com(
                raw_values, raw_timeline, data_completion, aggregation
            )
            if excel_period is not None:
                logger.info(
                    "Detected seasonality via Excel COM: %s (data points: %s)",
                    excel_period, len(raw_values)
                )
                return int(excel_period)

        values_arr = np.array(raw_values, dtype=float)
        timeline_arr = np.array(raw_timeline, dtype=float)
        
        # Подготовка данных (сортировка, агрегация, интерполяция)
        values_clean, timeline_clean = _prepare_data(
            values_arr.tolist(),
            timeline_arr.tolist(),
            data_completion,
            aggregation,
            min_valid_points=1
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
        return 1


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
    Forecast one future point using ETS-style logic.

    Behavior:
    - If `seasonality == 0`, seasonality is auto-detected.
    - If statsmodels is available and applicable, Holt-Winters from
      `statsmodels` is used.
    - Otherwise, internal additive Holt/Holt-Winters fallback is used.

    Returns:
    - Forecast for the specified `horizon` step ahead.
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
    Estimate trend component (slope) for the prepared series.

    If seasonality is present, a simple deseasonalization pass is applied
    before linear regression.

    Returns:
        Trend value (slope per timeline step).
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
    Forecast multiple target points.

    Args:
        values: Historical values.
        timeline: Historical timeline.
        target_timeline: Future points to forecast.
        seasonality: Seasonal period (0 = auto-detect).
        data_completion: Missing data handling mode.
        aggregation: Duplicate handling mode.

    Returns:
        Forecast value per requested target point.
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
    Compute forecast confidence interval.

    Args:
        values: Historical values.
        timeline: Historical timeline.
        target_date: Point to forecast (if None -> next point).
        confidence_level: Confidence level, e.g. 0.95.
        seasonality: Seasonal period.
        data_completion: Missing data handling mode.
        aggregation: Duplicate handling mode.

    Returns:
        Tuple `(lower_bound, upper_bound)`.

    Note:
    - Residual error is estimated from rolling one-step predictions when
      enough history exists; otherwise a simplified approximation is used.
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
    Build a complete fitted+forecast table with auto-detected seasonality.

    This function:
    1. Prepares input series.
    2. Detects seasonality.
    3. Fits internal Holt/Holt-Winters components.
    4. Produces in-sample fitted values and out-of-sample forecasts.

    Args:
        values: Historical series.
        timeline: Corresponding timeline.
        horizon: Number of future steps.
        data_completion: Missing data handling mode.
        aggregation: Duplicate handling mode.

    Returns:
        Dictionary with model metadata and full output table fields.
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
    Return only fitted+forecasted values list.

    This compact wrapper is used by SSE tensor output where only one numeric
    column is required.

    Returns:
        List of fitted historical values followed by forecast horizon values.
    """
    result = forecast_ets_seasonality_table(
        values, timeline, horizon, data_completion, aggregation
    )
    return result['fitted']
