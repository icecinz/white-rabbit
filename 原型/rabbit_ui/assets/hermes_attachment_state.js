/* Pure metadata validation for Hermes' non-atomic native attachment ledger.
 * No DOM, HTTP, queue observation, prompt inference or mutation lives here. */
(() => {
  "use strict";
  const fields = ["schema_version", "workspace_id", "chat_id", "client_id", "revision", "identity", "owner_epoch", "raw_request_sha256", "request_sha256", "images", "phase", "confirmed_count", "created_at", "updated_at"];
  const identityFields = ["session_id", "session_key", "cwd", "profile_home"];
  const phases = new Set(["prepared", "attaching", "staged", "prompt_started", "prompt_rejected", "consumed", "discarding", "discarded"]);
  const terminals = new Set(["consumed", "discarded"]);
  const discardPhases = new Set(["prepared", "attaching", "staged", "prompt_rejected", "discarding"]);
  const completePhases = new Set(["staged", "prompt_started", "prompt_rejected", "consumed"]);
  const MAX_IMAGE_BYTES = 5 * 1024 * 1024, MAX_TOTAL_BYTES = 20 * 1024 * 1024;
  const fail = () => { throw Error("Hermes附件事务记录无法核验。"); };

  function nativePrototype(prototype, name) {
    if (!prototype) return false;
    const constructor = Object.getOwnPropertyDescriptor(prototype, "constructor");
    return !!constructor && Object.hasOwn(constructor, "value") && typeof constructor.value === "function"
      && constructor.value.prototype === prototype
      && Function.prototype.toString.call(constructor.value) === "function " + name + "() { [native code] }";
  }
  function plain(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    // Ordinary JSON objects from another realm are allowed; custom, null and
    // inherited application prototypes are not accepted as protocol records.
    return nativePrototype(prototype, "Object") && Object.getPrototypeOf(prototype) === null;
  }
  function record(value, expected) {
    if (!plain(value)) fail();
    const keys = Reflect.ownKeys(value), descriptors = Object.getOwnPropertyDescriptors(value);
    if (keys.length !== expected.length || keys.some(key => typeof key !== "string" || !expected.includes(key))
        || expected.some(key => !Object.hasOwn(descriptors, key) || !Object.hasOwn(descriptors[key], "value") || descriptors[key].enumerable !== true)) fail();
    return value;
  }
  function array(value) {
    if (!Array.isArray(value) || !nativePrototype(Object.getPrototypeOf(value), "Array") || value.length < 1 || value.length > 4) fail();
    const keys = Reflect.ownKeys(value), descriptors = Object.getOwnPropertyDescriptors(value);
    if (keys.length !== value.length + 1 || keys.some(key => typeof key !== "string" || key !== "length" && !/^[0-3]$/.test(key))) fail();
    for (let index = 0; index < value.length; index++) if (!Object.hasOwn(descriptors, index) || !Object.hasOwn(descriptors[index], "value") || descriptors[index].enumerable !== true) fail();
    return value;
  }
  const hex = (value, size) => typeof value === "string" && value.length === size && !/[^a-f0-9]/.test(value);
  const uuid = value => typeof value === "string" && value.length === 36 && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value);
  const session = value => typeof value === "string" && value.length > 0 && value.length <= 128 && /^[A-Za-z0-9]/.test(value) && !/[^A-Za-z0-9_.-]/.test(value);
  function path(value) {
    if (typeof value !== "string" || !value.startsWith("/") || Array.from(value).length > 4096 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(value)) return false;
    if (value === "/" || value === "//") return true;
    if (value.startsWith("///")) return false;
    return value.slice(value.startsWith("//") ? 2 : 1).split("/").every(part => part && part !== "." && part !== "..");
  }
  function timestamp(value) {
    if (typeof value !== "string" || value.length < 20 || value.length > 64) return false;
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(value);
    if (!match || match[0].length !== value.length) return false;
    const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
    if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return false;
    const days = [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    return day >= 1 && day <= days[month - 1];
  }
  function parse(value, scope) {
    if (value === undefined || value === null) return null;
    try {
      record(scope, ["workspace_id", "chat_id"]);
      record(value, fields);
      if (!hex(scope.workspace_id, 64) || !uuid(scope.chat_id) || value.schema_version !== 1
          || value.workspace_id !== scope.workspace_id || value.chat_id !== scope.chat_id
          || !uuid(value.client_id) || !Number.isSafeInteger(value.revision) || value.revision < 1
          || !hex(value.owner_epoch, 32) || !hex(value.raw_request_sha256, 64) || !hex(value.request_sha256, 64)
          || typeof value.phase !== "string" || !phases.has(value.phase)
          || !Number.isSafeInteger(value.confirmed_count) || value.confirmed_count < 0
          || !timestamp(value.created_at) || !timestamp(value.updated_at)) fail();
      const identity = record(value.identity, identityFields);
      if (!session(identity.session_id) || !session(identity.session_key) || !path(identity.cwd) || !path(identity.profile_home)) fail();
      const images = array(value.images), root = identity.profile_home + (identity.profile_home.endsWith("/") ? "" : "/")
        + "images/rabbit-native/" + value.workspace_id + "/" + value.chat_id + "/images/";
      let total = 0;
      const copies = images.map(image => {
        record(image, ["path", "sha256", "size_bytes", "mime"]);
        if (!hex(image.sha256, 64) || !Number.isSafeInteger(image.size_bytes) || image.size_bytes < 1 || image.size_bytes > MAX_IMAGE_BYTES
            || !["image/png", "image/jpeg"].includes(image.mime) || !path(image.path)
            || image.path !== root + image.sha256 + (image.mime === "image/png" ? ".png" : ".jpg")) fail();
        total += image.size_bytes;
        return { path: image.path, sha256: image.sha256, size_bytes: image.size_bytes, mime: image.mime };
      });
      if (total > MAX_TOTAL_BYTES || value.confirmed_count > copies.length
          || ["prepared", "attaching"].includes(value.phase) && value.confirmed_count >= copies.length
          || completePhases.has(value.phase) && value.confirmed_count !== copies.length) fail();
      return { schema_version: 1, workspace_id: value.workspace_id, chat_id: value.chat_id, client_id: value.client_id,
        revision: value.revision, identity: { session_id: identity.session_id, session_key: identity.session_key, cwd: identity.cwd, profile_home: identity.profile_home },
        owner_epoch: value.owner_epoch, raw_request_sha256: value.raw_request_sha256, request_sha256: value.request_sha256,
        images: copies, phase: value.phase, confirmed_count: value.confirmed_count, created_at: value.created_at, updated_at: value.updated_at };
    } catch (_) { fail(); }
  }
  function checked(value) {
    try {
      if (!plain(value)) return null;
      const descriptors = Object.getOwnPropertyDescriptors(value);
      if (!["workspace_id", "chat_id"].every(key => Object.hasOwn(descriptors, key) && Object.hasOwn(descriptors[key], "value"))) return null;
      return parse(value, { workspace_id: descriptors.workspace_id.value, chat_id: descriptors.chat_id.value });
    } catch (_) { return null; }
  }
  const active = value => { const row = checked(value); return row !== null && !terminals.has(row.phase); };
  const disposable = value => { const row = checked(value); return row !== null && discardPhases.has(row.phase); };
  function ownValue(value, key) {
    if (!plain(value)) return undefined;
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    return descriptor && Object.hasOwn(descriptor, "value") && descriptor.enumerable ? descriptor.value : undefined;
  }
  function owned(value, view) {
    try {
      const row = checked(value);
      if (!row || ownValue(view, "workspace_id") !== row.workspace_id || ownValue(view, "chat_id") !== row.chat_id || ownValue(view, "epoch") !== row.owner_epoch) return false;
      const binding = ownValue(view, "binding");
      return ownValue(binding, "chat_id") === row.chat_id
        && ownValue(binding, "runtime_session_id") === row.identity.session_id
        && ownValue(binding, "session_key") === row.identity.session_key
        && ownValue(binding, "cwd") === row.identity.cwd && ownValue(binding, "profile_home") === row.identity.profile_home;
    } catch (_) { return false; }
  }
  window.RabbitHermesAttachmentState = Object.freeze({ parse, active, disposable, owned });
})();
