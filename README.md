## Forecast SSE (Qlik Sense)

This project is a Python Server-Side Extension (SSE) that exposes Excel-like `FORECAST.ETS` functions to Qlik Sense via an **Analytics Connection** (gRPC).

### What’s included

- `ExtensionService_forecast.py`: gRPC SSE server
- `FuncDefs_forecast.json`: SSE function definitions (what Qlik sees)
- `ets.py`: forecasting logic (ETS + helpers)

### Install (recommended: venv)

From the project folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

### Run

```powershell
.\.venv\Scripts\python .\ExtensionService_forecast.py --port 50053
```

Or (uses system Python):

```powershell
.\run_forecast_sse.ps1
```

### Qlik Sense: create the Analytics Connection

In QMC (or Qlik Sense Desktop where applicable):

- **Name**: `forecast_sse` (example)
- **Type**: `Server Side Extension`
- **Host**: the machine running the Python service (use `localhost` for same machine)
- **Port**: `50053`
- **Certificates**: this project can run insecure for local testing; for production use TLS certificates.

After creating it, you can call functions as `forecast_sse.<FunctionName>(...)` in measures.

### Алгоритм сезонности (кратко)

Модуль вычисляет сезонность, аналогичную Excel `FORECAST.ETS.SEASONALITY`, но работает с помесячным таймлайном. Он приводит даты к первому числу месяца, агрегирует дубликаты, достраивает непрерывную месячную сетку между `start_date` и `end_date` (или между минимальной и максимальной датой), заполняет пропуски нулями (если `fill_missing=True`), затем выбирает период сезонности по максимуму автокорреляции на лагах 2..min(24, N/2). Если достоверной сезонности нет, возвращает 1.

### Алгоритм сезонности (подробно)

Ниже описание логики из `ets.py`, в том порядке, как она выполняется.

1. Входные данные: `values` (числа), `timeline` (даты/числа/строки, опционально), `fill_missing` (по умолчанию True), `aggregation` (по умолчанию AVERAGE), `start_date` и `end_date` (опционально).
2. Если `timeline` не передан, он создается автоматически: последовательность месяцев начиная с `2000-01-01` длиной `len(values)`.
3. Каждый элемент `timeline` приводится к началу месяца:
   - `datetime` и `date` приводятся к первому числу месяца.
   - `numpy.datetime64` и строки ISO-дат читаются как дата и тоже приводятся к 1-му числу.
   - Числа интерпретируются как:
     - `yyyymm` если значение больше `200000` (например, `202401`),
     - иначе как Excel-серийная дата (база `1899-12-30`).
4. Пары `(месяц, значение)` собираются в таблицу, значения `None` и `NaN` отбрасываются.
5. Если несколько значений попадают в один и тот же месяц, они агрегируются по `aggregation`:
   - `AVERAGE`, `COUNT`, `COUNTA`, `MAX`, `MEDIAN`, `MIN`, `SUM`.
6. Границы временной сетки:
   - если `start_date` задан, он используется как нижняя граница (после такого же парсинга),
   - иначе берется минимальный месяц из данных,
   - аналогично для `end_date`.
7. Строится непрерывная месячная сетка `MS` между этими границами. Если `fill_missing=True`, пропущенные месяцы заполняются нулями; иначе удаляются.
8. Итоговый ряд преобразуется в массив и используется для поиска сезонности.
9. Детектор сезонности:
   - если длина ряда < 3, сезонность = 1.
   - ряд линейно детрендируется (вычитается тренд), как в Excel-подобной логике.
   - вычисляется автокорреляция на лагах от 2 до `min(24, N/2)`.
   - выбирается лаг с максимальной корреляцией; если максимум не положительный, возвращается 1.
10. Возвращаемое значение всегда целое число ≥ 1. При ошибках на уровне сервиса SSE результат по умолчанию тоже 1.

Примечание: при наличии `statsmodels` автокорреляция считается через `statsmodels.tsa.stattools.acf`, иначе используется ручной расчет через корреляцию между сдвинутыми рядами.
