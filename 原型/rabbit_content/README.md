# rabbit-content

小白兔的独立文件核心：保存文本资料、原图和成果，冻结输入版本，维护新增文件及 Markdown 整理的可信回执。不包含 HTTP、界面、Agent、配置或记忆服务。

在本目录安装：`pip install .`；处理图片时用 `pip install '.[images]'`。文本能力仅用 Python 标准库。需要 Python 3.11+；文件锁和安全目录操作面向 macOS / POSIX，保留旧对象的 Markdown 交换更新仅支持 macOS。Windows 暂不支持。

公共入口从 `rabbit_content` 导入：`FileTaskStore`、`FileTaskFiles`、`ImageStore`、`ProjectPublishStore`、`MarkdownChatRegistry`，以及对应错误类。`markdown_chat_document`、`history_markdown_reference` 和 `history_records` 是公开格式合同；不同材料的容量常量保留在各自子模块中。

目录持久身份原语的唯一可编辑源是 `../rabbit_runtime/directory_identity.py`；本包的私有 `_directory_identity.py` 是由仓库的 `工具/sync_directory_identity.py --sync` 生成的逐字节镜像，请勿单独修改。`--check` 和公开源码导出会拒绝不一致的镜像。两个独立安装包各自携带原语，只交换版本化 JSON 锚，不依赖对方包，也不改变已有锚或数据库格式。

开发检查：仓库根目录运行 `python 原型/verify_core_packages.py --source-only` 只需 Python 3.11+ 标准库，默认单测也运行两包隔离源码检查。真实 wheel 安装与 sdist 重建另行显式运行 `python 原型/verify_core_packages.py`；须事先准备本机 `pip`、`setuptools>=68` 及可用的 wheel 构建支持（由当前 setuptools 或 wheel 提供）。检查不联网补装工具，缺失时明确失败；构建、独立安装和虚构存储均在临时目录完成。这些检查不代表新用户或真实 Agent 验收。

`rabbit_content.attachments`是图片引用的唯一校验实现，提供`validate_attachment_ids`、`validate_native_attachments`及原有数量和字段常量；`rabbit_content.file_intent`提供`validate_intent`和`validate_inputs`。它们只核对明确传入的标识、字段、数量与SHA，返回独立结果，不读取文件、数据库或服务。准备包的同名附件入口直接引用这里的函数。

宿主提供数据根、项目根和工作区 ID；`ProjectPublishStore` 可注入时钟。`HistoryMarkdownReference` 的历史读取端只需实现 `indexed_session_metadata(agent, session_id)`，返回 `agent`、`session_id`、`title`。正文渲染接收明确传入的快照，不自行寻找聊天库。`MarkdownChatDirectory.root_scope()`可供宿主在自己的文件夹操作前后核对根目录身份；系统应用的启动仍由宿主负责。导入包不会打开存储；构造存储对象会访问宿主给定的路径。

```python
from uuid import uuid4
from rabbit_content import FileTaskStore

with FileTaskStore('/absolute/path/to/content') as store:
    source = store.add_source('材料.md', '# 一份材料\n', {'kind': 'upload'}, str(uuid4()))
    snapshot = store.input_snapshot(source['id'], expected_sha256=source['sha256'])
    assert snapshot['text'] == '# 一份材料\n'
```

内容与原有数据库格式保持一致。任务的 `chat_id` / `run_id` 由宿主提供；它们是归属标识，不要求宿主安装小白兔。未知发布保留原回执，不自动重发、覆盖原文件或迁移数据。
