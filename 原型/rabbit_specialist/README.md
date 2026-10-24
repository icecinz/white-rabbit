# rabbit-specialist

模块7的独立核心：专员连接策略、角色说明准备、报告式任务账本与有限的提交协调。原生执行、审批和会话恢复复用`rabbit-runtime`，不再实现一套Agent循环。

本目录可直接安装，需要Python 3.11+及`rabbit-runtime`、`rabbit-preparation`。准备包复用`rabbit-content`的材料ID规则。当前面向macOS／POSIX，原生Codex、模型账号和应用管理工具由宿主另行提供。

## 接口与装配

| 接口 | 输入与责任 |
| --- | --- |
| `CodexSpecialistConnection` | 明确传入本机端口、MCP Python与`tool_script`，生成同一工作区的惰性连接；固定专员会话请求Luna/max，原生工具与审批不变 |
| `prepare_workspace` | 明确的工作目录与文档读取函数；首次准备角色说明，保留已有本地文件，已绑定会话的说明缺失时拒绝重建 |
| `InterfaceTasks` | 明确的数据库路径与工作区；保存完整任务、版本和报告状态，提供可重读的操作回执 |
| `InterfaceTaskCoordinator` | 已绑定的原生专员、任务账本、纯文本渲染函数及宿主准入scope；只对新认领的任务尝试一次提交 |

```python
from pathlib import Path
from rabbit_runtime import NativeSessionStore
from rabbit_specialist import CodexSpecialistConnection, prepare_workspace

data = Path("/absolute/local/data")
work = data / "specialist-workspace"  # 宿主事先创建

def read_role():
    return {
        "content": "你是这个应用的专员，使用应用明确提供的管理接口。",
        "path": "/absolute/project/角色.md",
        "source_root": "/absolute/project",
        "snapshot": False,
        "updated_at": None,
    }

store = NativeSessionStore(data / "specialist-native.sqlite")
binding = store.read_binding("specialist/codex")
prepare_workspace(work, read_role,
    bound_thread_id=binding.get("thread_id") if binding else None)
connection = CodexSpecialistConnection(
    port=17860,
    mcp_python="/absolute/venv/bin/python",
    tool_script="/absolute/app/rabbit_management_mcp.py",
)
holder = connection.create(store, cwd=work, workspace_id="a" * 64)
try:
    holder.open()  # 到这里才启动原生Codex及显式管理扩展
    holder.send("unique-client-id", [{"type": "text", "text": "查看当前设置"}])
finally:
    holder.close()
    store.close()
```

`read_role`返回正文和来源元数据，不会自动读取其中链接。宿主负责读取权限、角色文档和应用管理服务；工具脚本必须实现连接公开列出的管理工具。本包不猜宿主目录，不携带用户资料或凭据。

## 任务状态的含义

任务账本记录的是报告状态。`sent`不是外部应用发送成功的独立证明，`completed`也不证明后台原生工具已退出。关闭未知任务不会消除未知结果，更不会授予桌面操作权限。

协调器只复用已有且已绑定的专员；不自动连接，不控制鼠标，不另开读取线程，不重发未知提交，也不代答审批。当前协调器只接受文字；材料引用可以保存与校验，但不假装已经送进外部应用。

小白兔宿主目前使用角色连接、说明准备及任务维护观察。任务协调器尚未接入生产的桌面往返流程。内嵌对话显示、长期记忆适配、管理业务接口及设备准入仍由各所属模块负责；独立打包不等于这些功能全部上线。
