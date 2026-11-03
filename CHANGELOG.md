# Public snapshots · 公开更新

## 0.2.0-alpha.1 — 2026-11-03

Early Blueprint 2.0 source snapshot. Configure and verify native connections in your own environment; supported application interfaces do not replace agent authentication or permissions.

- Added **My AI**, separate from New conversation: agent/model/memory summaries, recent work, exact-object management navigation, and a first-agent action when nothing is connected. Ordinary opening/polling uses existing facts and caches without starting an agent, probing a model, or reading memory content.
- Added one specialist application history with separate Codex/DSH native sessions and received-text positions. Settings selects the core; successful switching adds one boundary, carries missing prose on the next turn, and does not replay historical tools.
- Both specialist cores can use the supported management MCP for exact-target operations such as profile listing, versioned renaming, and rereading. Tool/model availability and native approvals remain core-specific.
- Kept New conversation, browser-local drafts, and same-conversation refresh/continuation separate from the resource overview; native session identity remains explicit.
- Removed device/wake-up/phone-preview entries and IM.codes-specific compatibility from source, preserving local service/access checks, local records, browser drafts, and separately installed user software. Historical pages stay outside runtime and active specialist document mirroring.
- Added the About letter as one bundled source, directly exported to the public history page. Public screenshots show the running application with invented resources and work, rather than design mockups or private conversations.
- Included narrow offline My AI UI checks and specialist-core protocol/failure checks. Developer checks require Python 3.11+ and Node; they do not establish fresh-user installation or real native failure handling. DSH specialist shared-memory selection remains unconnected, and the manual report's tool fact still observes the Codex-side holder.

### 中文

蓝图 2.0 的早期源码快照。请在自己的环境配置并验证原生连接；应用接口不替代 Agent 认证与权限。

- 增加与“新对话”分开的**我的 AI**：Agent／模型／记忆摘要、最近工作、同一对象的管理导航，零接入时可接入第一个 Agent。普通打开和轮询只读已有事实与缓存，不启动 Agent、不探测模型、不读记忆正文。
- 专员一段应用历史背后分别保存 Codex／DSH 原生会话与正文位置。设置中选择核心，成功才写一处分界，下一轮带缺失正文，不重放历史工具调用。
- 两种专员核心可通过受支持管理 MCP 操作准确目标，例如列配置、按版本改名与读回；工具／模型可用性与原生审批仍按核心区分。
- 新对话、浏览器本机草稿及同会话刷新续聊与资源总览分开，保留明确原生会话身份。
- 源码撤设备／唤醒／手机预览入口及 IM.codes 专用兼容，保留本机服务与访问校验、local 记录、浏览器草稿和用户另装的软件。历史页不进入运行负载或专员当前文档镜像。
- 关于页旧信保留唯一随包来源，公开历史直接导出同一正文。公开图来自实际运行界面，资源与工作为虚构示例，不纳入设计稿或私人对话。
- 增加有限的首页离线 UI 和专员核心协议／故障检查；开发测试需 Python 3.11+ 与 Node，不代表新用户安装或真实原生失败处理已验收。DSH 专员共享记忆未接通，手动报告工具事实仍观察 Codex 一侧。

### Carried-forward baseline / 保留的基础工作（2026-11-01–2026-11-02）

The following baseline features are retained in this snapshot. The fixed alpha.4 commit below records that older public version's exact scope.

以下基础功能保留在本快照中；下方固定 alpha.4 提交记录旧公开版本的准确范围。

- Added the warm-paper, ink-purple, and moss-green visual layer with Song/serif headings, bundled static texture, and a separate dark palette; access-color semantics stay unchanged.
- Refreshed the fictional-demo screenshots and restored its local simulated new-chat path; the preview needs no real agent or credentials.
- Unified the basic Codex/Hermes/Pi/Claude Code/DSH composer from home draft through native conversation and refresh continuation, retaining each agent's sending, approval, and recovery protocols. Pi retains a selected upload when typing continues, alongside existing draft text and files.
- Added read-only access to exactly bound saved text when a native conversation is unavailable. Search opens the same entry, with paging, refresh, reading-position recovery, and return without losing the original draft. Hermes source segments stay separate; a changed source requires an explicit reread. Reading does not start, restore, or take over an agent.
- Disable direct Hermes reconnection after an event-stream failure. Keep already read text and drafts, and explain the explicit sequence: check the original receipt, confirm the request has ended, use the existing guarded disconnect, then restore the original session. Bound closed sessions show Restore original session even with cached text. This recovery path does not guarantee uninterrupted native connections across sleep/wake.
- Added bidirectional Codex ↔ Hermes continuation using selected text, complete registered files, original images, and fixed snapshots. Preparation does not execute; Send creates the target once, with source/return navigation and original-ID recovery for uncertain results.
- Registered Markdown files can be added to the current draft while retaining its question and selected materials. Repeated selection after reload reuses the same import; a separate explicit handoff remains available, with version checks and original-receipt recovery for uncertain results.
- Added Codex draft/conversation model and reasoning fine-tuning. Specialist draft suggestions complete only after the originating page's versioned save and application receipt, preserving draft material and avoiding global-default changes.
- Save eligible Pi/Hermes complete task results after their first exact successful read, allowing later original-ID retrieval without treating saved evidence as a live connection.
- Keep independently verified work visible when another connection of the same Agent is unknown; preserve the overall unknown status.
- Check the Hermes source entry and the DeepSeek Harness CLI/package pairing before installation; path checks do not verify versions, credentials, or model replies.
- Verify the running local service belongs to this installation before opening it. A different or unconfirmed workspace is left untouched.
- Document how to retain a custom Hermes or DSH data directory in the login service.
- Clarify persistent Python selection and login-service environment limits; provide Pi and Claude Code connection examples.

### 中文

- 增加暖纸、墨紫、苔绿的视觉层，采用宋体标题、随包静态纸纹与独立深色配色，原有操作颜色含义不变。
- 更新虚构演示截图，并修复新首页的本地模拟新建路径；预览不需要真实 Agent 或凭据。
- Codex／Hermes／Pi／Claude Code／DSH 从首页草稿到原生对话、刷新续聊统一基本输入路径，保留各自的发送、审批和恢复协议；Pi 上传期间继续打字仍保留选入文件、原稿和已有材料。
- 原生对话暂不可用时，可以只读准确绑定的已存文字；搜索打开同一条目，支持分页、刷新、阅读位置恢复和保留原草稿返回。Hermes 各来源分开阅读，来源变化需明确重新读取；阅读不会启动、恢复或接管 Agent。
- Hermes 事件流失败后禁止直接重连，保留已读取文字和草稿，说明先核对原回执、确认请求结束，再沿现有护栏断开旧连接并明确恢复原会话；已绑定的断开会话即便有缓存文字也标“恢复原会话”。此恢复路径不保证睡眠／唤醒后原生连接始终连续。
- Codex ↔ Hermes 双向接续使用选定文字、完整登记文件、原图和固定快照；准备不执行，按发送才创建目标，保留来源／返回及未知结果的原编号核对。
- 整理后的 Markdown 文件可加入当前草稿，保留原问题与已选材料；刷新后重复选择复用同一导入。另选目标的明确接力仍保留，版本核验与未知结果的原回执核对照常生效。
- Codex 草稿／本对话支持模型与强度细调；专员草稿建议需原页面按版本保存并返回应用回执才完成，保留草稿材料，不改全局默认。
- Pi／Hermes 的符合条件完整结果在首次按原编号成功读回后保存，之后可沿原编号取回，不把存储证据当成活连接。
- 同一 Agent 的一个连接未知时，其他已核实的工作继续显示；整体状态仍标明未知。
- 安装前核对 Hermes 原生入口和 DeepSeek Harness 的 CLI／包目录配对；路径检查不代表版本、认证或模型调用已通过。
- 打开前确认本机服务属于当前安装；其他工作区或状态未知时保留原状。
- 说明登录启动时怎样沿用 Hermes 或 DSH 的自定义资料目录。
- 明确长期使用的 Python 选择和登录服务的环境变量限制，补充 Pi 与 Claude Code 的连接示例。

## 0.1.0-alpha.4 — 2026-11-01

[Published predecessor](https://github.com/icecinz/white-rabbit/commit/9e88305e17c4eca95550d86a6cc5fc4298b20296): warm-paper UI, then-supported unified composing, explicit Codex/Hermes continuation, and local configuration improvements. The fixed commit records the exact published scope.

[已公开的前版](https://github.com/icecinz/white-rabbit/commit/9e88305e17c4eca95550d86a6cc5fc4298b20296)：软纸界面、当时已支持的统一输入、Codex／Hermes 明确选材接续及局部配置改进；准确公开范围以固定提交为准。

## 0.1.0-alpha.3 — 2026-10-28

- Put real installation before the fictional demo; added a short root `AGENTS.md` and bilingual installation-agent checklists.
- Included the existing macOS installer, a read-only preflight, finite native connection examples, login startup, and a persistent open entry. Credentials remain in the selected agent.
- Separate source/path readiness, enabled native routes, authentication, and a verified real conversation. A demo or reachable page cannot satisfy installation acceptance.
- Preserve existing connection configuration on updates; explicit replacements require the complete intended connection set. The specialist and memory services remain optional.
- Added manual status reports: program facts → one tool-free analysis → locally saved facts, judgments, and explanation, with source/time metadata and changed-evidence handling. Refresh reuses the report rather than resubmitting analysis or changing settings.
- Keep the Codex management specialist separate from the existing Claude-configured text analysis route; a missing route or unknown outcome remains incomplete without automatic retry.
- Clarified configuration creation as starting a new profile from an existing one, removed the default-for-new-chats page action, and included immediate button help. Offline verification does not establish another user's login, model access, or macOS permissions.

### 中文

- 真实安装前置，增加根目录 `AGENTS.md` 与中英文安装 Agent 清单。
- 随包提供现有 macOS 安装器、只读预检、有限连接配置示例、登录启动与长期打开入口；凭据保留在所选 Agent 中。
- 区分源码／路径准备、原生入口启用、认证和真实对话通过；演示或网页可打开不能算安装完成。
- 更新默认保留旧连接，显式替换需包含完整连接集合；专员和记忆服务可稍后配置。
- 增加手动状态报告：程序事实 → 一次无工具分析 → 在本机保存事实、判断和说明，保留来源／时间及依据变化状态；刷新复用报告，不重新分析或改设置。
- Codex 管理专员与已有 Claude 配置文字分析通路分别说明；缺少连接或结果未知时保持未完成，不自动重试。
- 配置创建改为“从此配置开始新建配置”，去掉页面的新对话默认动作，并提供即时按钮说明；离线核对不等于其他用户的登录、模型权限或 macOS 授权已验证。

## 0.1.0-alpha.2 — 2026-10-27

- Synced the current native-runtime, management, material, memory, and UI source allowlists.
- Added current-work metadata and optional, manually connected HAPI Hub discovery. Opening a discovered session returns to HAPI; sending, approvals, and stopping remain there.
- Included independent Hermes document-memory support and the optional Mem0 read-only bridge, with an example configuration and separate dependency instructions. Real Mem0 credentials and environment setup are still required.
- Updated specialist management, scoped conversation configuration, organization, and request-bound task/result interfaces. Support varies by agent and connection; this is not desktop takeover or unrestricted device control.
- Added bounded local storage of eligible successful Claude SDK final results, with original-request identity and digest checks. Missing, invalid, or older body-free receipts are not fabricated into completed answers.
- Expanded English/Chinese project, architecture, specialist, and design documentation, including local module reuse examples and purple/neutral interface semantics.
- Refreshed fictional-demo screenshots and added selected offline checks for work observations, a temporary HAPI fixture, and synthetic memory adapters.

### 中文

- 同步当前原生运行、管理、材料、记忆和界面的源码白名单。
- 增加当前工作元数据与可选、手动连接的 HAPI Hub 工作发现。发现后打开 HAPI 原页面，发送、审批和停止仍由 HAPI 处理。
- 包含独立 Hermes 文档记忆、可选 Mem0 只读桥、配置示例及依赖说明；真实 Mem0 凭据与运行环境仍需自行配置。
- 更新专员管理、限定到具体对话的配置、整理与原请求任务／结果接口。能力依 Agent 和连接而异，不代表桌面接管或所有设备控制。
- 增加符合条件的 Claude SDK 成功最终结果的有界本机保存，按原请求身份和摘要核对；缺失、无效或旧版无正文回执不会补造成已完成回答。
- 扩充中英文项目、架构、专员与设计说明，加入本地模块复用示例和紫色／中性色的操作含义。
- 更新虚构演示截图，并加入工作状态、临时 HAPI Hub 和虚构记忆的离线测试。

## 0.1.0-alpha.1 — 2026-10-25

First public source snapshot: bilingual onboarding, six reusable Python cores,
MIT and third-party notices, a temporary fictional-data demo, and offline CI.

首次公开源码快照：中英文入门说明、六个可复用 Python 核心、MIT 与第三方声明、
临时虚构数据演示和离线自动检查。
