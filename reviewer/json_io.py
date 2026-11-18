"""Strict JSON parsing and byte-bounded serialization."""

import json
import math


class JsonLimitError(ValueError):
    """The serialized document exceeds the caller's byte budget."""


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _finite_float(raw):
    number = float(raw)
    if not math.isfinite(number):
        raise ValueError("Expected a finite JSON number")
    return number


def strict_loads(data):
    return json.loads(data, object_pairs_hook=_unique_keys, parse_float=_finite_float,
                      parse_constant=_finite_float)


def bounded_dumps(value, limit):
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                               allow_nan=False)
    pieces = []
    size = 0
    for piece in encoder.iterencode(value):
        size += len(piece.encode("utf-8"))
        if size > limit:
            raise JsonLimitError("JSON document exceeds its UTF-8 byte budget")
        pieces.append(piece)
    return "".join(pieces)
