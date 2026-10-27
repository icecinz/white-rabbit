"""Candidate: correlate one native gateway completion within this connection.

Import installs nothing. Three transparent wrappers observe an exact newly
claimed inflight object, one invocation's st/result, and that same native
completion. No Agent hook, DB, history search or original event is changed.
This bounded RAM cache is not durable deduplication: a restarted host must keep
using its existing operation ledger; an absent result never permits resending.
"""
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import hashlib
import importlib
import inspect
import json
from pathlib import Path, PurePosixPath
import re
import sys
import threading
import types
import uuid


PIN = "145c713f1691b44bc485ce0ce4818cdb0e9c396b"
SUBMIT, READ = "rabbit.prompt.submit", "rabbit.prompt.read"
MAX_ENTRIES, MAX_ANSWER_BYTES, MAX_ACK_BYTES = 128, 128 * 1024, 64 * 1024
MAX_TEXT_BYTES = 900 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SOURCE_SHA256 = {
    "tui_gateway/server.py": "095877028c49d3771553cadfda3c68d5f6f886bb2b29368a16080c2dacbabb9d",
    "tui_gateway/method_ctx.py": "12200d5cd91f7e387d069b30d2254e407e94e428e99295ffbf0902165d8d36ac",
    "tui_gateway/methods_prompt.py": "8c3cf07ca1cfca472f707ebb2884ed1bfc1a09c275d50bbf2f4a1665f4800d45",
    "tui_gateway/prompt_turn.py": "fe261820d10bff143f76adafe7dc1e1fe12e9f83d3000d2da81e01d4ef0e0ecb",
    "tui_gateway/session_history.py": "8dce7bf9fe8db42b7112c06a075ece6ac3b0e4e8835f63d5d73e7949f94b7a59",
}


def _codes(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from _codes(value)


def _signature(function):
    return tuple((p.name, p.kind, p.default) for p in inspect.signature(function, follow_wrapped=False).parameters.values())


def _verify(server, root, installed_commit, private_backend):
    head = (root / ".git/HEAD").read_text().strip()
    if head.startswith("ref: "):
        ref = head[5:]
        if not re.fullmatch(r"refs/[A-Za-z0-9_./-]{1,240}", ref) or ".." in ref.split("/"):
            raise ValueError("native_pin_mismatch")
        head = (root / ".git" / ref).read_text().strip()
    if private_backend is not True or installed_commit != PIN or head != PIN:
        raise ValueError("native_pin_mismatch")
    raw = {name: (root / name).read_bytes() for name in SOURCE_SHA256}
    if any(hashlib.sha256(data).hexdigest() != SOURCE_SHA256[name] for name, data in raw.items()):
        raise ValueError("native_source_changed")
    modules = {name: importlib.import_module(name) for name in (
        "tui_gateway.server", "tui_gateway.method_ctx", "tui_gateway.methods_prompt",
        "tui_gateway.prompt_turn", "tui_gateway.session_history")}
    if modules["tui_gateway.server"] is not server:
        raise ValueError("native_server_mismatch")
    for name, module in modules.items():
        if Path(module.__file__).resolve() != root / (name.replace(".", "/") + ".py"):
            raise ValueError("native_module_mismatch")
    compiled = {name: compile(data, str(root / name), "exec", dont_inherit=True, optimize=sys.flags.optimize)
                for name, data in raw.items()}

    def function(fn, module):
        if (type(fn) is not types.FunctionType or fn.__globals__ is not vars(module)
                or getattr(fn, "__wrapped__", None) is not None):
            raise ValueError("native_function_mismatch")
        try:
            name = str(Path(fn.__code__.co_filename).resolve().relative_to(root))
            choices = [code for code in _codes(compiled[name]) if code.co_qualname == fn.__code__.co_qualname
                       and code.co_firstlineno == fn.__code__.co_firstlineno]
        except (ValueError, KeyError):
            raise ValueError("native_function_mismatch") from None
        if len(choices) != 1 or fn.__code__ != choices[0]:
            raise ValueError("native_function_mismatch")

    prompt = modules["tui_gateway.methods_prompt"]
    pending = [fn for name, fn in prompt._registry._pending if name == "prompt.submit"]
    route = server._methods.get("prompt.submit")
    if len(pending) != 1:
        raise ValueError("native_route_mismatch")
    function(pending[0], prompt)
    function(route, server)
    if route.__code__ is not pending[0].__code__ or _signature(route) != _signature(pending[0]):
        raise ValueError("native_route_mismatch")
    definitions = {
        "_lock_in_submit_turn": (prompt, ("rid", "sid", "session", "text", "params", "has_truncation", "requested_rebind_ids", "hosted_task")),
        "_invoke_agent": (modules["tui_gateway.prompt_turn"], ("sid", "session", "st", "prompt", "run_message", "streamer", "images", "display_kind", "display_metadata")),
        "_complete_turn_payload": (modules["tui_gateway.prompt_turn"], ("session", "st", "status_note", "cols")),
        "_start_inflight_turn": (modules["tui_gateway.session_history"], ("session", "text")),
    }
    for name, (module, names) in definitions.items():
        original, bound = getattr(module, name), getattr(server, name)
        function(original, module)
        function(bound, server)
        signature = tuple((name, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty) for name in names)
        if bound.__code__ is not original.__code__ or _signature(bound) != signature or _signature(original) != signature:
            raise ValueError("native_rebinding_mismatch")
    if (type(server._sessions) is not dict or type(server._methods) is not dict
            or not hasattr(server._sessions_lock, "__enter__") or not callable(server._ok)):
        raise ValueError("native_registry_mismatch")
    if any((root / name).read_bytes() != data for name, data in raw.items()):
        raise ValueError("native_source_changed")
    return route


def _unavailable(reason, state="unavailable"):
    return {"state": state, "reason": reason, "answer": None, "source": None}


def _images(value):
    if type(value) is tuple and not value:
        return []
    if type(value) is not list or len(value) > 4:
        raise ValueError("invalid_images")
    rows = []
    for source in value:
        if type(source) is not dict or set(source) != {"path", "sha256", "size_bytes", "mime"}:
            raise ValueError("invalid_images")
        row = dict(source)
        path = row["path"]
        if (type(path) is not str or not path or len(path) > 4096
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in path)
                or not PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                or str(PurePosixPath(path)) != path
                or type(row["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                or type(row["size_bytes"]) is not int or not 0 < row["size_bytes"] <= 5 * 1024 * 1024
                or type(row["mime"]) is not str or row["mime"] not in ("image/png", "image/jpeg")):
            raise ValueError("invalid_images")
        path.encode("utf-8")
        rows.append(row)
    if sum(row["size_bytes"] for row in rows) > 20 * 1024 * 1024:
        raise ValueError("invalid_images")
    return rows


def prompt_sha256(text, images=()):
    """Hash the exact native logical prompt; the standalone bootstrap needs no SDK import."""
    if type(text) is not str:
        raise ValueError("invalid_prompt")
    value = {"type": "prompt", "message": text}
    images = _images(images)
    if images:
        value["images"] = images
    raw = json.dumps(value, ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class _Controller:
    def __init__(self, server, route):
        self.server, self.original_submit = server, route
        self.lock, self.entries = threading.RLock(), {}
        self.turn = threading.local()
        self.epoch = uuid.uuid4().hex
        self.submitting = ContextVar("rabbit_receipt_submit", default=None)
        self.original_routes = dict(server._methods)
        self.routes = {SUBMIT: self.submit, READ: self.read}
        self.patches, self.active = [], True
        self.had_marker = hasattr(server, "_rabbit_gateway_receipts")
        self.old_marker = getattr(server, "_rabbit_gateway_receipts", None)

    def _scope(self, session):
        return (session.get("cwd"), session.get("profile_home") or str(self.server._hermes_home), session.get("session_key"))

    def _same(self, entry):
        return (type(entry["session"]) is dict
                and self.server._sessions.get(entry["sid"]) is entry["session"]
                and self._scope(entry["session"]) == entry["scope"]
                and (not entry["consumed"] or entry["session"].get("agent") is entry["agent"]))

    def _fail(self, entry, reason):
        with self.lock:
            entry["reason"] = entry["reason"] or reason

    def _observe(self, entry, callback):
        try:
            callback()
        except Exception:
            self._fail(entry, "observer_unavailable")

    def _release(self, entry):
        with self.lock:
            entry["inflight"] = None
            entry["image_paths"] = None
            entry["executing"] = False

    def _scope_lost(self, entry):
        self._release(entry)
        entry.update(session=None, agent=None, scope=None, completion=None, response=None, reason="scope_changed")

    def _prune(self):
        # Caller owns self.lock. Keep every UUID tombstone; only release objects
        # whose original native registry ownership is gone or was never claimed.
        for entry in self.entries.values():
            if not self._same(entry):
                self._scope_lost(entry)
            elif (not entry["consumed"] and entry["inflight"] is not None
                    and (entry["session"].get("inflight_turn") is not entry["inflight"]
                         or not entry["session"].get("running"))):
                self._release(entry)

    @staticmethod
    def _error(rid, reason, unknown=False):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": 4093,
                "message": "Rabbit native completion could not be verified.",
                "data": {"rabbit_code": reason, "result_unknown": unknown}}}

    @staticmethod
    def _fields(params, submit=False):
        keys = {"session_id", "submission_id", "text"} if submit else {"session_id", "submission_id"}
        if (type(params) is not dict or not keys <= set(params)
                or set(params) - keys - ({"images"} if submit else set())):
            raise ValueError("invalid_arguments")
        sid, submission = params["session_id"], params["submission_id"]
        if (type(sid) is not str or _ID.fullmatch(sid) is None or type(submission) is not str
                or str(uuid.UUID(submission)) != submission):
            raise ValueError("invalid_arguments")
        digest, images = None, None
        if submit:
            if type(params["text"]) is not str:
                raise ValueError("invalid_arguments")
            raw = params["text"].encode("utf-8")
            if len(raw) > MAX_TEXT_BYTES:
                raise ValueError("invalid_arguments")
            digest = hashlib.sha256(raw).hexdigest()
            images = _images(params.get("images", []))
        return sid, submission, digest, images

    def submit(self, rid, params):
        try:
            sid, submission, digest, images = self._fields(params, True)
            request_digest = prompt_sha256(params["text"], images)
        except (ValueError, UnicodeError, AttributeError):
            return self._error(rid, "invalid_arguments")
        with self.server._sessions_lock, self.lock:
            if not self.active:
                return self._error(rid, "observer_not_installed")
            self._prune()
            if submission in self.entries:
                entry = self.entries[submission]
                if not self._same(entry):
                    self._scope_lost(entry)
                    return self._error(rid, "submission_conflict", True)
                if (entry["sid"] != sid or entry["text_sha256"] != digest
                        or entry["request_sha256"] != request_digest):
                    return self._error(rid, "submission_conflict", True)
                if entry["response"] is None:
                    return self._error(rid, "submission_unknown", True)
                response = deepcopy(entry["response"])
                response["id"] = rid
                return response
            if len(self.entries) >= MAX_ENTRIES:
                return self._error(rid, "receipt_capacity")
            session = self.server._sessions.get(sid)
            if type(session) is not dict:
                return self._error(rid, "session_unavailable")
            scope = self._scope(session)
            if any(type(v) is not str or not v for v in scope) or _ID.fullmatch(scope[2]) is None:
                return self._error(rid, "session_unavailable")
            entry = {"sid": sid, "submission": submission, "text_sha256": digest,
                     "request_sha256": request_digest, "image_paths": tuple(row["path"] for row in images),
                     "session": session,
                     "scope": scope, "agent": None, "inflight": None,
                     "consumed": False, "executing": False, "response": None, "completion": None, "reason": None}
            self.entries[submission] = entry
        token = self.submitting.set(entry)
        try:
            value = self.original_submit(rid, {"session_id": sid, "text": params["text"]})
        except BaseException:
            self._fail(entry, "native_submit_unknown")
            if not entry["consumed"]:
                self._release(entry)
            raise
        finally:
            self.submitting.reset(token)
        def remember():
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if type(value) is dict and len(encoded) <= MAX_ACK_BYTES:
                entry["response"] = deepcopy(value)
            if session.get("_compute_host_active") and not entry["consumed"]:
                self._fail(entry, "compute_host_unobserved")
            result = value.get("result") if type(value) is dict else None
            if not entry["consumed"] and ("error" in value or type(result) is dict and result.get("status") != "streaming"):
                self._release(entry)
        self._observe(entry, remember)
        return value

    def read(self, rid, params):
        if type(params) is dict and params == {"action": "capabilities"}:
            with self.lock:
                if not self.active:
                    return self._error(rid, "observer_not_installed")
                self._prune()
                return self.server._ok(rid, {
                    "schema_version": 1, "installed_commit": PIN, "connection_epoch": self.epoch,
                    "max_entries": MAX_ENTRIES, "max_answer_bytes": MAX_ANSWER_BYTES,
                    "mode": "current_connection"})
        try:
            sid, submission, _, _ = self._fields(params)
        except (ValueError, AttributeError):
            return self._error(rid, "invalid_arguments")
        with self.lock:
            if self.active:
                self._prune()
            entry = self.entries.get(submission)
            if not self.active:
                value = _unavailable("observer_not_installed")
            elif entry is None or entry["sid"] != sid:
                value = _unavailable("submission_unobserved")
            elif not self._same(entry):
                self._scope_lost(entry)
                value = _unavailable("scope_changed")
            elif entry["reason"] is not None:
                value = _unavailable(entry["reason"])
            elif entry["completion"] is None:
                value = _unavailable("not_completed")
            else:
                value = deepcopy(entry["completion"])
        return self.server._ok(rid, value)

    def _cache_completion(self, entry, value, result):
        if entry["reason"] is not None:
            return
        payload = value[0]
        if (not self._same(entry) or type(result) is not dict or result.get("completed") is not True
                or result.get("failed") is not False or result.get("interrupted") is not False
                or result.get("partial") is True or result.get("error") or type(payload) is not dict
                or payload.get("status") != "complete"):
            self._fail(entry, "terminal_unconfirmed")
            return
        text = payload.get("text")
        if type(text) is not str or type(result.get("final_response")) is not str:
            self._fail(entry, "completion_not_text")
            return
        raw = text.encode("utf-8")
        if raw != result["final_response"].encode("utf-8"):
            self._fail(entry, "delivered_content_changed")
        elif len(raw) > MAX_ANSWER_BYTES:
            entry["completion"] = _unavailable("answer_capacity", "incomplete")
        else:
            entry["completion"] = {"state": "available", "reason": "current_connection_result",
                "answer": {"text": text, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)},
                "source": {"kind": "hermes_gateway_completion", "submission_id": entry["submission"],
                    "runtime_sid": entry["sid"], "connection_epoch": self.epoch,
                    "request_text_sha256": entry["text_sha256"], "request_sha256": entry["request_sha256"],
                    "terminal_status": "completed"}}

    def wrappers(self):
        server = self.server
        old_start, old_invoke, old_complete = server._start_inflight_turn, server._invoke_agent, server._complete_turn_payload

        @wraps(old_start)
        def start_turn(session, text):
            entry = self.submitting.get()
            if entry is None or entry["session"] is not session:
                return old_start(session, text)
            # The pinned original _lock_in_submit_turn calls this while owning
            # its original history Lock. Never acquire it a second time.
            before = session.get("inflight_turn")
            value = old_start(session, text)
            def capture():
                current = session.get("inflight_turn")
                if type(current) is dict and current is not before and self._same(entry):
                    entry["inflight"] = current
                else:
                    self._fail(entry, "turn_unclaimed")
            self._observe(entry, capture)
            return value

        @wraps(old_invoke)
        def invoke(sid, session, st, prompt, run_message, streamer, images, display_kind, display_metadata):
            with self.lock:
                matches = [e for e in self.entries.values() if e["sid"] == sid and e["session"] is session
                           and not e["consumed"] and e["inflight"] is not None and e["inflight"] is session.get("inflight_turn")]
                entry = matches[0] if len(matches) == 1 else None
                if entry is not None:
                    self.turn.capture = {"entry": entry, "session": session, "inflight": entry["inflight"],
                                         "st": st, "result": None}
                    entry.update(consumed=True, executing=True, inflight=None, agent=st.agent)
                    def match_images():
                        try:
                            if (type(images) is not list or any(type(path) is not str for path in images)
                                    or tuple(images) != entry["image_paths"]):
                                self._fail(entry, "image_identity_changed")
                        finally:
                            entry["image_paths"] = None
                    self._observe(entry, match_images)
            try:
                value = old_invoke(sid, session, st, prompt, run_message, streamer, images, display_kind, display_metadata)
            except BaseException:
                if entry is not None:
                    self._fail(entry, "native_turn_unknown")
                    self._release(entry)
                    del self.turn.capture
                raise
            if entry is not None:
                self._observe(entry, lambda: self.turn.capture.update(result=st.result))
                entry["executing"] = False
            return value

        @wraps(old_complete)
        def complete(session, st, status_note, cols):
            capture = getattr(self.turn, "capture", None)
            entry = capture["entry"] if capture is not None and capture["st"] is st else None
            with self.lock:
                if entry is not None and (capture["session"] is not session or entry["session"] is not session
                        or capture["result"] is not st.result or capture["inflight"] is not session.get("inflight_turn")):
                    self._fail(entry, "completion_identity_changed")
            try:
                value = old_complete(session, st, status_note, cols)
            except BaseException:
                if entry is not None:
                    self._fail(entry, "native_completion_unknown")
                    self._release(entry)
                    del self.turn.capture
                raise
            if entry is not None:
                self._observe(entry, lambda: self._cache_completion(entry, value, st.result))
                self._release(entry)
                del self.turn.capture
            return value

        return [("_start_inflight_turn", old_start, start_turn), ("_invoke_agent", old_invoke, invoke),
                ("_complete_turn_payload", old_complete, complete)]

    def uninstall(self):
        with self.server._sessions_lock, self.lock:
            if not self.active or getattr(self.server, "_rabbit_gateway_receipts", None) is not self:
                raise ValueError("observer_not_installed")
            if any(e["executing"] for e in self.entries.values()) or any(s.get("running") for s in self.server._sessions.values()):
                raise ValueError("observer_busy")
            if (any(getattr(self.server, n) is not replacement for n, _, replacement in self.patches)
                    or any(self.server._methods.get(n) is not value for n, value in {**self.original_routes, **self.routes}.items())):
                raise ValueError("observer_changed")
            for name, original, _ in reversed(self.patches):
                setattr(self.server, name, original)
            for name in self.routes:
                self.server._methods.pop(name)
            if self.had_marker:
                self.server._rabbit_gateway_receipts = self.old_marker
            else:
                del self.server._rabbit_gateway_receipts
            self.entries.clear()
            self.active = False


def install_gateway_receipts(server, *, native_root, installed_commit, private_backend):
    """Install once in an empty backend whose --isolated fact the caller verified."""
    root = Path(native_root).resolve(strict=True)
    if getattr(server, "_rabbit_gateway_receipts", None) is not None:
        raise ValueError("observer_already_installed")
    route = _verify(server, root, installed_commit, private_backend)
    controller = _Controller(server, route)
    patches, changed = controller.wrappers(), []
    with server._sessions_lock:
        if server._sessions or SUBMIT in server._methods or READ in server._methods:
            raise ValueError("observer_install_not_idle")
        if server._methods != controller.original_routes or server._methods.get("prompt.submit") is not route:
            raise ValueError("native_route_changed")
        try:
            for name, original, replacement in patches:
                if getattr(server, name) is not original:
                    raise ValueError("native_function_changed")
                setattr(server, name, replacement)
                changed.append((name, original, replacement))
            server._methods.update(controller.routes)
            server._rabbit_gateway_receipts = controller
            controller.patches = patches
        except BaseException:
            for name, original, _ in reversed(changed):
                setattr(server, name, original)
            for name in controller.routes:
                server._methods.pop(name, None)
            if getattr(server, "_rabbit_gateway_receipts", None) is controller:
                if controller.had_marker:
                    server._rabbit_gateway_receipts = controller.old_marker
                else:
                    del server._rabbit_gateway_receipts
            raise
    return controller
