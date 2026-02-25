$ErrorActionPreference = "Stop"

# Some older `ServerSideExtension_pb2.py` generations require this with newer protobuf.
#$env:PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION = "python"

python .\ExtensionService_forecast.py --port 50051

