"""Same-ledger persistent scope evidence; legacy rows and receipts stay intact.

The owning store creates the exact table in its versioned schema. Evidence is
inserted only in an existing write transaction and only after the legacy raw
identity still matches. A saved UUID never licenses replacing an inode/path.
"""
from contextlib import contextmanager
import json
import re

from .directory_identity import (DirectoryIdentityError, DirectoryPin, FilePin,
                                 validate_anchor)


class ScopeAnchorError(ValueError):
    def __init__(self, code='scope_changed'):
        self.code = code
        super().__init__('Persistent scope evidence could not be verified: ' + code)


class ScopeAnchors:
    def __init__(self):
        self._pins = {}
        self._witnesses = {}
        self._frames = []
        self._closed = False

    @staticmethod
    def ddl(parent_table, key_column):
        for name in (parent_table, key_column):
            if type(name) is not str or not re.fullmatch(r'[a-z_]+', name):
                raise ScopeAnchorError('invalid_arguments')
        return ('''CREATE TABLE native_scope_anchors (
        scope_key TEXT NOT NULL, role TEXT NOT NULL, path TEXT NOT NULL,
        anchor_json TEXT NOT NULL, PRIMARY KEY(scope_key,role),
        FOREIGN KEY(scope_key) REFERENCES ''' + parent_table + '(' + key_column + '))')

    @staticmethod
    def _anchor(raw, kind):
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value
        try:
            if type(raw) is not str or len(raw) > 4096:
                raise ValueError
            value = validate_anchor(json.loads(raw, object_pairs_hook=unique,
                parse_constant=lambda _x: (_ for _ in ()).throw(ValueError())))
            if value['type'] != kind:
                raise ValueError
            return value
        except (DirectoryIdentityError, ValueError, TypeError, RecursionError):
            raise ScopeAnchorError('invalid_anchor') from None

    @contextmanager
    def transaction(self):
        """Check only scopes used by this transaction, before its commit."""
        if self._closed:
            raise ScopeAnchorError('closed')
        touched = set()
        self._frames.append(touched)
        try:
            yield
            if self._closed:
                raise ScopeAnchorError('closed')
            for key in touched:
                self._pins[key].check()
        except DirectoryIdentityError:
            raise ScopeAnchorError('scope_changed') from None
        finally:
            assert self._frames[-1] is touched
            self._frames.pop()
            still_touched = set().union(*self._frames) if self._frames else set()
            for key in touched - still_touched:
                pin = self._pins.pop(key, None)
                if pin is not None:
                    pin.close()

    def verify(self, db, scope_key, role, path, legacy_identity, *, kind='directory', seed=False):
        if self._closed:
            raise ScopeAnchorError('closed')
        if (type(scope_key) is not str or not scope_key or len(scope_key) > 256
                or type(role) is not str or not re.fullmatch(r'[a-z_]+', role)
                or kind not in ('directory', 'file') or type(seed) is not bool
                or not isinstance(legacy_identity, (tuple, list)) or len(legacy_identity) != 2
                or type(legacy_identity[0]) is not int or legacy_identity[0] < 0
                or type(legacy_identity[1]) is not int or legacy_identity[1] <= 0):
            raise ScopeAnchorError('invalid_arguments')
        path = str(path)
        present = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_scope_anchors'").fetchone()
        if seed and (not present or not db.in_transaction or db.execute('PRAGMA query_only').fetchone()[0]):
            raise ScopeAnchorError('seed_requires_write_transaction')
        row = (db.execute('SELECT path,anchor_json FROM native_scope_anchors WHERE scope_key=? AND role=?',
                          (scope_key, role)).fetchone() if present else None)
        anchor = None
        if row is not None:
            if row[0] != path:
                raise ScopeAnchorError('scope_changed')
            anchor = self._anchor(row[1], kind)
            if anchor['inode'] != legacy_identity[1]:
                raise ScopeAnchorError('scope_changed')
        key = (scope_key, role)
        pin = self._pins.get(key)
        created = pin is None
        retained = not created
        try:
            if created:
                pin = (DirectoryPin if kind == 'directory' else FilePin)(path, anchor)
            elif (pin.path != path or pin.anchor['type'] != kind
                    or (anchor is not None and pin.anchor != anchor)):
                raise ScopeAnchorError('scope_changed')
            pin.check()
            witness = (path, kind, pin.anchor, pin.process_witness)
            if key in self._witnesses and self._witnesses[key] != witness:
                raise ScopeAnchorError('scope_changed')
            observed = pin.current_identity
            if anchor is None and observed != tuple(legacy_identity):
                raise ScopeAnchorError('scope_changed')
            if row is None and seed:
                db.execute('INSERT INTO native_scope_anchors(scope_key,role,path,anchor_json) VALUES(?,?,?,?)',
                    (scope_key, role, path, json.dumps(pin.anchor, sort_keys=True, separators=(',', ':'))))
            self._witnesses[key] = witness
            if created and self._frames:
                self._pins[key] = pin
                retained = True
            for frame in self._frames:
                frame.add(key)
            return observed
        except DirectoryIdentityError:
            raise ScopeAnchorError('scope_changed') from None
        finally:
            if created and not retained and pin is not None:
                pin.close()

    def close(self):
        if not self._closed:
            self._closed = True
            for pin in self._pins.values():
                pin.close()
            self._pins.clear()
            self._witnesses.clear()
