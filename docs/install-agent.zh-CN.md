# 给安装 Agent 的清单

[English](install-agent.md) · [简体中文](install-agent.zh-CN.md) · [首页](../README.zh-CN.md)

用户说“帮我装好并配置好小白兔”时，目标是：**以后能方便打开，并能与至少一个真实 Agent 连续聊天。** 不以启动 `demo.py` 或打开页面作为完成。

## 1. 先看现有环境

- 确认系统和 Python 版本。当前需 Python 3.11+；持久本机安装先按 macOS 路线，Windows 暂不支持。
- 检查是否已经装过小白兔、17860 端口是否被使用；已有安装先核对，不能覆盖另一份运行数据或中断任务。
- 找到用户平时使用的一个 Agent 及准确的 CLI、运行环境和资料目录。先复用它，不要求装齐 Codex、Hermes、Pi、Claude Code、DSH。
- 用原 Agent 的登录状态入口核对认证，不打印密钥或整份凭据文件。不清楚使用哪个环境、账号或资料目录时，只问缺少的那一项。

选择一个稳定的 Python 3.11 或更新版本的绝对路径；不要选会删除的临时虚拟环境，登录服务以后仍要用它。可依次检查 `command -v python3.13`、`command -v python3.12`、`command -v python3.11` 和 `command -v python3` 返回的路径，并对候选运行 `"/绝对路径/python" --version`。系统自带的 `python3` 可能低于 3.11；没有合适版本时需先安装。记录该路径，并在本次终端中设置（替换示例路径）：

```sh
RABBIT_PYTHON="/absolute/path/to/python3.11"
"$RABBIT_PYTHON" --version
```

图片功能还需要服务 Python 的 Pillow。预检会给出 `service_python.path`，但不会执行它检查版本或图片依赖；先以该路径运行 `--version`，再运行 `-c "from PIL import Image; print(Image.__version__)"` 核对。缺少时，在用户选定的稳定环境中安装 Pillow，再核对；只安装文字通路时可如实说明图片暂不可用。

## 2. 选一个真实通路

按[原生连接](native-agents.zh-CN.md)核对这一个 Agent 所需路径及版本。保留它正在生效的模型、提供商、工具和权限；例如 Claude Code 已由 CC Switch 管理时，不把它改回另一套默认配置。

当前持久连接 JSON 只有 Hermes／DSH 支持 `home`。登录启动服务不会自动保存 `CODEX_HOME`／`CLAUDE_CONFIG_DIR`；在终端设置这些变量，不代表登录服务沿用同一资料目录。无法确认原账号和路由环境时，暂停该通路，或使用另一个已经核实可用的现有 Agent；不能默认回退、迁移凭据或复制密钥。

缺登录、API 权限或依赖时，明确列出所缺内容，让用户在本地完成必要认证；不要要求把 Key 发到聊天。未经核实的模型或连接标为待配置。

## 3. 安装并留下打开方式

在仓库根目录先运行只读预检：

```sh
"$RABBIT_PYTHON" -B 原型/install_local.py check --source-dir "$PWD/原型"
```

按[原生连接的 JSON 示例](native-agents.zh-CN.md#持久安装)把查实的绝对路径写进仓库外的 `$HOME/.white-rabbit-connections.json`；由你配置，不是程序自动扫描。JSON 不放 Key。然后运行：

```sh
"$RABBIT_PYTHON" -B 原型/install_local.py check --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
"$RABBIT_PYTHON" -B 原型/install_local.py install --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
```

`check` 核对基础环境、路径及 17860 端口：`service` 为 `free`、`current_installation`、`other` 或 `unknown`。其他服务占用或状态未知时先核对；这只是一次只读观察，安装器仍会检查活动任务和维护状态。它不证明登录、SDK 版本或真实模型回复。首次安装需至少一个普通 Agent；只有专员或空连接不能算配置完成。

预检的 `visible_commands` 可帮助定位当前 PATH 中的程序，再逐一核对实际安装位置与原 Agent 的状态；它不是完整扫描，也不代表程序已经可用。不要读取或打印凭据。

安装器保留私有程序副本与数据在 `$HOME/.imcodes/one-ai`，并注册当前用户的登录启动服务；此目录名不表示需要安装 IM.codes。以后双击其中的 `打开小白兔.command` 即可，无需保留终端变量。也可以用安装时选定的 Python 绝对路径运行（替换示例路径）：

```sh
"/absolute/path/to/python3.11" -B "$HOME/.imcodes/one-ai/manage.py" open
```

已有安装更新时，省略 `--native-config` 会保留连接配置。明确传入的 `connections` 是完整替换，不是增量添加；增加 Agent 前先合并原有条目，不能只写新 Agent。

涉及数据格式升级时，在没有活动任务时为要迁移的账本做 SQLite 一致备份，并与旧程序版本一起保留；不要在服务运行时只复制 `.sqlite` 文件而漏掉 WAL。安装器的恢复副本只含程序、启动配置和控制脚本，不含数据数据库。Hermes／Pi 的结果保留更新会将各自的 `hermes-native.sqlite`／`pi-native.sqlite` 从 v1 升至 v2，旧程序不能直接读取 v2。`rollback` 只恢复程序；若需恢复迁移前账本，先保留当前数据并说明会失去升级后的新记录，由用户决定，不自动覆盖。

保留源码目录，供查看项目文档及以后更新。更新默认沿用已保存的服务 Python，不会因调用安装器的环境不同而更换。如果旧路径不可用，先选定新的稳定 Python 3.11+，分别在 `check` 和 `install` 中加 `--python "$RABBIT_PYTHON"` 明确更换；同时核对它的 Pillow。不要假设会自动迁移解释器。

把小白兔数据与 Agent 的原始资料分开保存，使用绝对路径记录所选环境。安装后确认页面、启动文件及数据目录实际存在；退出当前终端后仍能按上述方式打开。

第一次只配置所选普通 Agent。记忆、明确配置的小白兔专员核心和其他 Agent 可在真实聊天成功后继续，不把可选功能当安装前提。没有 Codex 的用户可以先使用自己的 Hermes 或 DSH；普通 DSH 接入不自动配置其专员角色。是否配置专员、哪种核心已实际核验，都如实说明。手机预览已转为历史，不列为安装选项。

## 4. 从真实界面验收

全新配置优先采用本次已启用、能核对默认参数的原生入口；没有合适默认时再明确选择。已有用户保存的默认不自动改动，也不为了完成安装强行接入 Codex。

1. 打开小白兔正式入口，选择刚配置的真实原生 Agent，建立一段新的验收对话。
2. 发送一句包含随机短代号的普通请求，例如“这次验收代号是纸船7318，请简短确认；不用调用工具”。确认收到原 Agent 的真实回答，并记录其实际报告的模型信息；未报告的型号或强度不猜。
3. 刷新页面，打开同一段对话，确认问答仍在；再问“刚才的验收代号是什么？”，核对同一原生会话的连续回答。
4. 用留下的打开方式重新进入，确认无需重新拼接启动命令。若尚未实际重启服务，只报告页面刷新验证，不声称已验证重启恢复。

重开后的 Hermes 原会话若显示“恢复原会话”，先说明原生恢复也可能继续未完成任务，由用户明确恢复，再核对已有内容；不通过发送重复问题来让历史出现。

请求结果未知时，核对原编号，不用重发来判断是否成功。原生登录、授权与审批由用户决定。浏览器不可用或缺少必要权限时，将这项验收明确留待用户完成。

## 5. 简短交接

只交代这五项：**怎样打开、数据在哪里、接入了哪个 Agent／模型、真实验收结果、还缺什么。**

完成程度分别说清：演示能运行／程序已安装／真实聊天已通过。不要用“环境检查通过”替代“真实聊天已通过”，也不把支持列表写成已在这台设备配置完成。
