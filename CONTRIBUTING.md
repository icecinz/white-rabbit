# Contributing · 参与方式

For everyday use, follow the [real installation checklist](docs/install-agent.md). For interface development without an agent, try the [fictional demo](README.md#interface-preview-only-fictional-demo), then choose a small problem. Improvements to a reusable core, an accurate integration boundary, or a clear explanation are welcome alongside product features.

## Report a problem

Include the intended operation, reproduction steps, expected/actual result, OS/Python version, and relevant native agent/SDK/CLI versions. State whether the evidence comes from fictional fixtures, browser interaction, a real agent, or an already running external application.

Use invented data where possible. Omit credentials, real conversations, account files, and unredacted private paths. If sending may already have happened, report the original request's state; a second request is not a way to check the first.

## Work within a module

Before a larger change, name its module, owned data, inputs, outputs, and failure states. The [architecture guide](ARCHITECTURE.md) maps seven responsibilities to six installable cores and remaining host assembly.

- Keep a core independently installable; provide explicit paths/readers/capabilities instead of importing the host.
- Use one implementation for shared rules. The specialist and graphical UI call the same business API and version checks.
- Preserve the original agent's executor, session identity, tool behavior, and approvals. Reading history or opening a new CLI is not attachment to live desktop work.
- Add a supported interface before marking an entry AI-operable or moving its control into the specialist's settings catalog.
- Apply [human/AI color semantics](docs/design.md) to the whole entry. Read-only purple content does not imply writable access; confirmations remain human actions.

## Submit a change

Use a branch and pull request. Explain the resulting behavior, verification, and remaining limits. Prefer a small reviewable change over many nominal options. Record model/SDK version assumptions in the adapter rather than presenting them as universal compatibility.

Use the repository's `Makefile` for the supplied offline checks. Keep tests isolated from personal histories and model calls; do not run a live verification script merely because its name starts with `test` or `verify`. Run relevant browser checks when changing interaction. A passing offline suite does not establish real-account permissions, every tool, or another machine's recovery behavior.

Keep English and Chinese user documentation aligned. For integration evidence, distinguish a native target round trip from the complete specialist-to-target-to-result chain. Preserve previous failed evidence when reporting a later correction; do not substitute the most recent answer for the answer belonging to an exact request.

## 中文

先试演示，再挑一个小问题。反馈写清步骤、预期、实际、版本及证据范围，用虚构资料复现，不上传真实聊天或凭据。

独立核心需保持可安装和明确输入；界面、专员共用业务接口与版本检查，不建第二套设置系统。原生执行器、身份、工具和审批继续归原 Agent；历史可读、会话恢复和存活工作接管分开验证。

新增“交给 AI 的设置”前先实现可靠接口。紫色表达已接通的程序能力，黑色／中性色保留当前人操作或确认；颜色不扩大权限。

PR 说明变化、核验和剩余边界。离线测试、浏览器操作、真实 Agent、完整交办链分别报告；未知请求只核对原编号，不重发。中英文说明尽量同步，欢迎小而清楚、能单独复用的修改。
