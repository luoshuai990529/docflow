"""Pure normalization for machine-readable workflow error codes."""

import re


_ERROR_CODE = re.compile(r"[A-Z_]{2,80}")


def normalize_error_code(error) -> str:
    code = str(error).split(":", 1)[0]
    return code if _ERROR_CODE.fullmatch(code) else "PROCESSING_ERROR"
