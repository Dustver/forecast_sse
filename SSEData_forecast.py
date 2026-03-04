"""
Shared enum declarations used by ScriptEval/SSE glue code.

Why this file exists:
- Qlik SSE passes metadata that describes function kind and argument/return types.
- Multiple modules (`ScriptEval_forecast.py`, `ExtensionService_forecast.py`) need
  a common set of constants to interpret those metadata values consistently.

Design note:
- Values in these enums are protocol-facing numeric codes.
- Keep numeric values stable, because they are part of the integration contract.
"""

from enum import Enum


class ArgType(Enum):
    """
    Data type classification for function arguments in script evaluation mode.

    Meanings:
    - Undefined (-1): invalid/uninitialized type.
    - Empty (0): argument is present but empty.
    - String (1): textual argument.
    - Numeric (2): numeric argument.
    - Mixed (3): heterogeneous payload.
    """
    Undefined = -1
    Empty = 0
    String = 1
    Numeric = 2
    Mixed = 3


class ReturnType(Enum):
    """
    Return type classification for SSE function outputs.

    Meanings:
    - Undefined (-1): invalid/uninitialized output type.
    - String (0): textual output.
    - Numeric (1): numeric output.
    - Dual (2): Qlik dual value (text + number).
    """
    Undefined = -1
    String = 0
    Numeric = 1
    Dual = 2


class FunctionType(Enum):
    """
    Function execution mode used by Qlik SSE runtime.

    Modes:
    - Scalar (0): one output row per input row.
    - Aggregation (1): one output for a group of input rows.
    - Tensor (2): table-like output with arbitrary row count.
    """
    Scalar = 0
    Aggregation = 1
    Tensor = 2
