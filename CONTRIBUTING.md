# Contributing · 参与方式

Thank you for trying White Rabbit. Start with the [demo](README.md#try-the-demo), then choose one small change.

## Report a problem

Please include:

- What you wanted to do, the steps, and what happened instead.
- Your OS and Python version; for a native connection, the agent/SDK/CLI versions.
- Whether it happened in the fictional demo or with a real agent.
- A minimal reproduction using invented data when possible.

Do not put credentials, real conversations, native account files, or unredacted private paths in an issue or log. If a send's outcome is unknown, report its state rather than retrying it as a new request.

## Make a change

Describe the affected module, its input/output, and its failure behavior before making a large change. Reuse existing application interfaces and mature upstream components. An adapter that returns text should not be described as a full native integration.

Use a branch and a pull request. Explain the resulting behavior, how you checked it, and what remains unverified. Separate unit tests with fictional data, browser checks, and actual native-agent runs. New native adapters should preserve the original agent's session identity, approval rules, and execution loop.

The public tree includes a selected set of offline regression tests. Use the test command in the repository's `Makefile`; do not assume a passing offline suite verifies credentials, every native tool, or recovery on another machine. Demo and fixture tests must not discover personal histories or contact models.

Write user-facing documentation in English and Chinese when practical. Clear text and small, reviewable changes help more than adding many settings at once.

## 中文

先运行演示，再选一个小问题。反馈时说明操作、预期、实际结果、系统与版本，并区分虚构演示和真实 Agent；尽量用虚构资料复现，不附带真实聊天、凭据或未经处理的私人日志。

改动应说明所属模块、输入输出与失败状态。页面和 AI 管理共用业务规则，原生连接保留 Agent 自己的执行器、会话和审批规则。发送结果未知时查询原回执，不换一个请求编号重复发送。

PR 请分别写明离线测试、浏览器操作、真实 Agent 调用的证据与尚未验证的部分。公开测试的范围以仓库实际提供的文件和 `Makefile` 为准；测试通过不代表所有原生能力已经验收。
