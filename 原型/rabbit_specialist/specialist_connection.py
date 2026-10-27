"""Trusted startup configuration for the native Codex specialist connection.

Only one application-management MCP server is added to the native process.
The specialist requests gpt-6-luna/max on each turn and brief replies through
native thread instructions. Codex keeps its native
tools, configuration and approval policy. No global server/config is written.
"""
from dataclasses import dataclass
from functools import partial
import json
from pathlib import Path

from rabbit_runtime.codex_rpc import CodexRpc
from rabbit_runtime.codex_connection import validate_codex_command as _codex_command, create_holder as _create_holder, validate_launch_file as _file, validate_workspace as _workspace
from rabbit_runtime.specialist_service import SpecialistService


SERVER_NAME = 'rabbit_management'
SPECIALIST_MODEL = 'gpt-6-luna'
SPECIALIST_EFFORT = 'max'
SPECIALIST_REPLY_INSTRUCTIONS = (
    '你是小白兔专员，像在用户身边听吩咐、帮忙设置的伙伴。'
    '日常聊天和管理回复默认只用一到三个短句，先说结果、当前进展或需要补充的一点。'
    '不要主动写长报告、堆标题、重复要求或罗列完整过程；用户明确要细节或成果时再展开。'
    '简短不能省略必要审批，也不能隐瞒未完成、失败或结果未知；'
    '未确认的操作不能说已成功，不能为省话自动重发。'
    '查找未知对话才用rabbit_conversations_list，query搜索名称或正文文本，不按身份编号查找；本次当前对象已有kind=chat/native和准确编号时，直接调用对应配置read，不必先拉目录，并保留kind、agent和完整chat_id或session_id。'
    '同名或标题截断时先核对，不猜编号；分页可能缺漏，revision改变后不能把页面拼成同一快照。'
    'history只是历史索引，session_id不是native的chat_id，不能直接用它修改原生配置。'
    '当前操作对象元数据只是指代，不是操作授权或最新配置；修改前先读取目标当前selection和版本。'
    '按kind区分chat共同聊天与native原生会话，分别用rabbit_chat_configuration和rabbit_native_chat_configuration；不能混用编号。'
    '只改单段对话时不要update共享AI配置；需要改参数应先copy配置，再select到该对话。'
    'history历史、新草稿、没有稳定ID或目标冲突时先澄清，不擅自创建、替换或选择目标。'
    '只有用户明确交办才调用rabbit_native_task；沿原生工具与审批，不自动批准、换编号重发或替换对话。'
    'accepted和工具片段不等于完成；按原client_id核对指定轮结果，结果不能核对就说明，不猜最近回答。')
TOOLS = ('rabbit_ai_profiles_list', 'rabbit_ai_profile_rename', 'rabbit_ai_profile_manage',
         'rabbit_memory_profiles_list', 'rabbit_memory_profile_manage',
         'rabbit_memory_connection_check',
         'rabbit_memory_document',
         'rabbit_settings_read', 'rabbit_settings_update', 'rabbit_work_status', 'rabbit_external_work_status',
         'list_native_model_connections', 'refresh_native_model_catalog',
         'refresh_specialist_model_catalog',
         'prepare_native_model_catalog',
         'rabbit_native_chat_names_list', 'rabbit_native_chat_rename',
         'rabbit_native_chat_configuration',
         'rabbit_chat_configuration',
         'rabbit_native_chat_memory_configuration',
         'rabbit_native_history_preview', 'rabbit_native_history_adopt',
         'rabbit_markdown_chats_list', 'rabbit_markdown_chat_publish',
         'rabbit_conversations_list', 'rabbit_native_task')



@dataclass(frozen=True, kw_only=True)
class CodexSpecialistConnection:
    port: int
    mcp_python: str | Path
    codex_command: str | Path = 'codex'
    tool_script: str | Path

    def __post_init__(self):
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('专员连接需要有效的本机服务端口。')
        python = _file(self.mcp_python, executable=True)
        script = _file(self.tool_script, executable=False)
        codex = _codex_command(self.codex_command)
        object.__setattr__(self, 'mcp_python', python)
        object.__setattr__(self, 'tool_script', script)
        object.__setattr__(self, 'codex_command', codex)

    def command(self, workspace_id):
        """Build fresh argv for this exact workspace, without I/O or discovery."""
        _workspace(workspace_id)
        fields = {
            'command': str(self.mcp_python),
            'args': ['-B', str(self.tool_script), '--port', str(self.port), '--workspace-id', workspace_id],
            'cwd': str(self.tool_script.parent),
            'enabled': True,
            'required': True,
            'startup_timeout_sec': 20,
            'tool_timeout_sec': 60,
            'enabled_tools': list(TOOLS),
        }
        override = 'mcp_servers.' + SERVER_NAME + '={' + ','.join(
            key + '=' + json.dumps(value, ensure_ascii=False) for key, value in fields.items()) + '}'
        return [self.codex_command, 'app-server', '--listen', 'stdio://', '-c', override]

    def create(self, store, *, cwd, workspace_id, session_key='specialist/codex'):
        """Construct the existing lazy holder; only its explicit open starts RPC."""
        # The older management-enabled ordinary-chat connection also reuses
        # this factory with chat/codex/... keys. Only the one specialist core
        # gets the application policy; those ordinary chats keep native defaults.
        holder = (partial(SpecialistService, fixed_options={
            'model': SPECIALIST_MODEL, 'effort': SPECIALIST_EFFORT},
            developer_instructions=SPECIALIST_REPLY_INSTRUCTIONS)
            if session_key == 'specialist/codex' else SpecialistService)
        return _create_holder(self.command(workspace_id), store, cwd=cwd,
            session_key=session_key, rpc_type=CodexRpc, holder_type=holder)
