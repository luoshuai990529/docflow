"""Allowlisted model diagnostics shared by use cases and model adapters."""

import re


def diagnostic_metadata(metadata):
    """Keep only bounded identifiers and hashes; never retain model text."""
    result = {}
    for key in ('schema_sha256', 'response_sha256'):
        value = metadata.get(key)
        if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value):
            result[key] = value
    if metadata.get('output_mode') in ('json_schema', 'json_object', 'prompt_only'):
        result['output_mode'] = metadata['output_mode']
    value = metadata.get('request_id')
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,199}', value):
        result['request_id'] = value
    return result
