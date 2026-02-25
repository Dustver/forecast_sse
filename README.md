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

