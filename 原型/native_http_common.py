"""Finite HTTP primitives shared by native frontends; no Agent imports.

The historical SpecialistHTTPError name is retained as the exact public class
for compatibility. This module owns only local JSON validation and error shape.
"""

class SpecialistHTTPError(ValueError):
    def __init__(self, status, code, message, *, result_unknown=False, accepted=None):
        self.status, self.code, self.message = status, code, message
        self.result_unknown = result_unknown
        self.accepted = accepted
        super().__init__(message)

    def payload(self):
        value = {'error': self.message, 'code': self.code}
        if self.result_unknown:
            value['result_unknown'] = True
        if self.accepted is False:
            value['accepted'] = False
        return value


def invalid():
    raise SpecialistHTTPError(400, 'specialist_invalid_request', '专员请求格式无效，请重新打开页面后再试。')


def _json(value, *, depth=0, budget=None):
    if budget is None:
        budget = [0]
    budget[0] += 1
    if depth > 32 or budget[0] > 10000:
        invalid()
    kind = type(value)
    if kind is str:
        try: value.encode('utf-8')
        except UnicodeError: invalid()
        return
    if value is None or kind is bool:
        return
    if kind is int and -(2**53 - 1) <= value <= 2**53 - 1:
        return
    if kind is float:
        import math
        if math.isfinite(value):
            return
    if kind in (dict, list):
        if kind is dict:
            for key in value:
                if type(key) is not str:
                    invalid()
                try: key.encode('utf-8')
                except UnicodeError: invalid()
        for child in value.values() if kind is dict else value:
            _json(child, depth=depth + 1, budget=budget)
        return
    invalid()


def validate_fields(body, required, optional=()):
    """Bounded JSON fields shared by the native HTTP adapters."""
    try:
        if type(body) is not dict or not set(required) <= body.keys() or body.keys() - set(required) - set(optional):
            invalid()
        _json(body)
    except SpecialistHTTPError as error:
        error.accepted = False
        raise

