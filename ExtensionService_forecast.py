#! /usr/bin/env python3
"""
Minimal SSE service exposing only:
- PING (echo)
- FORECAST_ETS_SEASONALITY / SEASONALITY
"""

import argparse
import json
import logging
import logging.config
import os
import sys
import time
from concurrent import futures

import grpc

from ets import forecast_ets_seasonality, Aggregation

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in [THIS_DIR, os.path.join(THIS_DIR, "Generated"), os.path.join(THIS_DIR, "vendor")]:
    if p not in sys.path:
        sys.path.append(p)

import ServerSideExtension_pb2 as SSE  # noqa: E402

_ONE_DAY_IN_SECONDS = 60 * 60 * 24


class ExtensionService(SSE.ConnectorServicer):
    def __init__(self, funcdef_file):
        self._function_definitions = funcdef_file
        os.makedirs("logs", exist_ok=True)
        log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logger.config")
        logging.config.fileConfig(log_file)
        logging.info("Logging enabled - Seasonality-only SSE")

    @property
    def function_definitions(self):
        return self._function_definitions

    @property
    def functions(self):
        return {
            0: "_ping",
            1: "_forecast_ets_seasonality",
        }

    @staticmethod
    def _ping(request, context):
        for bundle in request:
            for row in bundle.rows:
                value = row.duals[0].numData
                yield SSE.BundledRows(rows=[SSE.Row(duals=[SSE.Dual(numData=value)])])

    @staticmethod
    def _forecast_ets_seasonality(request, context):
        values = []
        timeline = []
        fill_missing = True
        aggregation = Aggregation.AVERAGE
        start_date = None
        end_date = None

        for bundle in request:
            for row in bundle.rows:
                duals = row.duals
                num_params = len(duals)

                values.append(duals[0].numData)

                if num_params > 1 and duals[1].numData is not None:
                    timeline.append(duals[1].numData)

                if num_params > 2 and duals[2].numData is not None:
                    fill_missing = bool(int(duals[2].numData))

                if num_params > 3 and duals[3].numData is not None:
                    agg = int(duals[3].numData)
                    if 1 <= agg <= 7:
                        aggregation = agg

                if num_params > 4 and duals[4].numData is not None:
                    start_date = duals[4].numData

                if num_params > 5 and duals[5].numData is not None:
                    end_date = duals[5].numData

        if not timeline:
            timeline = None

        try:
            seasonality = forecast_ets_seasonality(
                values=values,
                timeline=timeline,
                fill_missing=fill_missing,
                aggregation=aggregation,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            logging.error(f"SEASONALITY error: {e}")
            seasonality = 1

        yield SSE.BundledRows(rows=[SSE.Row(duals=[SSE.Dual(numData=float(seasonality))])])

    @staticmethod
    def _get_function_id(context):
        metadata = dict(context.invocation_metadata())
        header = SSE.FunctionRequestHeader()
        header.ParseFromString(metadata["qlik-functionrequestheader-bin"])
        return header.functionId

    def GetCapabilities(self, request, context):
        logging.info("GetCapabilities")

        capabilities = SSE.Capabilities(
            allowScript=False,
            pluginIdentifier="Seasonality-only SSE",
            pluginVersion="v2.1.0",
        )

        with open(self.function_definitions) as json_file:
            for definition in json.load(json_file)["Functions"]:
                function = capabilities.functions.add()
                function.name = definition["Name"]
                function.functionId = definition["Id"]
                function.functionType = definition["Type"]
                function.returnType = definition["ReturnType"]

                for param_name, param_type in sorted(definition["Params"].items()):
                    function.params.add(name=param_name, dataType=param_type)

        return capabilities

    def ExecuteFunction(self, request_iterator, context):
        func_id = self._get_function_id(context)
        logging.info(f"ExecuteFunction (functionId: {func_id})")
        return getattr(self, self.functions[func_id])(request_iterator, context)

    def Serve(self, port, pem_dir):
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        SSE.add_ConnectorServicer_to_server(self, server)

        if pem_dir:
            with open(os.path.join(pem_dir, "sse_server_key.pem"), "rb") as f:
                private_key = f.read()
            with open(os.path.join(pem_dir, "sse_server_cert.pem"), "rb") as f:
                cert_chain = f.read()
            with open(os.path.join(pem_dir, "root_cert.pem"), "rb") as f:
                root_cert = f.read()
            credentials = grpc.ssl_server_credentials([(private_key, cert_chain)], root_cert, True)
            server.add_secure_port("[::]:{}".format(port), credentials)
            logging.info(f"*** Running server in secure mode on port: {port} ***")
        else:
            server.add_insecure_port("[::]:{}".format(port))
            logging.info(f"*** Running server in insecure mode on port: {port} ***")

        server.start()
        try:
            while True:
                time.sleep(_ONE_DAY_IN_SECONDS)
        except KeyboardInterrupt:
            server.stop(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", nargs="?", default="50053")
    parser.add_argument("--pem_dir", nargs="?")
    parser.add_argument("--definition_file", nargs="?", default="FuncDefs_forecast.json")
    args = parser.parse_args()

    def_file = os.path.join(os.path.dirname(os.path.realpath(sys.argv[0])), args.definition_file)

    calc = ExtensionService(def_file)
    calc.Serve(args.port, args.pem_dir)
