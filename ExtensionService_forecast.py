#! /usr/bin/env python3
"""
Qlik Sense Server Side Extension for FORECAST.ETS Functions

This service is the transport/integration layer between Qlik SSE protocol and
the forecasting engine in `ets.py`.

Main responsibilities:
1. Parse incoming gRPC/SSE bundles into Python lists and scalar options.
2. Validate optional parameters and apply defaults compatible with Excel usage.
3. Call the corresponding core function (`forecast_ets*`).
4. Convert Python outputs back to SSE `Dual` rows.
5. Expose function metadata via `GetCapabilities`.

This SSE provides Excel-compatible FORECAST.ETS family endpoints:
- FORECAST_ETS_SEASONALITY(values, timeline, [data_completion], [aggregation])
- FORECAST_ETS(values, timeline, [seasonality], [data_completion], [aggregation])
- FORECAST_ETS_TREND(values, timeline, [seasonality], [data_completion], [aggregation])
- FORECAST_ETS_SERIES(values, timeline, target_timeline, [seasonality], [data_completion], [aggregation])
- FORECAST_ETS_CONFINT(values, timeline, target_date, [confidence_level], [seasonality], [data_completion], [aggregation])

Author: Matrix Agent
"""

import argparse
import json
import logging
import logging.config
import os
import sys
import time
from concurrent import futures

# Compatibility: some Qlik SSE `*_pb2.py` files in the wild were generated with
# older protoc and break with newer `protobuf`'s default (cpp) implementation.
# For those, forcing the pure-Python implementation avoids
# "Descriptors cannot be created directly" at import time.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

from ets import (
    forecast_ets,
    forecast_ets_seasonality,
    forecast_ets_trend,
    forecast_ets_series,
    forecast_ets_confint,
    forecast_ets_seasonality_table_simple,
    DataCompletion,
    Aggregation
)
from seasonality import SEASONALITY

# Ensure protobuf stubs are importable.
# Supports either:
# - `ServerSideExtension_pb2.py` in this folder (common during development), or
# - `Generated/ServerSideExtension_pb2.py` (common in Qlik examples)
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in [THIS_DIR, os.path.join(THIS_DIR, 'Generated'), os.path.join(THIS_DIR, 'vendor')]:
    if p not in sys.path:
        sys.path.append(p)

import ServerSideExtension_pb2 as SSE
import grpc
from ScriptEval_forecast import ScriptEval
from SSEData_forecast import FunctionType

_ONE_DAY_IN_SECONDS = 60 * 60 * 24
_MINFLOAT = float('-inf')

# Default values for optional parameters
DEFAULT_DATA_COMPLETION = DataCompletion.INTERPOLATE  # 1
DEFAULT_AGGREGATION = Aggregation.AVERAGE  # 1
DEFAULT_SEASONALITY = 0  # Auto-detect
DEFAULT_CONFIDENCE_LEVEL = 0.95

USE_STATSMODELS = os.environ.get('SSE_USE_STATSMODELS', 'true').lower() == 'true'

# Parameter position convention used across handlers:
# - The SSE request is row-oriented: each row contains `duals` array with params.
# - For aggregate functions, we collect values from all rows and compute once.
# - Optional params are usually repeated as constants across rows.
#   Implementation rule: "last valid value wins" while scanning rows.
#
# Common positions:
#   p0 = values
#   p1 = timeline
#   p2..pn = function-specific optional args (see each handler docstring).

class ExtensionService(SSE.ConnectorServicer):
    """
    gRPC implementation of the Qlik SSE Connector service.

    Qlik calls two main RPCs:
    - `GetCapabilities`: asks plugin which functions are available.
    - `ExecuteFunction`: sends grouped rows for one function id.

    This class maps function ids to static handlers that parse SSE bundles and
    stream results back.
    """

    def __init__(self, funcdef_file):
        """
        Initialize service and logging.

        Args:
            funcdef_file: path to JSON function definition file used by
                `GetCapabilities`.
        """
        self._function_definitions = funcdef_file
        self.scriptEval = ScriptEval()
        # Ensure log directory exists before loading logger config.
        os.makedirs('logs', exist_ok=True)
        log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logger.config')
        logging.config.fileConfig(log_file)
        logging.info('Logging enabled - Forecast ETS SSE v2.0.0')

    @property
    def function_definitions(self):
        """
        Function definition file path used for capability negotiation.
        """
        return self._function_definitions

    @property
    def functions(self):
        """
        Registry mapping Qlik `functionId` -> class method name.

        Important:
        - IDs must match `FuncDefs_forecast.json`.
        - Values are method names resolved by `getattr` in ExecuteFunction.
        """
        return {
            0: '_ping',
            1: '_forecast_ets',
            2: '_forecast_ets_seasonality',
            3: '_forecast_ets_trend',
            4: '_forecast_ets_series',
            5: '_forecast_ets_confint',
            6: '_forecast_ets_seasonality_table',
            7: '_seasonality'
        }

    @staticmethod
    def _ping(request, context):
        """
        Echo helper for connectivity/smoke tests.

        Protocol behavior:
        - Reads first numeric dual from each incoming row.
        - Streams one output row with exactly that numeric value.
        """
        for bundle in request:
            for row in bundle.rows:
                value = row.duals[0].numData
                yield SSE.BundledRows(
                    rows=[SSE.Row(duals=[SSE.Dual(numData=value)])]
                )
    @staticmethod
    def _seasonality(request, context):
        """
        Legacy alias endpoint for seasonality detection.

        Signature (row layout):
            param0 -> value (required)
            param1 -> timeline (required but can be omitted globally)
            param2 -> data_completion (optional, 0/1)
            param3 -> aggregation (optional, 1..7)

        Notes:
        - This handler calls `SEASONALITY(...)` wrapper for backward compatibility.
        - On any error, returns `1` (Excel semantic: no seasonality).
        """
        values = []
        timeline = []
        data_completion = DEFAULT_DATA_COMPLETION
        aggregation = DEFAULT_AGGREGATION

        for bundle in request:
            for row in bundle.rows:
                duals = row.duals
                num_params = len(duals)

                # Required: values (param 0)
                values.append(duals[0].numData)

                # Required: timeline (param 1)
                if num_params > 1:
                    timeline.append(duals[1].numData)

                # Optional: data_completion (param 2)
                # Accept only documented values 0/1; ignore invalid values silently.
                if num_params > 2 and duals[2].numData is not None:
                    dc = int(duals[2].numData)
                    if dc in [0, 1]:
                        data_completion = dc

                # Optional: aggregation (param 3)
                # Accept only supported aggregation codes 1..7.
                if num_params > 3 and duals[3].numData is not None:
                    agg = int(duals[3].numData)
                    if 1 <= agg <= 7:
                        aggregation = agg

        # If no timeline provided, create sequential integers
        if not timeline:
            # Keeps aggregate use-cases functional when caller passes only values.
            timeline = list(range(len(values)))

        try:
            result = SEASONALITY(
                values=values,
                timeline=timeline,
                data_completion=data_completion,
                aggregation=aggregation,
                use_statsmodels=USE_STATSMODELS
            )
            logging.info(f'FORECAST_ETS_SEASONALITY: detected seasonality = {result}')
        except Exception as e:
            logging.error(f'FORECAST_ETS_SEASONALITY error: {str(e)}')
            result = 1  # Return 1 (no seasonality) on error

        yield SSE.BundledRows(
            rows=[SSE.Row(duals=[SSE.Dual(numData=float(result))])]
        )



    @staticmethod
    def _forecast_ets_seasonality(request, context):
        """
        FORECAST_ETS_SEASONALITY(values, timeline, [data_completion], [aggregation])

        Primary seasonality endpoint.

        Input parsing model:
        - The request may contain multiple bundles; all rows are concatenated.
        - Each row contributes one `value` (and optionally timeline/params).
        - Optional parameters are "last non-null wins", which matches common
          Qlik usage where optional args are repeated as constants per row.

        Return:
        - Single numeric row with detected period.
        """
        values = []
        timeline = []
        data_completion = DEFAULT_DATA_COMPLETION
        aggregation = DEFAULT_AGGREGATION

        for bundle in request:
            for row in bundle.rows:
                duals = row.duals
                num_params = len(duals)

                # Required: values (param 0)
                values.append(duals[0].numData)

                # Required: timeline (param 1)
                if num_params > 1:
                    timeline.append(duals[1].numData)

                # Optional: data_completion (param 2)
                if num_params > 2 and duals[2].numData is not None:
                    dc = int(duals[2].numData)
                    if dc in [0, 1]:
                        data_completion = dc

                # Optional: aggregation (param 3)
                if num_params > 3 and duals[3].numData is not None:
                    agg = int(duals[3].numData)
                    if 1 <= agg <= 7:
                        aggregation = agg

        # If no timeline provided, create sequential integers
        if not timeline:
            timeline = list(range(len(values)))

        try:
            result = forecast_ets_seasonality(
                values=values,
                timeline=timeline,
                data_completion=data_completion,
                aggregation=aggregation,
                use_statsmodels=USE_STATSMODELS
            )
            logging.info(f'FORECAST_ETS_SEASONALITY: detected seasonality = {result}')
        except Exception as e:
            logging.error(f'FORECAST_ETS_SEASONALITY error: {str(e)}')
            result = 1  # Return 1 (no seasonality) on error

        yield SSE.BundledRows(
            rows=[SSE.Row(duals=[SSE.Dual(numData=float(result))])]
        )

    @staticmethod
    def _forecast_ets(request, context):
        """
        FORECAST_ETS(values, timeline, [seasonality], [data_completion], [aggregation])

        Aggregate endpoint that returns one-step-ahead forecast.

        Option semantics:
        - `seasonality=0` enables auto-detection.
        - `seasonality=1` forces non-seasonal model.
        - `seasonality>1` forces specified period.

        Error behavior:
        - Returns NaN if model fails, but does not abort the RPC stream.
        """
        values = []
        timeline = []
        seasonality = DEFAULT_SEASONALITY
        data_completion = DEFAULT_DATA_COMPLETION
        aggregation = DEFAULT_AGGREGATION

        for bundle in request:
            for row in bundle.rows:
                duals = row.duals
                num_params = len(duals)

                values.append(duals[0].numData)

                if num_params > 1:
                    timeline.append(duals[1].numData)

                if num_params > 2 and duals[2].numData is not None:
                    # `seasonality` can be 0 (auto), 1 (none), >1 fixed period.
                    seasonality = int(duals[2].numData)

                if num_params > 3 and duals[3].numData is not None:
                    dc = int(duals[3].numData)
                    if dc in [0, 1]:
                        data_completion = dc

                if num_params > 4 and duals[4].numData is not None:
                    agg = int(duals[4].numData)
                    if 1 <= agg <= 7:
                        aggregation = agg

        if not timeline:
            timeline = list(range(len(values)))

        try:
            result = forecast_ets(
                values=values,
                timeline=timeline,
                seasonality=seasonality,
                data_completion=data_completion,
                aggregation=aggregation,
                horizon=1,
                use_statsmodels=USE_STATSMODELS
            )
            logging.info(f'FORECAST_ETS: forecast = {result}')
        except Exception as e:
            logging.error(f'FORECAST_ETS error: {str(e)}')
            result = float('nan')

        yield SSE.BundledRows(
            rows=[SSE.Row(duals=[SSE.Dual(numData=result)])]
        )

    @staticmethod
    def _forecast_ets_trend(request, context):
        """
        FORECAST_ETS_TREND(values, timeline, [seasonality], [data_completion], [aggregation])

        Aggregate endpoint that returns trend slope of the prepared series.
        """
        values = []
        timeline = []
        seasonality = DEFAULT_SEASONALITY
        data_completion = DEFAULT_DATA_COMPLETION
        aggregation = DEFAULT_AGGREGATION

        for bundle in request:
            for row in bundle.rows:
                duals = row.duals
                num_params = len(duals)

                values.append(duals[0].numData)

                if num_params > 1:
                    timeline.append(duals[1].numData)

                if num_params > 2 and duals[2].numData is not None:
                    seasonality = int(duals[2].numData)

                if num_params > 3 and duals[3].numData is not None:
                    dc = int(duals[3].numData)
                    if dc in [0, 1]:
                        data_completion = dc

                if num_params > 4 and duals[4].numData is not None:
                    agg = int(duals[4].numData)
                    if 1 <= agg <= 7:
                        aggregation = agg

        if not timeline:
            timeline = list(range(len(values)))

        try:
            result = forecast_ets_trend(
                values=values,
                timeline=timeline,
                seasonality=seasonality,
                data_completion=data_completion,
                aggregation=aggregation
            )
            logging.info(f'FORECAST_ETS_TREND: trend = {result}')
        except Exception as e:
            logging.error(f'FORECAST_ETS_TREND error: {str(e)}')
            result = float('nan')

        yield SSE.BundledRows(
            rows=[SSE.Row(duals=[SSE.Dual(numData=result)])]
        )

    @staticmethod
    def _forecast_ets_series(request, context):
        """
        FORECAST_ETS_SERIES(values, timeline, target_timeline, [seasonality], [data_completion], [aggregation])

        Tensor endpoint: returns one row per target point forecast.

        Parsing specifics:
        - `target_timeline` is collected row-wise from param2.
        - If target timeline is empty, core function forecasts one next point.

        Stream behavior:
        - Outputs one `BundledRows` per result value.
        """
        values = []
        timeline = []
        target_timeline = []
        seasonality = DEFAULT_SEASONALITY
        data_completion = DEFAULT_DATA_COMPLETION
        aggregation = DEFAULT_AGGREGATION

        for bundle in request:
            for row in bundle.rows:
                duals = row.duals
                num_params = len(duals)

                values.append(duals[0].numData)

                if num_params > 1:
                    timeline.append(duals[1].numData)

                if num_params > 2:
                    # For tensor call this is usually a future timestamp per row.
                    target_timeline.append(duals[2].numData)

                if num_params > 3 and duals[3].numData is not None:
                    seasonality = int(duals[3].numData)

                if num_params > 4 and duals[4].numData is not None:
                    dc = int(duals[4].numData)
                    if dc in [0, 1]:
                        data_completion = dc

                if num_params > 5 and duals[5].numData is not None:
                    agg = int(duals[5].numData)
                    if 1 <= agg <= 7:
                        aggregation = agg

        if not timeline:
            timeline = list(range(len(values)))

        try:
            results = forecast_ets_series(
                values=values,
                timeline=timeline,
                target_timeline=target_timeline if target_timeline else None,
                seasonality=seasonality,
                data_completion=data_completion,
                aggregation=aggregation
            )
            logging.info(f'FORECAST_ETS_SERIES: generated {len(results)} forecasts')

            for v in results:
                yield SSE.BundledRows(
                    rows=[SSE.Row(duals=[SSE.Dual(numData=v)])]
                )
        except Exception as e:
            logging.error(f'FORECAST_ETS_SERIES error: {str(e)}')
            yield SSE.BundledRows(
                rows=[SSE.Row(duals=[SSE.Dual(numData=float('nan'))])]
            )

    @staticmethod
    def _forecast_ets_confint(request, context):
        """
        FORECAST_ETS_CONFINT(values, timeline, target_date, [confidence_level], [seasonality], [data_completion], [aggregation])

        Aggregate endpoint returning lower/upper confidence bounds.

        Output contract:
        - Returns exactly two rows in one bundle:
          row1 = lower bound, row2 = upper bound.
        """
        values = []
        timeline = []
        target_date = None
        confidence_level = DEFAULT_CONFIDENCE_LEVEL
        seasonality = DEFAULT_SEASONALITY
        data_completion = DEFAULT_DATA_COMPLETION
        aggregation = DEFAULT_AGGREGATION

        for bundle in request:
            for row in bundle.rows:
                duals = row.duals
                num_params = len(duals)

                values.append(duals[0].numData)

                if num_params > 1:
                    timeline.append(duals[1].numData)

                if num_params > 2 and duals[2].numData is not None:
                    target_date = duals[2].numData

                if num_params > 3 and duals[3].numData is not None:
                    cl = duals[3].numData
                    # Excel-style confidence level is open interval (0,1).
                    if 0 < cl < 1:
                        confidence_level = cl

                if num_params > 4 and duals[4].numData is not None:
                    seasonality = int(duals[4].numData)

                if num_params > 5 and duals[5].numData is not None:
                    dc = int(duals[5].numData)
                    if dc in [0, 1]:
                        data_completion = dc

                if num_params > 6 and duals[6].numData is not None:
                    agg = int(duals[6].numData)
                    if 1 <= agg <= 7:
                        aggregation = agg

        if not timeline:
            timeline = list(range(len(values)))

        try:
            lower, upper = forecast_ets_confint(
                values=values,
                timeline=timeline,
                target_date=target_date,
                confidence_level=confidence_level,
                seasonality=seasonality,
                data_completion=data_completion,
                aggregation=aggregation
            )
            logging.info(f'FORECAST_ETS_CONFINT: [{lower}, {upper}]')
        except Exception as e:
            logging.error(f'FORECAST_ETS_CONFINT error: {str(e)}')
            lower, upper = float('nan'), float('nan')

        yield SSE.BundledRows(
            rows=[
                SSE.Row(duals=[SSE.Dual(numData=lower)]),
                SSE.Row(duals=[SSE.Dual(numData=upper)])
            ]
        )

    @staticmethod
    def _forecast_ets_seasonality_table(request, context):
        """
        FORECAST_ETS_SEASONALITY_TABLE(values, timeline, horizon, [data_completion], [aggregation])

        Tensor endpoint for full fitted+forecast sequence.

        Typical use:
        - Caller passes historical values/timeline and fixed `horizon`.
        - Function returns historical fitted values followed by future forecast.

        Output cardinality:
        - `len(cleaned_series) + horizon` rows.
        """
        values = []
        timeline = []
        horizon = None
        data_completion = DEFAULT_DATA_COMPLETION
        aggregation = DEFAULT_AGGREGATION

        for bundle in request:
            for row in bundle.rows:
                duals = row.duals
                num_params = len(duals)

                # Required: values (param 0)
                values.append(duals[0].numData)

                # Optional/required depending on call pattern: timeline (param 1)
                if num_params > 1:
                    timeline.append(duals[1].numData)

                # Required: horizon (param 2).
                # In grouped calls this is usually repeated constant; last value is used.
                if num_params > 2 and duals[2].numData is not None:
                    horizon = int(duals[2].numData)

                # Optional: data_completion (param 3)
                if num_params > 3 and duals[3].numData is not None:
                    dc = int(duals[3].numData)
                    if dc in [0, 1]:
                        data_completion = dc

                # Optional: aggregation (param 4)
                if num_params > 4 and duals[4].numData is not None:
                    agg = int(duals[4].numData)
                    if 1 <= agg <= 7:
                        aggregation = agg

        if not timeline:
            timeline = list(range(len(values)))

        if horizon is None:
            # Safe default when caller omitted horizon.
            horizon = 12

        try:
            results = forecast_ets_seasonality_table_simple(
                values=values,
                timeline=timeline,
                horizon=horizon,
                data_completion=data_completion,
                aggregation=aggregation,
            )
            logging.info(
                f'FORECAST_ETS_SEASONALITY_TABLE: generated {len(results)} values '
                f'(historical={len(values)}, horizon={horizon})'
            )

            for v in results:
                yield SSE.BundledRows(
                    rows=[SSE.Row(duals=[SSE.Dual(numData=float(v))])]
                )
        except Exception as e:
            logging.error(f'FORECAST_ETS_SEASONALITY_TABLE error: {str(e)}')
            yield SSE.BundledRows(
                rows=[SSE.Row(duals=[SSE.Dual(numData=float("nan"))])]
            )

    @staticmethod
    def _get_function_id(context):
        """
        Extract Qlik `functionId` from gRPC invocation metadata.

        Qlik puts serialized `FunctionRequestHeader` into metadata key
        `qlik-functionrequestheader-bin`. This helper decodes it and returns id.
        """
        metadata = dict(context.invocation_metadata())
        header = SSE.FunctionRequestHeader()
        header.ParseFromString(metadata['qlik-functionrequestheader-bin'])
        return header.functionId

    def GetCapabilities(self, request, context):
        """
        Build and return plugin capabilities for Qlik handshake.

        Reads function definitions from JSON and mirrors them into protobuf
        `Capabilities` response expected by Qlik engine.
        """
        logging.info('GetCapabilities')

        capabilities = SSE.Capabilities(
            allowScript=True,
            pluginIdentifier='Forecast ETS Operations - Qlik (Excel-compatible)',
            pluginVersion='v2.0.0'
        )

        with open(self.function_definitions) as json_file:
            for definition in json.load(json_file)['Functions']:
                function = capabilities.functions.add()
                function.name = definition['Name']
                function.functionId = definition['Id']
                function.functionType = definition['Type']
                function.returnType = definition['ReturnType']

                for param_name, param_type in sorted(definition['Params'].items()):
                    function.params.add(name=param_name, dataType=param_type)

                logging.info('Adding to capabilities: {}({})'.format(
                    function.name,
                    [p.name for p in function.params]
                ))

        return capabilities

    def ExecuteFunction(self, request_iterator, context):
        """
        Dispatch runtime SSE function call by `functionId`.

        Flow:
        1. Decode function id from metadata.
        2. Resolve handler name via `self.functions`.
        3. Call handler with request iterator/context.
        """
        func_id = self._get_function_id(context)
        logging.info('ExecuteFunction (functionId: {})'.format(func_id))

        return getattr(self, self.functions[func_id])(request_iterator, context)

    def EvaluateScript(self, request, context):
        """
        Handle Qlik script-evaluation mode.

        This path delegates to `ScriptEval_forecast.py` and supports only
        Tensor/Aggregation modes configured for this plugin.
        """
        metadata = dict(context.invocation_metadata())
        header = SSE.ScriptRequestHeader()
        header.ParseFromString(metadata['qlik-scriptrequestheader-bin'])

        func_type = self.scriptEval.get_func_type(header)

        if (func_type == FunctionType.Tensor) or (func_type == FunctionType.Aggregation):
            return self.scriptEval.EvaluateScript(request, context, header, func_type)
        else:
            msg = 'Function type {} is not supported in this plugin.'.format(func_type.name)
            context.set_code(grpc.StatusCode.UNIMPLEMENTED)
            context.set_details(msg)
            raise grpc.RpcError(grpc.StatusCode.UNIMPLEMENTED, msg)

    def Serve(self, port, pem_dir):
        """
        Start gRPC server and block forever.

        Args:
            port: TCP port for SSE endpoint.
            pem_dir: TLS certificate directory. If provided, secure mode is used.

        Runtime notes:
        - Uses thread pool with 10 workers.
        - Runs until KeyboardInterrupt.
        """
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        SSE.add_ConnectorServicer_to_server(self, server)

        if pem_dir:
            with open(os.path.join(pem_dir, 'sse_server_key.pem'), 'rb') as f:
                private_key = f.read()
            with open(os.path.join(pem_dir, 'sse_server_cert.pem'), 'rb') as f:
                cert_chain = f.read()
            with open(os.path.join(pem_dir, 'root_cert.pem'), 'rb') as f:
                root_cert = f.read()
            credentials = grpc.ssl_server_credentials([(private_key, cert_chain)], root_cert, True)
            server.add_secure_port('[::]:{}'.format(port), credentials)
            logging.info('*** Running server in secure mode on port: {} ***'.format(port))
        else:
            server.add_insecure_port('[::]:{}'.format(port))
            logging.info('*** Running server in insecure mode on port: {} ***'.format(port))

        server.start()
        try:
            while True:
                time.sleep(_ONE_DAY_IN_SECONDS)
        except KeyboardInterrupt:
            server.stop(0)


if __name__ == '__main__':
    # CLI entrypoint used in local/dev and service wrappers.
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', nargs='?', default='50053')
    parser.add_argument('--pem_dir', nargs='?')
    parser.add_argument('--definition_file', nargs='?', default='FuncDefs_forecast.json')
    args = parser.parse_args()

    def_file = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), args.definition_file)

    calc = ExtensionService(def_file)
    calc.Serve(args.port, args.pem_dir)
