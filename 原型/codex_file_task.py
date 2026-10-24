"""Codex file-task adapter using a private three-tool MCP, separate from chat.

The caller owns the task directory. This adapter reports completed native
answers and tool receipts; only the host's independent filesystem checks can
establish saved artifacts. Cancellation can leave previously saved files.
"""
import copy
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

import codex_adapter
from rabbit_content.file_task_files import FileTaskFiles, FileTaskError
from file_task_mcp import TOOLS


SERVER_NAME = 'rabbit_file_task'
MCP_PROGRAM = Path(__file__).with_name('file_task_mcp.py').resolve()
MAX_EVENT_CHARACTERS = 16 * 1024 * 1024
MAX_TEXT_CHARACTERS = 30000
MAX_TOOL_EVENTS = 128
RUN_TIMEOUT = 300
DISABLED = ('shell_tool', 'unified_exec', 'browser_use', 'computer_use',
    'image_generation', 'multi_agent', 'apps', 'plugins', 'memories', 'hooks',
    'goals', 'sleep_tool', 'skill_search', 'skill_mcp_dependency_install',
    'view_image', 'unbounded_connection_retries')


class FileTaskExecutionError(RuntimeError):
    def __init__(self, message, tool_events):
        self.tool_events = copy.deepcopy(tool_events)
        super().__init__(message)


def _toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def file_task_command(binary, task_root, route, working_directory, python=None):
    """Pure command builder; the root and Python are selected by the host."""
    root = Path(task_root).absolute()
    interpreter = str(Path(python or sys.executable).resolve())
    arguments = ['-B', str(MCP_PROGRAM), '--root', str(root)]
    tool_policy = ','.join(name + '={approval_mode="approve"}' for name in TOOLS)
    mcp = ('{command=' + _toml_string(interpreter) + ',args=[' +
        ','.join(_toml_string(value) for value in arguments) + '],cwd=' +
        _toml_string(root) + ',required=true,enabled=true,enabled_tools=[' +
        ','.join(_toml_string(value) for value in TOOLS) + '],startup_timeout_sec=15,'
        'tool_timeout_sec=10,default_tools_approval_mode="prompt",tools={' + tool_policy + '}}')
    command = [str(binary), 'exec', '--json', '--ephemeral', '--ignore-user-config',
        '--skip-git-repo-check', '--sandbox', 'read-only', '--color', 'never',
        '--model', route['model'], '-C', str(working_directory)]
    for feature in DISABLED:
        command.extend(('--disable', feature))
    command.extend(('--enable', 'skip_host_skill_discovery', '-c', 'approval_policy="never"',
        '-c', 'model_reasoning_effort=' + _toml_string(route['reasoning_effort']),
        '-c', 'model_provider="openai"', '-c', 'web_search="disabled"',
        '-c', 'mcp_servers={' + SERVER_NAME + '=' + mcp + '}', '-'))
    return command


def _tool_event(item, phase):
    if item.get('server') != SERVER_NAME or item.get('tool') not in TOOLS:
        raise ValueError('Unexpected file-task tool')
    result = item.get('result')
    failed = bool(item.get('error')) or isinstance(result, dict) and result.get('isError') is True
    value = {'type': 'tool', 'server': SERVER_NAME, 'tool': item['tool'],
             'phase': phase, 'failed': bool(failed)}
    arguments = item.get('arguments')
    if isinstance(arguments, str) and len(arguments) <= MAX_EVENT_CHARACTERS:
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = None
    path = arguments.get('path') if isinstance(arguments, dict) else None
    if (isinstance(path, str) and 0 < len(path) <= 240 and not path.startswith('/')
            and '\\' not in path and all(part and not part.startswith('.') for part in path.split('/'))
            and Path(path).suffix.casefold() in ('.md', '.txt')
            and all(ord(char) >= 32 and ord(char) != 127 for char in path)):
        value['path'] = path
    return value


def run(prompt, task_root, on_event, cancel_event, route):
    """Production signature shared with the Hermes file-task adapter."""
    return run_codex_task(prompt, task_root, route, on_event, cancel_event)


def run_codex_task(prompt, task_root, route, on_event, cancel_event):
    """Compatibility entry; the caller controls directory lifetime and scope."""
    if cancel_event.is_set():
        raise InterruptedError('已停止这次文件任务。')
    try:
        with FileTaskFiles(task_root, cancel_event=cancel_event) as files:
            return _execute_codex_task(prompt, task_root, route, on_event, cancel_event, files)
    except FileTaskError as error:
        if error.code == 'cancelled':
            raise InterruptedError('已停止这次文件任务。') from None
        raise FileTaskExecutionError('本轮文件任务目录未通过核验。', []) from None


def _execute_codex_task(prompt, task_root, route, on_event, cancel_event, files):
    """Use Codex's tool loop with exactly three new-file MCP tools.

    The caller owns task_root and its lifetime. Return tool receipts, never raw
    stderr or arbitrary MCP arguments. Invocation count is one CLI delegation;
    this does not independently count internal provider-network attempts.
    """
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 120000:
        raise ValueError('文件任务需要有界的非空文字。')
    prompt.encode('utf-8', errors='strict')
    if cancel_event.is_set():
        raise InterruptedError('已停止这次文件任务。')
    # The frozen host scope is also checked on each poll and before returning.
    catalog = files.tools()
    if {item['name'] for item in catalog} != set(TOOLS) or len(catalog) != 3:
        raise ValueError('本轮文件工具目录未通过核验。')
    files.check_cancelled()
    route = codex_adapter.validate_route(route)
    binary = codex_adapter.executable()
    if not binary:
        raise ValueError('没有找到 Codex CLI。')
    events, tool_events, process = queue.Queue(maxsize=16), [], None
    reader_stop = threading.Event()
    output, completed = '', False
    failure = None
    readers = []
    # Empty cwd prevents generated AGENTS.md/config files from becoming native
    # high-priority project instructions on the next task. Files are MCP-only.
    with tempfile.TemporaryDirectory(prefix='rabbit-codex-file-cwd-', dir='/private/tmp') as temporary:
        command = file_task_command(binary, task_root, route, temporary)
        if cancel_event.is_set():
            raise InterruptedError('已停止这次文件任务。')
        files.check_cancelled()
        try:
            process = subprocess.Popen(command, cwd=temporary, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                errors='replace', start_new_session=True)

            def stdout_reader():
                def put(value):
                    while not reader_stop.is_set():
                        try:
                            events.put(value, timeout=0.1)
                            return
                        except queue.Full:
                            pass
                try:
                    while not reader_stop.is_set():
                        line = process.stdout.readline(MAX_EVENT_CHARACTERS + 1)
                        if not line:
                            break
                        if len(line) > MAX_EVENT_CHARACTERS:
                            put(('invalid', None))
                            break
                        put(('line', line))
                finally:
                    put(('end', None))

            def stderr_reader():
                # Drain without retaining or publishing paths, keys or errors.
                while process.stderr.read(8192):
                    pass

            readers = [threading.Thread(target=stdout_reader, daemon=True),
                       threading.Thread(target=stderr_reader, daemon=True)]
            for reader in readers:
                reader.start()
            process.stdin.write(prompt)
            process.stdin.close()
            on_event({'type': 'status', 'text': 'Codex 正在处理本轮文件任务。'})
            started, event_count = time.monotonic(), 0
            while True:
                if cancel_event.is_set():
                    raise InterruptedError('已停止这次文件任务。')
                files.check_cancelled()
                if time.monotonic() - started > RUN_TIMEOUT:
                    raise TimeoutError('文件任务等待超时，已停止。')
                try:
                    event_type, raw = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if event_type == 'end':
                    break
                if event_type == 'invalid':
                    raise ValueError('文件任务事件超过容量。')
                event_count += 1
                if event_count > 4096:
                    raise ValueError('文件任务事件超过容量。')
                try:
                    event = json.loads(raw)
                except ValueError:
                    raise ValueError('文件任务返回了无效事件。') from None
                if not isinstance(event, dict):
                    raise ValueError('文件任务返回了无效事件。')
                kind, item = event.get('type'), event.get('item')
                if isinstance(item, dict) and kind in ('item.started', 'item.updated', 'item.completed'):
                    item_type = item.get('type')
                    if item_type == 'mcp_tool_call':
                        receipt = _tool_event(item, kind.split('.', 1)[1])
                        tool_events.append(receipt)
                        if len(tool_events) > MAX_TOOL_EVENTS:
                            raise ValueError('文件任务工具次数超过容量。')
                        on_event(copy.deepcopy(receipt))
                    elif item_type in ('command_execution', 'file_change', 'web_search') or (
                            isinstance(item_type, str) and item_type.endswith('tool_call')):
                        raise ValueError('文件任务出现了未允许的执行工具。')
                    elif item_type == 'agent_message' and kind == 'item.completed':
                        text = item.get('text')
                        if not isinstance(text, str) or len(text) > MAX_TEXT_CHARACTERS:
                            raise ValueError('文件任务回答超过容量或格式无效。')
                        output = text
                        on_event({'type': 'text', 'text': text})
                elif kind == 'turn.completed':
                    completed = True
                elif kind in ('turn.failed', 'error'):
                    failure = True
            return_code = process.wait(timeout=10)
            if cancel_event.is_set():
                raise InterruptedError('已停止这次文件任务。')
            files.check_cancelled()
            if return_code or failure or not completed or not output.strip():
                raise ValueError('Codex 文件任务未返回完整回答。')
            # Reading, listing, or explaining a task can finish without saving.
            # Never manufacture an artifact or a successful save receipt here.
            return {'text': output, 'model': route['model'], 'provider': 'ChatGPT',
                    'model_source': 'requested_route', 'tool_events': copy.deepcopy(tool_events)}
        except InterruptedError:
            raise
        except FileTaskError as error:
            if error.code == 'cancelled':
                raise InterruptedError('已停止这次文件任务。') from None
            raise FileTaskExecutionError('Codex 文件任务未通过执行核验。', tool_events) from None
        except Exception:
            raise FileTaskExecutionError('Codex 文件任务未通过执行核验。', tool_events) from None
        finally:
            if process is not None:
                reader_stop.set()
                codex_adapter.stop_process(process)
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
                for reader in readers:
                    reader.join(timeout=2)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


