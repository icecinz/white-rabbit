# Architecture · 模块结构

[English overview](README.md) · [中文介绍](README.zh-CN.md)

White Rabbit is one local application, with independently reusable cores. The browser talks to the host's same-origin HTTP API; the host owns the chat catalog, transactions, orchestration, and service lifecycle. Splitting the code into packages does not require seven services or seven databases.

```text
Browser / 浏览器
    │ same-origin HTTP
Local application host / 本机宿主
    ├── content and versioned materials
    ├── explicit context preparation
    ├── native agent connections and receipts
    ├── optional long-term memory
    ├── static UI resources
    └── management assistant using the same application interfaces
```

## Seven responsibilities, six extracted cores

| Responsibility / 职责 | Current implementation / 当前实现 |
| --- | --- |
| 1. Content / 内容存储与管理 | [`rabbit_content`](原型/rabbit_content/README.md): files, images, snapshots, publications, and Markdown organization. The chat store and history scheduling remain in the host. |
| 2. Preparation / 内容准备 | [`rabbit_preparation`](原型/rabbit_preparation/README.md): selected messages, material validation, and handoff packaging. The host performs actual reads and memory retrieval. |
| 3. Runtime / Agent 接入与运行 | [`rabbit_runtime`](原型/rabbit_runtime/README.md): process transport, Codex execution, and Pi/DSH/Claude/Hermes foundations. The host still coordinates sends, profiles, and HTTP. |
| 4. Memory / 长期记忆 | [`rabbit_memory`](原型/rabbit_memory/README.md): profiles, source bindings, bounded Hindsight transport, and write jobs. Background scheduling remains in the host. |
| 5. UI / 用户端展示 | [`rabbit_ui`](原型/rabbit_ui/README.md): static assets and page assembly. It needs a compatible host API; it is not a standalone chat backend. |
| 6. Remote I/O / 远程输入输出 | Uses the external IM.codes mobile route. An independent device-management implementation is not provided. |
| 7. Specialist / 小白兔专员 | [`rabbit_specialist`](原型/rabbit_specialist/README.md): connection policy, role preparation, and bounded task coordination. Application management and display remain with their owning modules. |

All six extracted cores have a `pyproject.toml` and require Python 3.11+. Preparation depends on content; specialist depends on runtime and preparation. Optional image and Claude SDK integrations have additional dependencies. The module READMEs currently contain more detailed technical contracts in Chinese.

## Where to look

| Change | Entry points |
| --- | --- |
| Browser interaction | `原型/rabbit_ui/assets/` |
| HTTP and host assembly | `原型/server.py` |
| Chat data, profiles, history | `原型/chat_store.py`, `原型/profiles.py`, `原型/history_sources.py`, `原型/history_view.py` |
| Context and materials | `原型/rabbit_preparation/`, `原型/rabbit_content/`, host material adapters |
| Native execution | `原型/rabbit_runtime/`, host native chat coordinators |
| Explicit startup paths | `原型/native_startup.py`, arguments in `原型/server.py` |
| Application-management tools | `工具/rabbit_management_*` |
| Isolated fictional demo | `demo.py` |

## Data ownership and execution

Native agents retain their own original transcripts and internal state. White Rabbit owns its application records and explicitly selected materials. A text handoff is a versioned input package, not an import of another agent's private execution state.

The host preserves the original request identity and receipt when a send has an unknown outcome. A timeout or disconnected UI is not evidence that a task failed or that its process exited. Approvals remain attached to the original agent and request.

The demo changes the data source and adapters: it creates invented history in a temporary directory and uses rule-based replies, while exercising the same page and application workflows. It does not start native agents.

## 中文说明

七个编号表示职责，六个包表示当前已经实体拆出的核心；模块 1、2、3、4、7 仍有宿主装配，不代表七块已经全部独立。模块可以在同一进程内通过接口组合，不为目录整齐而拆散共同事务。

共同内容和运行回执由小白兔保存，各 Agent 的原生正文及内部状态仍归原生程序管理。跨 Agent 交接保留来源与选定版本；权限、恢复和工具能力按各原生路线分别核验。
