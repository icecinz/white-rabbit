"""Shared presentation and context semantics of generated history records."""

PI_COMPACT_PREFIX = 'The conversation history before this point was compacted into the following summary:\n\n<summary>\n'
PI_COMPACT_SUFFIX = '\n</summary>'
PI_BRANCH_PREFIX = 'The following is a summary of a branch that this conversation came back from:\n\n<summary>\n'
PI_BRANCH_SUFFIX = '</summary>'
PI_COVERAGE_MAX_BYTES = 2 * 1024 * 1024
PI_CONTEXT_MAX_IDS = 20000

SOURCE_PHASES = {
    'compact_summary': ('自动压缩摘要', '自动压缩摘要，并非用户逐字消息'),
    'branch_summary': ('分支摘要', '分支摘要，非用户逐字消息'),
    'custom_context': ('扩展上下文', '扩展上下文，非用户逐字消息'),
    'bash_context': ('终端执行记录', '终端执行记录，非用户逐字消息'),
    'unverified_context': ('未核验资料', '未核验的背景资料，非用户逐字消息'),
}


def source_phase(phase):
    """Return source identity without changing archived role or text."""
    value = SOURCE_PHASES.get(phase)
    return ({'message_kind': phase, 'phase_label': value[0], 'source_kind': value[1]}
            if value else {})


def context_content(text, agent, phase):
    """Pi's public convertToLlm summary wrappers, inside quoted history data.

    The stored source text stays raw. These wrappers never become top-level
    system instructions, and other source formats retain their own text.
    """
    if agent == 'pi' and phase == 'compact_summary':
        return PI_COMPACT_PREFIX + text + PI_COMPACT_SUFFIX
    if agent == 'pi' and phase == 'branch_summary':
        return PI_BRANCH_PREFIX + text + PI_BRANCH_SUFFIX
    return text
