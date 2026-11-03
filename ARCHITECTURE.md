# Architecture · 模块结构

[English overview](README.md) · [中文介绍](README.zh-CN.md)

White Rabbit is an assembly of reusable cores around one local application host. The goal is that a useful part can be installed, understood, replaced, or developed on its own. An application using the content or memory core should not have to adopt the White Rabbit chat server.

The host owns shared transactions, the chat catalog, HTTP, orchestration, and lifecycle. Native agents own their original sessions, tool loops, credentials, and permission decisions. Packaging does not turn the system into seven services or seven databases.

This is the **0.2.0-alpha.1 early source snapshot**. My AI projects existing metadata, the specialist coordinates one history over two native cores, and remote integration is historical. These structures do not replace native authentication, permissions, or environment-specific configuration.

Application ledgers also have their own data scope. The Claude operation ledger can freeze a successful, non-truncated, top-level SDK final answer of at most 128 KiB alongside its original request identity and SHA-256. Exact reads can use that saved body after disconnect/rebuild and identify it as `stored_sdk_result`, without claiming a live owner. Older records without bodies are not reconstructed; legacy read-only V1 ledgers are not migrated by result lookup, and invalid or mismatched evidence is rejected. These ledgers are private runtime data, not public source files. Full original transcripts and internal session state remain owned by the native agent.

## Six active responsibilities, six installable cores

| Responsibility / 职责 | Extracted core | What remains with the host |
| --- | --- | --- |
| 1. Content / 内容存储与管理 | [`rabbit_content`](原型/rabbit_content/README.md): versioned text and image materials, publication receipts, Markdown organization | Chat storage, history indexing, and their shared transaction/scheduling boundaries |
| 2. Preparation / 内容准备 | [`rabbit_preparation`](原型/rabbit_preparation/README.md): context selection, material validation, and handoff packaging | Reading actual sources and retrieving selected memory |
| 3. Runtime / Agent 接入与运行 | [`rabbit_runtime`](原型/rabbit_runtime/README.md): process transport, Codex execution, Pi/DSH/Claude/Hermes foundations, optional HAPI work reader | Upper-level send coordination, profiles, material/memory assembly, HTTP, and manual external-work binding |
| 4. Memory / 长期记忆 | [`rabbit_memory`](原型/rabbit_memory/README.md): profiles, source bindings, bounded transports and write jobs | Driver configuration, background scheduling, admission, and chat transactions |
| 5. UI / 用户端展示 | [`rabbit_ui`](原型/rabbit_ui/README.md): assets and page assembly | Compatible same-origin business APIs, validated agent definitions, and security headers |
| 6. Remote I/O / 远程输入输出（历史） | Historical index only; no runtime package or device-control interface | Local HTTP/access checks remain host responsibilities; archived phone integration is not restored |
| 7. Specialist / 小白兔专员 | [`rabbit_specialist`](原型/rabbit_specialist/README.md): role connection, work-description preparation, bounded task coordination, and application-specific core-history coordination | Native holders, module business interfaces, management service, display, and target availability |

Six packages have independent directories, versions, and `pyproject.toml` files. The documented extracted interfaces can be reused independently, as in the examples below. Application-specific integration still exists: the 2.0 specialist core-history coordinator imports host adapters and is not a standalone generic two-agent service. Parts of responsibilities 1, 2, 3, 4, and 7 still require host assembly. Original numbering is retained; responsibility 6 is historical rather than an active seventh core.

## Overview and specialist data boundaries

`my_ai_overview.py` projects saved profiles, existing model/agent caches, recent-work indexes, and observed execution metadata. Reading it does not perform connection discovery, start an agent, or recall memory content. UI actions route by the same object's stable identity; read, continue, and original-application links remain different capabilities. Unknown or stale facts stay unverified.

The specialist coordinator stores one bounded application history of received user/assistant prose, source identities, per-core native session IDs, and received-text positions. Native tools, approvals, and execution receipts remain in their original holders. Switching supplies missing prose on the next turn, rather than replaying historical tool commands or migrating native internal state. Its persistence and unknown-operation guards are application integration, not proof of all-model or restart acceptance.

Historical pages under `docs/history/remote/` are public documentation, outside the runtime payload and active specialist document mirror. The About letter is intentionally a bundled read-only UI asset: `原型/rabbit_ui/assets/old-letter.md` is its single source, directly exported to the historical page. That asset is also excluded from the active specialist document mirror; no independently maintained letter copy is used.

```mermaid
flowchart LR
    SPECIALIST["rabbit-specialist"] --> RUNTIME["rabbit-runtime"]
    SPECIALIST --> PREP["rabbit-preparation"]
    PREP --> CONTENT["rabbit-content"]
    HOST["Your application host"] --> SPECIALIST
    HOST --> UI["rabbit-ui"]
    HOST --> MEMORY["rabbit-memory"]
    HOST --> CONTENT
    HOST --> RUNTIME
```

*Arrows between packages are declared Python dependencies. The host-to-core arrows are an example assembly, not mandatory dependencies between every part.*

## Reuse one core

Use **Python 3.11+**. From this checkout, install a complete package directory, including its metadata and bundled notices:

```sh
python3 -m pip install ./原型/rabbit_content
```

A small application can use the content store without starting White Rabbit or an agent:

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from rabbit_content import FileTaskStore

with TemporaryDirectory(prefix="rabbit-content-example-") as directory:
    with FileTaskStore(Path(directory).resolve()) as store:
        source = store.add_source(
            "brief.md", "# Project brief\n\nAn invented example.\n",
            {"kind": "upload"}, str(uuid4()),
        )
        snapshot = store.input_snapshot(source["id"], expected_sha256=source["sha256"])
        print(snapshot["text"])
```

Text capabilities use the standard library. Optional image operations need the package's `images` extra. File-locking/safe-directory operations target macOS/POSIX; the preserve-original Markdown exchange update is macOS-specific.

The UI core can also provide a resource to another compatible host:

```sh
python3 -m pip install ./原型/rabbit_ui
```

```python
from rabbit_ui import read_resource

resource = read_resource("/style.css", agent_bootstrap="")
print(resource.content_type, len(resource.data))
```

Serving these assets is not a standalone chat backend. The embedding host supplies the compatible HTTP APIs and corresponding security headers.

## Install cores with local dependencies

`rabbit-preparation` depends on `rabbit-content`; `rabbit-specialist` depends on runtime and preparation. Pass all required **local directories** to pip together, rather than asking PyPI for unpublished White Rabbit packages:

```sh
python3 -m pip install \
  ./原型/rabbit_content \
  ./原型/rabbit_preparation \
  ./原型/rabbit_runtime \
  ./原型/rabbit_memory \
  ./原型/rabbit_ui \
  ./原型/rabbit_specialist
```

No native agent is installed or started by this example. The runtime foundation is standard-library based; the Claude worker separately needs its supported official SDK environment and a matching installed runtime distribution. Optional providers, image libraries, build tools, and services have their own dependencies.

## Keep the boundaries real

1. **One owner for each rule and record.** UI actions and specialist tools call the same versioned business interfaces. The specialist has no parallel settings database.
2. **Explicit inputs.** Embedding applications provide roots, source readers, identities, snapshots, and capabilities. A core should not search for private host data just because it was imported.
3. **Execution stays native.** Transport and adapters connect mature agents; the application does not copy their execution loop or treat a text response as full native integration.
4. **Evidence keeps its scope.** History reading, saved-session recovery, and attachment to a live external executor are distinct. HAPI summaries are Hub reports, not authoritative observations of all native work.
5. **Unknown stays unknown.** A timeout, cached status, or recent answer does not establish which request completed. Preserve original IDs and receipts; do not resend an uncertain task.

## Where to work

| Change | Entry points |
| --- | --- |
| UI and color semantics | `原型/rabbit_ui/assets/`, [design guide](docs/design.md) |
| My AI overview | `原型/my_ai_overview.py`, `原型/rabbit_ui/assets/my_ai.js`, `my_ai_page.js` |
| HTTP and host assembly | `原型/server.py` |
| Chat/profile/history records | `原型/chat_store.py`, `原型/profiles.py`, `原型/history_sources.py`, `原型/history_view.py` |
| Materials and input preparation | `原型/rabbit_content/`, `原型/rabbit_preparation/` and explicit host adapters |
| Native execution and external summaries | `原型/rabbit_runtime/`, host native coordinators, `原型/external_work.py` |
| Fixed connection paths | `原型/native_startup.py`, arguments in `原型/server.py` |
| Specialist and management interfaces | `原型/rabbit_specialist/`, including host-dependent `core_conversation.py`; `工具/rabbit_management_*`, [specialist guide](docs/specialist.md) |
| Fictional demo | `demo.py` |

## 中文：独立不是只分文件夹

每个核心都有自己的目录、版本和安装信息，公开接口会写清需要传入什么。需要哪块，就从本地源码安装哪块，不必运行整个小白兔。上面的内容快照例子不会启动聊天服务器或 Agent。

准备包依赖内容包，专员依赖运行包和准备包。一起安装时，在同一次 pip 命令里传入这些本地目录，避免去 PyPI 找本项目尚未发布的包。许可证、资源和协议文件都要随完整模块一起保留，不能只拷一个 Python 文件。

2.0 有六项在用职责和六个已拆核心，沿用原编号，6 号远程输入输出只保留历史。独立复用指已经抽出的明确接口，不表示目录里的每个子模块都能脱离主程序。新增的专员核心历史协调器仍会导入主程序的适配代码。严格模块化是持续要求：数据归谁负责要清楚，规则只保留一份，接口写清输入、输出和失败状态，换一块时尽量少改其他块。不要为拆包增加长期后台服务，也不要拆散需要一起完成的事务。模块技术 README 目前主要使用中文。

“我的 AI”只汇集保存的配置和目录缓存，以及最近工作索引和能观察到的状态，不启动 Agent，也不读记忆正文。专员保存收到的应用正文、来源身份，以及两个核心各自的原生编号／正文位置。工具、审批和回执仍归原持有者，不重新执行历史工具命令。这些结构不能代替原生认证、权限或具体环境中的配置。

`docs/history/remote/` 不进入运行程序，也不进入专员当前使用的文档副本。旧信作为只读界面资源随包提供，唯一来源是 `原型/rabbit_ui/assets/old-letter.md`，公开历史直接从它导出。这篇正文同样不进入专员当前使用的文档副本。

运行账本保存哪些数据，也有明确范围。Claude 可以在本机按原请求身份和 SHA-256 保存成功、未截断的顶层 SDK 最终回答，最多 128 KiB。断开／重建后，准确读回的内容标为存储结果，不表示当前仍有活执行者。旧回执没有正文就不补造，旧 V1 只读账本不会因为查询而迁移，数据损坏或身份不符时拒绝读取。账本属于私人运行数据，不进入公开源码，完整原生历史和内部状态仍归原 Agent。
