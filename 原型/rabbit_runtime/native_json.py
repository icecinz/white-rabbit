"""Finite canonical JSON bytes shared by native transports; no Agent or I/O.

Callers own their limits and error vocabulary. Hashing streams every encoded
part without constructing a second full tree or one full encoded document.
"""
import hashlib
import json
import math


class NativeJSONError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('Native JSON exceeds its boundary.' if code == 'capacity' else 'Invalid native JSON.')


def canonical_metadata(value, *, max_bytes, max_depth, max_nodes, safe_integers=False):
    if (any(type(limit) is not int or limit <= 0 for limit in (max_bytes, max_depth, max_nodes))
            or type(safe_integers) is not bool):
        raise NativeJSONError('invalid_json')
    active, count = set(), 0

    def validate(item, depth=0):
        nonlocal count
        count += 1
        if count > max_nodes or depth > max_depth:
            raise NativeJSONError('invalid_json')
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is int:
            if (safe_integers and not -(2**53 - 1) <= item <= 2**53 - 1
                    or not safe_integers and not math.isfinite(float(item))):
                raise NativeJSONError('invalid_json')
            return
        if kind is float:
            if not math.isfinite(item):
                raise NativeJSONError('invalid_json')
            return
        if kind is str:
            item.encode('utf-8', 'strict')
            return
        if kind not in (dict, list) or id(item) in active:
            raise NativeJSONError('invalid_json')
        active.add(id(item))
        try:
            if kind is dict:
                for key, nested in item.items():
                    if type(key) is not str:
                        raise NativeJSONError('invalid_json')
                    validate(key, depth + 1)
                    validate(nested, depth + 1)
            else:
                for nested in item:
                    validate(nested, depth + 1)
        finally:
            active.remove(id(item))

    try:
        validate(value)
        digest, size = hashlib.sha256(), 0
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        for part in encoder.iterencode(value):
            raw = part.encode('utf-8', 'strict')
            size += len(raw)
            if size > max_bytes:
                raise NativeJSONError('capacity')
            digest.update(raw)
        return {'sha256': digest.hexdigest(), 'size_bytes': size}
    except NativeJSONError:
        raise
    except (RuntimeError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise NativeJSONError('invalid_json') from None
