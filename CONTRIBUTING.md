# Contributing · 参与方式

For everyday use, follow the [real installation checklist](docs/install-agent.md). For interface development without an agent, try the [fictional demo](README.md#interface-preview-only-fictional-demo), then choose a small problem. Improvements to a reusable core, an accurate integration boundary, or a clear explanation are welcome alongside product features.

## Report a problem

Include the intended operation, reproduction steps, expected/actual result, OS/Python version, and relevant native agent/SDK/CLI versions. State whether the evidence comes from fictional fixtures, browser interaction, a real agent, or an already running external application.

Use invented data where possible. Omit credentials, real conversations, account files, and unredacted private paths. If sending may already have happened, report the original request's state; a second request is not a way to check the first.

## Work within a module

Before a larger change, name its module, owned data, inputs, outputs, and failure states. The [architecture guide](ARCHITECTURE.md) maps six active responsibilities to six installable cores and remaining host assembly; original module 6 is historical.

- Keep a core independently installable; provide explicit paths/readers/capabilities instead of importing the host.
- Use one implementation for shared rules. The specialist and graphical UI call the same business API and version checks.
- Preserve the original agent's executor, session identity, tool behavior, and approvals. Reading history or opening a new CLI is not attachment to live desktop work.
- Add a supported interface before marking an entry AI-operable or moving its control into the specialist's settings catalog.
- Apply [human/AI color semantics](docs/design.md) to the whole entry. Read-only purple content does not imply writable access; confirmations remain human actions.

## Submit a change

Use a branch and pull request. Explain the resulting behavior, verification, and remaining limits. Prefer a small reviewable change over many nominal options. Record model/SDK version assumptions in the adapter rather than presenting them as universal compatibility.

Use the repository's `Makefile` for the supplied offline checks. Keep tests isolated from personal histories and model calls; do not run a live verification script merely because its name starts with `test` or `verify`. Run relevant browser checks when changing interaction. A passing offline suite does not establish real-account permissions, every tool, or another machine's recovery behavior.

Development checks require **Python 3.11+ and Node**. `make test` runs the supplied Python checks and the narrow `make test-ui` target (`test_blueprint_home_ui.cjs`); some specialist-core Python tests also invoke the existing Node codec. These use Node's standard library, with no npm dependency install. The UI uses system fonts and bundled images; those do not add an installation dependency. This test selection does not include every historical browser script or establish fresh-user installation acceptance.

Keep English and Chinese user documentation aligned. For integration evidence, distinguish a native target round trip from the complete specialist-to-target-to-result chain. Preserve previous failed evidence when reporting a later correction; do not substitute the most recent answer for the answer belonging to an exact request.

## 中文

先试演示，再挑一个小问题。反馈请写清操作步骤、预期结果、实际结果、版本，以及这些结果是怎样核对的。尽量用虚构资料复现，不上传真实聊天或凭据。

独立核心要能单独安装，写清需要什么输入。界面和专员使用同一套业务接口与版本检查，不另建设置系统。原生执行器、身份、工具和审批仍归原 Agent。能读历史、能恢复会话和能接管正在运行的工作，需要分别验证。

增加“交给 AI 的设置”前，先做好可靠接口。紫色表示已经接通的程序能力，黑色／中性色保留人通过界面操作或确认的步骤。颜色不增加权限。

PR 请说明改了什么、怎样核对，还有什么限制。离线测试、浏览器操作、真实 Agent 和完整交办流程分别说明。结果不明的请求只核对原编号，不重发。中文和英文说明尽量同步，欢迎小而清楚、能单独使用的修改。

开发检查需要 **Python 3.11+ 和 Node**。`make test` 先运行随包 Python 检查，再执行 `make test-ui`，后者只跑 `test_blueprint_home_ui.cjs`。部分专员核心 Python 检查还会调用已有 Node codec。只用 Node 标准库，不用安装 npm 依赖。系统字体和随包图片也不会增加安装依赖。这些检查不包含全部历史浏览器脚本，也不能说明全新用户安装已经验收。
