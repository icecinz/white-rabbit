"""Read-only UI view of the local_history_mcp.py version 1 text index.

No refresh, main-index writes, or network access happen here. ``content``
is always the complete indexed text, which has already been redacted by the indexer.
``display_content`` is an optional presentation view, never a replacement archive.
Optional native image descriptions and image-only rows are merged for the app;
only a selected page invokes an explicitly supplied private materializer. Other
files, tool calls, reasoning, skipped oversized source lines, and secrets remain
outside coverage. Do not describe it as a lossless source archive.
"""

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import quote
from agent_registry import IDS as AGENTS, LABELS
from rabbit_content.history_records import source_phase, SOURCE_PHASES, PI_COVERAGE_MAX_BYTES, PI_CONTEXT_MAX_IDS
from rabbit_content.history_markdown_reference import reference_source


SCHEMA_VERSION = "1"
NATIVE_ATTACHMENT_PUBLIC_FIELDS = ("attachment_ordinal", "content_index", "field", "token", "fingerprint",
    "kind", "mime", "size_bytes", "state", "reason", "omitted_count", "attachment_id", "material_state", "material_reason")
NATIVE_ATTACHMENT_SOURCE_FIELDS = NATIVE_ATTACHMENT_PUBLIC_FIELDS[:11]
_AUTOMATIC_TAGS = (
    "environment_context", "recommended_plugins", "skills_instructions",
    "app-context", "permissions instructions", "collaboration_mode",
    "multi_agent_role", "multi_agent_mode",
)
_APPROVAL_PREFIXES = (
    "The following is the Codex agent history added since your last approval assessment.",
    "The following is the Codex agent history whose request action you are assessing.",
)
_AGENT_HEADER = re.compile(
    r"\AMessage Type: (NEW_TASK|MESSAGE|FINAL_ANSWER)\r?\n"
    r"Task name: ([^\r\n]*)\r?\nSender: ([^\r\n]*)\r?\nPayload:\r?\n"
)
_INTERNAL_SESSION_SQL = (
    "(s.kind='child' OR (s.agent='codex' AND EXISTS (SELECT 1 FROM messages internal_m "
    "WHERE internal_m.agent=s.agent AND internal_m.session_id=s.session_id "
    "AND internal_m.role='user' AND is_approval_context(internal_m.content))))"
)


def _is_approval_context(content):
    return any(str(content or "").lstrip().startswith(prefix) for prefix in _APPROVAL_PREFIXES)


def _integer(value, name, lower, upper):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError("Invalid " + name)
    return value


def _agent(value, allow_all=True):
    allowed = AGENTS + (("all",) if allow_all else ())
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("Invalid agent")
    return value


def history_order(agent, prefix=''):
    fields = ('ordinal','message_id') if agent in ('claude', 'pi', 'dsh') else ('timestamp','ordinal','message_id')
    return ','.join(prefix + name for name in fields)


def history_position(agent, row):
    return tuple(row[name] for name in history_order(agent).split(','))


def _query(value, required=False):
    if not isinstance(value, str) or len(value) > 400:
        raise ValueError("query must contain at most 400 characters")
    value = value.strip()
    if required and not value:
        raise ValueError("query is required")
    return value.split()


def _take_leading_block(text, tag):
    """Only remove a complete leading block; preserve quoted/embedded examples."""
    start = re.match(r"\s*<" + re.escape(tag) + r">", text)
    if not start:
        return None
    end = text.find("</" + tag + ">", start.end())
    if end < 0:
        return None
    return text[end + len(tag) + 3:]


def display_message(content, agent, role, kind="root", *, phase=''):
    """Return presentation metadata without changing or truncating indexed text.

    ``child_context`` describes a child session's source-role=user message. The
    index cannot prove which inherited messages were directly typed by a human.
    Only explicit, recognizable automation prefixes get stronger classifications.
    Unknown structures are shown verbatim rather than guessed away.
    """
    result = {"has_wrapper": False,
              "message_kind": ("assistant" if role == "assistant" else
                               "child_context" if kind == "child" else "user")}
    phase_info = source_phase(phase)
    if phase_info:
        return dict(phase_info, has_wrapper=False)
    if phase == 'incomplete_reply':
        return {"has_wrapper": False, "message_kind": 'incomplete_reply', 'phase_label': '未完成回复，未带入接续'}
    if role != "user" or agent != "codex":
        return result
    stripped = content.lstrip()
    if _is_approval_context(stripped):
        return {"display_content": "自动审批评估上下文；展开可查看索引原文。",
                "has_wrapper": True, "message_kind": "approval_context"}

    value = content
    removed = False
    while True:
        for tag in _AUTOMATIC_TAGS:
            remainder = _take_leading_block(value, tag)
            if remainder is not None:
                value, removed = remainder, True
                break
        else:
            break

    # AGENTS.md is separately injected into some Codex user-role records.
    if value.lstrip().startswith("# AGENTS.md instructions for "):
        match = re.match(r"\s*# AGENTS\.md instructions for [^\r\n]+\s*"
                         r"<INSTRUCTIONS>.*?</INSTRUCTIONS>\s*", value, re.S)
        if match:
            value, removed = value[match.end():], True

    header = _AGENT_HEADER.match(value.lstrip())
    if header:
        value = value.lstrip()[header.end():]
        result.update(message_kind="agent_message", agent_message_type=header.group(1),
                      agent_sender=header.group(3))
        removed = True
    else:
        notification = re.fullmatch(r"\s*<subagent_notification(?:\s[^<>]*)?>"
                                    r"(.*?)</subagent_notification>\s*", value, re.S)
        if notification:
            value = notification.group(1).strip()
            result["message_kind"] = "agent_message"
            removed = True
        elif re.fullmatch(r"\s*<turn_aborted>.*?</turn_aborted>\s*", value, re.S):
            value = "本轮已中断。"
            result["message_kind"] = "interruption"
            removed = True

    if removed:
        value = value.strip()
        if not value:
            value = "自动环境信息；展开可查看索引原文。"
            result["message_kind"] = "environment"
        result.update(display_content=value, has_wrapper=True)
    return result


def clean_for_context(message):
    """Clean a returned message for a new context, without mutating its archive.

    Recognized automation/collaboration records are omitted. ``child_context``
    alone is deliberately insufficient to omit text: child sessions may contain
    inherited human messages, and source-role=user is not an origin guarantee.
    History remains untrusted evidence, never new task instructions.
    """
    if not isinstance(message, dict):
        raise ValueError("message must be an object")
    content = message.get("content", "")
    if not isinstance(content, str):
        raise ValueError("message content must be text")
    provenance = message.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    agent = message.get("agent") or message.get("backend") or provenance.get("agent") or ""
    inferred = display_message(content, agent, message.get("role"),
                               message.get("kind") or provenance.get("kind") or "root", phase=message.get('phase') or '')
    # Common storage retains source content and provenance but may omit display
    # fields. Reclassify those originals instead of depending on an UI cache.
    presentation = dict(inferred)
    excluded = {
        "approval_context", "agent_context", "agent_message", "environment", "interruption",
    }
    if (message.get("message_kind") in excluded or
            presentation.get("message_kind") in excluded):
        return ""
    value = message.get("display_content", presentation.get("display_content", content))
    if not isinstance(value, str):
        raise ValueError("message content must be text")
    return value


def _excerpt(content, words, size=440):
    folded = content.casefold()
    positions = [folded.find(word.casefold()) for word in words]
    positions = [position for position in positions if position >= 0]
    start = max(0, min(positions) - size // 4) if positions else 0
    # A casefold may expand characters (e.g. ß). Map the found position back.
    if start and len(folded) != len(content):
        length, original = 0, 0
        for original, character in enumerate(content):
            if length >= start:
                break
            length += len(character.casefold())
        start = original
    end = min(len(content), start + size)
    return ("…" if start else "") + content[start:end] + ("…" if end < len(content) else "")


def _prepare_app_history_views(connection):
    """Create fixed TEMP views before query_only; main always stays mode=ro.

    Future/partial optional schemas are not guessed. The compatibility views
    have the same columns even when an old v1 index has no image tables.
    """
    version = connection.execute("SELECT value FROM meta WHERE key='native_attachments_version'").fetchone()
    enabled = version is not None and version[0] == "1"
    tables = {row[0] for row in connection.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
    image_columns = {row[1] for row in connection.execute("PRAGMA main.table_info(native_image_messages)")} if "native_image_messages" in tables else set()
    attachment_columns = {row[1] for row in connection.execute("PRAGMA main.table_info(native_attachments)")} if "native_attachments" in tables else set()
    image_enabled = enabled and {"agent", "session_id", "message_id", "role", "timestamp", "source_path", "source_line", "phase", "ordinal"}.issubset(image_columns)
    attachments_enabled = enabled and set(NATIVE_ATTACHMENT_SOURCE_FIELDS + ("agent", "session_id", "message_id", "sha256", "locator_json")).issubset(attachment_columns)
    if image_enabled:
        connection.execute("""CREATE TEMP VIEW app_history_messages AS
            SELECT agent,session_id,message_id,role,timestamp,content,source_path,source_line,phase,ordinal,
                0 AS native_image_only FROM main.messages
            UNION ALL SELECT im.agent,im.session_id,im.message_id,im.role,im.timestamp,'' AS content,
                im.source_path,im.source_line,im.phase,im.ordinal,1 AS native_image_only
            FROM main.native_image_messages im WHERE NOT EXISTS(SELECT 1 FROM main.messages tm
                WHERE tm.agent=im.agent AND tm.session_id=im.session_id AND tm.message_id=im.message_id)""")
    else:
        connection.execute("""CREATE TEMP VIEW app_history_messages AS
            SELECT agent,session_id,message_id,role,timestamp,content,source_path,source_line,phase,ordinal,
                0 AS native_image_only FROM main.messages""")
    if attachments_enabled:
        connection.execute("""CREATE TEMP VIEW app_history_attachments AS
            SELECT agent,session_id,message_id,attachment_ordinal,content_index,field,token,fingerprint,
                kind,mime,size_bytes,sha256,state,reason,omitted_count,locator_json FROM main.native_attachments""")
    else:
        connection.execute("""CREATE TEMP VIEW app_history_attachments AS SELECT
            '' AS agent,'' AS session_id,'' AS message_id,0 AS attachment_ordinal,-1 AS content_index,
            '' AS field,'' AS token,'' AS fingerprint,'' AS kind,'' AS mime,0 AS size_bytes,'' AS sha256,
            '' AS state,'' AS reason,0 AS omitted_count,'' AS locator_json WHERE 0""")


def attachment_summary(descriptors):
    """Count original image occurrences; effective material IDs deduplicate later."""
    summary = {"total": 0, "ready": 0, "unavailable": 0, "omitted": 0, "unknown_omitted": False, "reasons": {}}
    for item in descriptors:
        if item.get("kind") == "inspection_summary":
            count = item.get("omitted_count")
            if type(count) is int and count >= 0:
                summary["omitted"] += count
                summary["total"] += count
            else:
                summary["unknown_omitted"] = True
            reason = item.get("reason") or item.get("material_reason") or "inspection_unavailable"
            summary["reasons"][reason] = summary["reasons"].get(reason, 0) + (count if type(count) is int and count > 0 else 1)
        else:
            summary["total"] += 1
            ready = item.get("material_state") == "ready" and isinstance(item.get("attachment_id"), str) and re.fullmatch(r"img_[0-9a-f]{32}", item["attachment_id"])
            summary["ready" if ready else "unavailable"] += 1
            if not ready:
                reason = item.get("reason") or item.get("material_reason") or "material_unavailable"
                summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
    return summary


class HistoryView:
    """Stateless, standard-library-only UI API over an existing private index."""

    def __init__(self, index_path, materializer=None):
        self.index_path = Path(index_path).expanduser().resolve()
        self.materializer = materializer

    def _open(self):
        # mode=ro does not create a missing index. query_only is a second guard.
        uri = "file:" + quote(str(self.index_path), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                raise ValueError("Unsupported history index schema")
            _prepare_app_history_views(connection)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
        except Exception:
            connection.close()
            raise
        connection.create_function("contains_fold", 2,
                                   lambda text, word: int(str(word).casefold() in str(text or "").casefold()))
        connection.create_function("is_approval_context", 1,
                                   lambda text: int(_is_approval_context(text)))
        return connection

    def stats(self):
        with closing(self._open()) as connection:
            meta = dict(connection.execute("SELECT key,value FROM meta"))
            counts = {agent: {"sessions": 0, "messages": 0} for agent in AGENTS}
            for row in connection.execute("SELECT agent,COUNT(*) n FROM sessions GROUP BY agent"):
                if row["agent"] in counts:
                    counts[row["agent"]]["sessions"] = row["n"]
            for row in connection.execute("SELECT agent,COUNT(*) n FROM app_history_messages GROUP BY agent"):
                if row["agent"] in counts:
                    counts[row["agent"]]["messages"] = row["n"]
            try:
                source_stats = json.loads(meta.get("stats", "{}"))
            except (ValueError, TypeError):
                source_stats = {}
            if not isinstance(source_stats, dict):
                source_stats = {}
            structured = source_stats.get("coverage")
            structured = structured if isinstance(structured, dict) else {}
            coverage = {"text_only": True, "secrets_redacted": True,
                        "skipped_large_lines": source_stats.get("skipped_large_lines", 0),
                        "skipped_invalid_lines": source_stats.get("skipped_invalid_lines", 0),
                        "source_errors": source_stats.get("source_errors", 0),
                        "count_basis": "source_scan_before_deduplication"}
            for key in ("structured_messages", "nontext_blocks_skipped", "attachment_blocks_skipped",
                        "attachment_only_messages_skipped", "invalid_structured_payloads",
                        "unsupported_structured_payloads"):
                value = structured.get(key)
                coverage[key] = value if type(value) is int and 0 <= value <= 100000000 else None
            skipped = source_stats.get("skipped_large_lines", 0)
            image_version = meta.get("native_attachments_version") == "1"
            notice = ("文字索引保持脱敏；已识别的旧图按需读取，缺失、不支持及超限图片保留状态；工具和推理未收录。"
                      if image_version else "已索引用户与助手文本，内容已脱敏；图像、附件、工具和推理不在此索引内。")
            if skipped:
                notice += "有 " + str(skipped) + " 条超大源记录未导入。"
            if coverage["attachment_only_messages_skipped"]:
                notice += "来源扫描遇到 " + str(coverage["attachment_only_messages_skipped"]) + " 条仅含附件的消息，未收入文字索引。"
            if coverage["invalid_structured_payloads"]:
                notice += "有 " + str(coverage["invalid_structured_payloads"]) + " 条结构化记录无法解析，未作为文字导入。"
            if coverage["unsupported_structured_payloads"]:
                notice += "有 " + str(coverage["unsupported_structured_payloads"]) + " 条结构化记录含暂不支持的部分，已保留可识别文字。"
            if structured:
                notice += "这些数量按来源扫描统计，可能含重复副本。"
            native_images = {"version": 1 if image_version else None,
                "image_only_messages": connection.execute("SELECT COUNT(*) FROM app_history_messages WHERE native_image_only=1").fetchone()[0],
                "descriptors": connection.execute("SELECT COUNT(*) FROM app_history_attachments").fetchone()[0]}
            return {"built_at": meta.get("built_at", ""), "agents": counts, "notice": notice,
                    "coverage": coverage, "native_images": native_images}

    def _session(self, connection, row):
        session = dict(row)
        if session['agent'] == 'claude':
            session['coverage'] = self._claude_coverage(connection, session['session_id'])
        elif session['agent'] == 'pi':
            session['coverage'] = self._pi_coverage(connection, session['session_id'])
        elif session['agent'] == 'dsh':
            from dsh_history import read_coverage
            session['coverage'] = read_coverage(connection, session['session_id'])
        aggregate = connection.execute("""SELECT COUNT(*) AS message_count,MAX(timestamp) AS last_timestamp,
            SUM(native_image_only) AS image_only_messages FROM app_history_messages WHERE agent=? AND session_id=?""",
            (session["agent"], session["session_id"])).fetchone()
        session["text_message_count"] = session.get("message_count", 0)
        session["message_count"] = aggregate["message_count"]
        session["image_only_messages"] = aggregate["image_only_messages"] or 0
        if session["image_only_messages"] and aggregate["last_timestamp"]:
            session["updated_at"] = aggregate["last_timestamp"]
        session["archived"] = bool(session["archived"])
        session["source_title"] = session["title"]
        session["title_generated"] = not bool(session["title"].strip())
        if not session["title_generated"]:
            return session
        candidates = connection.execute(
            "SELECT content,role,phase FROM app_history_messages WHERE agent=? AND session_id=? AND role='user' "
            "ORDER BY " + history_order(session['agent']), (session["agent"], session["session_id"]))
        for candidate in candidates:
            presentation = display_message(candidate["content"], session["agent"], "user", session["kind"], phase=candidate['phase'])
            message_kind = presentation["message_kind"]
            if message_kind == "approval_context":
                session["title"] = "Codex 自动审批评估"
                return session
            if message_kind in ("environment", "interruption", "agent_message", *SOURCE_PHASES):
                continue
            title = presentation.get("display_content", candidate["content"]).strip()
            if title:
                title = re.sub(r"\s+", " ", title)
                session["title"] = title[:80] + ("…" if len(title) > 80 else "")
                return session
        session["title"] = LABELS.get(session['agent'], '助手') + " 会话 " + session["session_id"][:8]
        if session["image_only_messages"]:
            session["title"] = LABELS.get(session['agent'], '助手') + " 图片对话 " + session["session_id"][:8]
        return session

    @staticmethod
    def _pi_coverage(connection, session_id, *, internal=False):
        unavailable = {'continuable': False, 'context_mode': 'text_projection',
            'notice': 'Pi来源完整性暂时无法核对；可以查看或引用具体原文，尚不能整段接续。'}
        row = connection.execute("SELECT value FROM meta WHERE key='pi_session_coverage'").fetchone()
        try:
            if row is None or not isinstance(row[0], str) or len(row[0].encode()) > PI_COVERAGE_MAX_BYTES:
                return unavailable
            item = json.loads(row[0]).get(session_id)
            if (not isinstance(item, dict) or type(item.get('continuable')) is not bool
                    or item.get('source_version') != 3 or item.get('context_mode') != 'text_projection'):
                return unavailable
            result = dict(continuable=item['continuable'], context_mode='text_projection')
            for key in ('summary_count','text_messages','image_only_messages','generated_context_messages',
                        'omitted_image_blocks','omitted_tool_blocks','omitted_thinking_blocks',
                        'missing_parents','duplicate_ids','id_conflict','unparsed_lines','invalid_entries',
                        'invalid_header','unsupported_version','ambiguous_copies','unknown_context_records',
                        'unsupported_content_blocks','invalid_message_records','unsupported_compaction',
                        'context_image_messages','context_omitted_tool_blocks','context_omitted_thinking_blocks',
                        'context_omitted_image_blocks','omitted_failed_assistant_messages','omitted_excluded_context_messages'):
                value = item.get(key)
                if type(value) is not int or not 0 <= value <= 10**9:
                    return unavailable
                result[key] = value
            if type(item.get('cycle')) is not bool:
                return unavailable
            result['cycle'] = item['cycle']
            if type(item.get('is_exported_branch')) is not bool:
                return unavailable
            result['is_exported_branch'] = item['is_exported_branch']
            if any(result[key] for key in ('cycle','missing_parents','duplicate_ids','id_conflict',
                    'unparsed_lines','invalid_entries','invalid_header','unsupported_version',
                    'ambiguous_copies','unknown_context_records','unsupported_content_blocks',
                    'invalid_message_records','unsupported_compaction')):
                result['continuable'] = False
            ids = item.get('context_message_ids')
            if (not isinstance(ids, list) or not ids or len(ids) > PI_CONTEXT_MAX_IDS
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 256
                           or any(ord(ch) < 32 or ord(ch) == 127 for ch in value) for value in ids)
                    or len(set(ids)) != len(ids)):
                return unavailable
            transforms = item.get('context_transforms')
            if not isinstance(transforms, dict) or set(transforms) != set(ids):
                return unavailable
            expected_phases = {'user':'', 'assistant':'', 'custom':'custom_context',
                'bashExecution':'bash_context', 'compactionSummary':'compact_summary', 'branchSummary':'branch_summary'}
            rows = connection.execute("SELECT message_id,role,phase FROM app_history_messages WHERE agent='pi' "
                "AND session_id=? AND message_id IN (" + ','.join('?' for _ in ids) + ')', [session_id,*ids]).fetchall()
            if len(rows) != len(ids):
                return unavailable
            for message in rows:
                transform = transforms[message['message_id']]
                if not isinstance(transform, dict):
                    return unavailable
                native_role = transform.get('native_role')
                if (native_role not in expected_phases or transform.get('phase') != expected_phases[native_role]
                        or message['phase'] != transform['phase'] or transform.get('context_role') != message['role']
                        or message['role'] != ('assistant' if native_role == 'assistant' else 'user')):
                    return unavailable
                if native_role == 'bashExecution' and transform.get('exclude_from_context') is not False:
                    return unavailable
                if native_role == 'custom' and (type(transform.get('display')) is not bool
                        or not isinstance(transform.get('custom_type'), str) or not 1 <= len(transform['custom_type']) <= 256):
                    return unavailable
            result['context_message_count'] = len(ids)
            result['branch_kind'] = item.get('branch_kind') if item.get('branch_kind') in ('main','archive') else 'unknown'
            result['notice'] = ('Pi文字接续按当前分支的有效摘要和保留段落组织；浏览仍显示旧原文。'
                '工具、思考、原生扩展和系统提示不回放，原图尚未接入。'
                if result['continuable'] else unavailable['notice'])
            if internal:
                result['context_message_ids'] = ids
            return result
        except (ValueError, TypeError, AttributeError, RecursionError):
            return unavailable

    def context_selection(self, connection, agent, session_id):
        """Ordered effective source IDs, or None for a full-chain source."""
        if agent == 'dsh':
            from dsh_history import read_coverage
            coverage = read_coverage(connection, session_id, internal=True)
        elif agent == 'pi':
            coverage = self._pi_coverage(connection, session_id, internal=True)
        else:
            return None
        ids = coverage.pop('context_message_ids', []) if coverage['continuable'] else []
        return {'ids': ids, 'coverage': coverage}

    @staticmethod
    def _claude_coverage(connection, session_id):
        unavailable = {'continuable': False, 'notice': 'Claude来源完整性暂时无法核对；可以查看或引用具体原文，尚不能整段接续。'}
        row = connection.execute("SELECT value FROM meta WHERE key='claude_session_coverage'").fetchone()
        try:
            if row is None or not isinstance(row[0], str) or len(row[0].encode('utf-8')) > 2*1024*1024:
                return unavailable
            item = json.loads(row[0]).get(session_id)
            if not isinstance(item, dict) or type(item.get('continuable')) is not bool:
                return unavailable
            result = {'continuable': item['continuable']}
            for name in ('summary_count','text_messages','omitted_image_blocks','omitted_tool_blocks',
                         'omitted_thinking_blocks','omitted_meta_messages',
                         'missing_parents','uuid_conflict','unparsed_lines','unsupported_compaction',
                         'invalid_message_records','ambiguous_copies'):
                value = item.get(name, 0)
                if type(value) is not int or not 0 <= value <= 10**9:
                    return unavailable
                result[name] = value
            if type(item.get('cycle',False)) is not bool:
                return unavailable
            result['cycle'] = item.get('cycle',False)
            if any(result[name] for name in ('cycle','missing_parents','uuid_conflict','unparsed_lines',
                                             'unsupported_compaction','invalid_message_records','ambiguous_copies')):
                result['continuable'] = False
            result['branch_kind'] = item.get('branch_kind') if item.get('branch_kind') in ('main','archive','child') else 'unknown'
            result['notice'] = ('Claude文字已按原始消息链组织；思考、工具结果不作为对话文字，原图尚未接入。'
                                if result['continuable'] else unavailable['notice'])
            return result
        except (ValueError,TypeError,AttributeError,RecursionError):
            return unavailable

    @staticmethod
    def _page(offset, limit, max_limit):
        return (_integer(offset, "offset", 0, 1000000000),
                _integer(limit, "limit", 1, max_limit))

    def list_sessions(self, agent="all", q="", offset=0, limit=50, include_internal=False):
        agent = _agent(agent)
        words = _query(q)
        offset, limit = self._page(offset, limit, 200)
        if not isinstance(include_internal, bool):
            raise ValueError("include_internal must be boolean")
        clauses, parameters = [], []
        if agent != "all":
            clauses.append("s.agent=?")
            parameters.append(agent)
        for word in words:
            clauses.append("(contains_fold(s.title,?) OR EXISTS (SELECT 1 FROM app_history_messages m "
                           "WHERE m.agent=s.agent AND m.session_id=s.session_id AND contains_fold(m.content,?)))")
            parameters.extend((word, word))
        # Classify only known child metadata or the exact approval prefix. Never
        # hide a normal root session just because its body mentions "agent".
        internal = _INTERNAL_SESSION_SQL
        base_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        internal_where = (base_where + " AND " if clauses else " WHERE ") + internal
        if not include_internal:
            clauses.append("NOT " + internal)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._open()) as connection:
            hidden = connection.execute("SELECT COUNT(*) FROM sessions s" + internal_where,
                                        parameters).fetchone()[0]
            total = connection.execute("SELECT COUNT(*) FROM sessions s" + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT s.* FROM sessions s" + where +
                                      " ORDER BY CASE WHEN EXISTS(SELECT 1 FROM app_history_messages im "
                                      "WHERE im.agent=s.agent AND im.session_id=s.session_id AND im.native_image_only=1) "
                                      "THEN COALESCE((SELECT MAX(am.timestamp) FROM app_history_messages am WHERE am.agent=s.agent "
                                      "AND am.session_id=s.session_id),s.updated_at) ELSE s.updated_at END DESC,s.agent,s.session_id LIMIT ? OFFSET ?",
                                      parameters + [limit, offset]).fetchall()
            sessions = [self._session(connection, row) for row in rows]
        following = offset + len(sessions)
        return {"sessions": sessions, "total": total,
                "next_offset": following if following < total else None,
                "hidden_internal_count": hidden}

    def indexed_session_metadata(self, agent, session_id):
        """Exact existing source metadata; no message bodies or resume checks."""
        source = reference_source(agent, session_id)

        def state():
            info = self.index_path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('History index is not a regular file')
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        before = state()
        with closing(self._open()) as connection:
            row = connection.execute('SELECT title FROM sessions WHERE agent=? AND session_id=?',
                                     (agent, session_id)).fetchone()
            if row is None:
                raise KeyError('Indexed reference source is unavailable')
            title = row['title']
            if type(title) is not str or len(title) > 4096:
                raise ValueError('Invalid indexed reference title')
            title.encode('utf-8')
        if state() != before:
            raise ValueError('History index changed during metadata read')
        return dict(source, title=title)

    def indexed_text_snapshot(self, agent, session_ids):
        """All indexed text from exact sources in one read-only transaction.

        This is the redacted index's projection, not complete native state.
        No display/context transforms, attachment reads or source refreshes run.
        """
        if (type(agent) is not str or agent not in ('codex', 'pi', 'hermes', 'claude', 'dsh')
                or type(session_ids) not in (list, tuple)
                or not 1 <= len(session_ids) <= (128 if agent == 'hermes' else 1)):
            raise ValueError('Invalid indexed snapshot sources')
        for identity in session_ids:
            if (type(identity) is not str or not 1 <= len(identity) <= 256
                    or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                           or 0xD800 <= ord(char) <= 0xDFFF for char in identity)):
                raise ValueError('Invalid indexed snapshot session_id')
        if len(set(session_ids)) != len(session_ids):
            raise ValueError('Duplicate indexed snapshot session_id')
        identities = sorted(session_ids)

        def index_state():
            info = self.index_path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('History index is not a regular file')
            return (info.st_dev, info.st_ino, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)

        before = index_state()
        placeholders = ','.join('?' for _ in identities)
        parameters = [agent, *identities]
        sources = []
        with closing(self._open()) as connection:
            existing = {row['session_id'] for row in connection.execute(
                'SELECT session_id FROM sessions WHERE agent=? AND session_id IN ('
                + placeholders + ')', parameters)}
            if existing != set(identities):
                raise KeyError(next(identity for identity in identities if identity not in existing))
            built = connection.execute("SELECT value FROM meta WHERE key='built_at'").fetchone()
            if built is None or type(built[0]) is not str:
                raise ValueError('History index built_at is unavailable')
            indexed_at = built[0]
            indexed_at.encode('utf-8')
            total = connection.execute(
                'SELECT COUNT(*) FROM app_history_messages WHERE agent=? AND session_id IN ('
                + placeholders + ')', parameters).fetchone()[0]
            if total > 20000:
                raise ValueError('Indexed snapshot exceeds 20000 records')
            coverage_key = {'pi': 'pi_session_coverage', 'claude': 'claude_session_coverage',
                            'dsh': 'dsh_session_coverage'}.get(agent)
            has_coverage = coverage_key is not None and connection.execute(
                'SELECT 1 FROM meta WHERE key=?', (coverage_key,)).fetchone() is not None
            content_bytes = 0
            for identity in identities:
                coverage = {}
                if has_coverage:
                    if agent == 'pi':
                        coverage = self._pi_coverage(connection, identity)
                    elif agent == 'claude':
                        coverage = self._claude_coverage(connection, identity)
                    else:
                        from dsh_history import read_coverage
                        coverage = read_coverage(connection, identity)
                messages = []
                for row in connection.execute(
                        'SELECT message_id,role,phase,timestamp,ordinal,content,native_image_only '
                        'FROM app_history_messages WHERE agent=? AND session_id=? ORDER BY '
                        + history_order(agent), (agent, identity)):
                    message = dict(row)
                    if type(message['content']) is not str:
                        raise ValueError('Invalid indexed snapshot content')
                    content_bytes += len(message['content'].encode('utf-8'))
                    if content_bytes > 16 * 1024 * 1024:
                        raise ValueError('Indexed snapshot exceeds 16 MiB of UTF-8 content')
                    message['native_image_only'] = bool(message['native_image_only'])
                    messages.append(message)
                sources.append({'session_id': identity, 'coverage': coverage, 'messages': messages})
        if index_state() != before:
            raise ValueError('History index changed during snapshot')
        return {'agent': agent, 'session_ids': identities, 'indexed_at': indexed_at, 'sources': sources}

    def get_session(self, agent, session_id, offset=0, limit=100, *, for_context=False):
        agent = _agent(agent, allow_all=False)
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("Invalid session_id")
        offset, limit = self._page(offset, limit, 200)
        if type(for_context) is not bool:
            raise ValueError('Invalid context selection')
        with closing(self._open()) as connection:
            row = connection.execute("SELECT * FROM sessions WHERE agent=? AND session_id=?",
                                     (agent, session_id)).fetchone()
            if row is None:
                return {"session": None, "messages": [], "total": 0, "next_offset": None}
            session = self._session(connection, row)
            selection = self.context_selection(connection, agent, session_id) if for_context else None
            if selection is not None:
                identities = selection['ids']
                total, page = len(identities), identities[offset:offset+limit]
                session['coverage'] = selection['coverage']
                if page:
                    values = ','.join('(?,?)' for _ in page)
                    parameters = [value for pair in enumerate(page) for value in (pair[1],pair[0])]
                    rows = connection.execute('WITH selected(message_id,position) AS (VALUES ' + values + ') '
                        'SELECT m.message_id,role,content,timestamp,phase,source_path,source_line,ordinal '
                        'FROM app_history_messages m JOIN selected p ON m.message_id=p.message_id '
                        'WHERE agent=? AND session_id=? ORDER BY p.position', [*parameters,agent,session_id])
                else:
                    rows = ()
            else:
                total = connection.execute("SELECT COUNT(*) FROM app_history_messages WHERE agent=? AND session_id=?",
                                           (agent, session_id)).fetchone()[0]
                rows = connection.execute("SELECT message_id,role,content,timestamp,phase,source_path,source_line,ordinal "
                                          "FROM app_history_messages WHERE agent=? AND session_id=? "
                                          "ORDER BY " + history_order(agent) + " LIMIT ? OFFSET ?",
                                          (agent, session_id, limit, offset))
            messages = []
            for row in rows:
                message = dict(row)
                message["message_index"] = offset + len(messages)
                message.update(display_message(message["content"], agent, message["role"], session["kind"], phase=message['phase']))
                # phase itself is untouched, including Codex's final_answer spelling.
                message.setdefault("phase_label", {"commentary": "进度", "final_answer": "回复", "final": "回复"}.get(
                    message["phase"], "助手消息" if message["role"] == "assistant" else ""))
                private_descriptors = connection.execute("SELECT * FROM app_history_attachments "
                    "WHERE agent=? AND session_id=? AND message_id=? ORDER BY attachment_ordinal",
                    (agent, session_id, message["message_id"])).fetchall()
                descriptors, attachment_ids = [], []
                for private in private_descriptors:
                    original = dict(private)
                    result = {key: original.get(key) for key in NATIVE_ATTACHMENT_SOURCE_FIELDS}
                    result.update(attachment_id=None,
                        material_state="not_materialized" if result.get("state") == "embedded" else "unavailable",
                        material_reason="not_materialized" if result.get("state") == "embedded" else result.get("reason") or "native_unavailable")
                    if self.materializer is not None:
                        try:
                            resolved = self.materializer.materialize(agent, session_id, message["role"], original)
                            if not isinstance(resolved, dict):
                                raise ValueError("Invalid materializer result")
                            # The public projection is fixed. Even an adapter
                            # accidentally returning a private locator cannot
                            # expand this history API's payload.
                            result.update({key: resolved.get(key) for key in ("attachment_id", "material_state", "material_reason")})
                        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                            result.update(attachment_id=None, material_state="unavailable", material_reason="materialization_unavailable")
                    material_id = result.get("attachment_id")
                    if result.get("material_state") == "ready":
                        if not isinstance(material_id, str) or not re.fullmatch(r"img_[0-9a-f]{32}", material_id):
                            result.update(attachment_id=None, material_state="unavailable", material_reason="descriptor_invalid")
                        elif material_id not in attachment_ids:
                            attachment_ids.append(material_id)
                    else:
                        result["attachment_id"] = None
                    descriptors.append(result)
                message.update(native_attachments=descriptors, attachment_ids=attachment_ids,
                    attachment_summary=attachment_summary(descriptors))
                messages.append(message)
        following = offset + len(messages)
        return {"session": session, "messages": messages, "total": total,
                "next_offset": following if following < total else None}

    def search(self, q, agent="all", offset=0, limit=30, include_internal=False):
        agent = _agent(agent)
        words = _query(q, required=True)
        offset, limit = self._page(offset, limit, 100)
        if not isinstance(include_internal, bool):
            raise ValueError("include_internal must be boolean")
        clauses = ["contains_fold(m.content,?)" for word in words]
        parameters = list(words)
        if agent != "all":
            clauses.append("m.agent=?")
            parameters.append(agent)
        if not include_internal:
            clauses.append("NOT " + _INTERNAL_SESSION_SQL)
        where = " WHERE " + " AND ".join(clauses)
        joined = " FROM app_history_messages m JOIN sessions s ON s.agent=m.agent AND s.session_id=m.session_id"
        with closing(self._open()) as connection:
            total = connection.execute("SELECT COUNT(*)" + joined + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT m.*,s.kind" + joined + where +
                                      " ORDER BY m.timestamp DESC,m.agent,m.session_id,m.ordinal DESC,m.message_id "
                                      "LIMIT ? OFFSET ?", parameters + [limit, offset]).fetchall()
            results, titles = [], {}
            for row in rows:
                key = (row["agent"], row["session_id"])
                if key not in titles:
                    session_row = connection.execute("SELECT * FROM sessions WHERE agent=? AND session_id=?", key).fetchone()
                    titles[key] = self._session(connection, session_row)["title"]
                presentation = display_message(row["content"], row["agent"], row["role"], row["kind"], phase=row['phase'])
                visible = presentation.get("display_content", row["content"])
                use_visible = all(word.casefold() in visible.casefold() for word in words)
                # Source ordinal can be a sparse JSONL line number. Count earlier
                # messages in this session using its composite order index, not
                # the search-result position or all-history rowid. This is the
                # exact ORDER BY used by get_session, including message-id ties.
                order_fields = history_order(row['agent'])
                position_values = history_position(row['agent'], row)
                message_index = connection.execute(
                    "SELECT COUNT(*) FROM app_history_messages prior WHERE prior.agent=? AND prior.session_id=? "
                    "AND (" + order_fields + ") < (" + ','.join('?' for _ in position_values) + ")",
                    (row["agent"], row["session_id"], *position_values)
                ).fetchone()[0]
                results.append({"agent": row["agent"], "session_id": row["session_id"],
                                "title": titles[key], "role": row["role"], "message_id": row["message_id"],
                                "message_index": message_index, "ordinal": row["ordinal"],
                                "timestamp": row["timestamp"], "phase": row["phase"],
                                "message_kind": presentation["message_kind"], "kind": row["kind"],
                                "has_wrapper": presentation["has_wrapper"],
                                "excerpt_source": "display" if use_visible else "indexed_original",
                                "excerpt": _excerpt(visible if use_visible else row["content"], words)})
        following = offset + len(results)
        return {"results": results, "total": total,
                "next_offset": following if following < total else None}
