# 给安装 Agent 的清单

[English](install-agent.md) · [简体中文](install-agent.zh-CN.md) · [首页](../README.zh-CN.md)

这份清单给帮用户安装的 AI 或熟悉命令行的人看。第一次使用的人可以先读[新手安装说明](getting-started.zh-CN.md)。

装好以后，用户应当能方便打开小白兔，与至少一个真实 Agent 连续聊天。只运行演示或打开空页面，还没有完成安装。

可以克隆仓库，也可以[下载 ZIP](https://github.com/icecinz/white-rabbit/archive/refs/heads/main.zip)后解压，下载 ZIP 不需要 Git。先把源码放到准备长期保留的目录，再进入根目录操作，确认里面有 `AGENTS.md`、`demo.py` 和 `原型/install_local.py`。文件夹名称可以不同。不要在临时目录或压缩包预览中安装。源码 ZIP 也不是配好后的应用安装包。

## 1. 先看现有环境

- 确认系统和 Python 版本。当前需 Python 3.11+；持久本机安装先按 macOS 路线，Windows 暂不支持。
- 检查是否已经装过小白兔、17860 端口是否被使用；已有安装先核对，不能覆盖另一份运行数据或中断任务。
- 找到用户平时使用的一个 Agent 及准确的 CLI、运行环境和资料目录。先复用它，不要求装齐 Codex、Hermes、Pi、Claude Code、DSH。
- 用原 Agent 的登录状态入口核对认证，不打印密钥或整份凭据文件。不清楚使用哪个环境、账号或资料目录时，只问缺少的那一项。

找到一个稳定的 Python 3.11 或更新版本，记录它的绝对路径。登录服务以后仍要用它，不要选会删除的临时环境。可检查 `command -v python3.13`、`command -v python3.12`、`command -v python3.11` 和 `command -v python3` 返回的路径，再逐个运行 `"/绝对路径/python" --version`。系统自带的版本可能太旧，缺少时先安装。下面的路径需要替换成查到的真实路径：

```sh
RABBIT_PYTHON="/absolute/path/to/python3.11"
"$RABBIT_PYTHON" --version
```

图片功能还需要服务 Python 安装 Pillow。预检会给出 `service_python.path`，但不会运行它检查版本或图片依赖。用这个路径运行 `--version`，再运行 `-c "from PIL import Image; print(Image.__version__)"` 核对。缺少时在选定环境中安装，再检查一次。先只配置文字聊天也可以，交接时说明图片暂不可用。

## 2. 选一个真实通路

按[原生连接](native-agents.zh-CN.md)核对这一个 Agent 所需路径及版本。保留它正在生效的模型、提供商、工具和权限；例如 Claude Code 已由 CC Switch 管理时，不把它改回另一套默认配置。

持久连接 JSON 只有 Hermes／DSH 支持 `home`。登录启动服务不会自动保存 `CODEX_HOME`／`CLAUDE_CONFIG_DIR`，在终端设置这些变量还不够。先确认服务会使用原账号、资料目录和提供商配置。确认不了就暂停，或使用另一个已经核实可用的现有 Agent，不要擅自改用默认配置、迁移凭据或复制密钥。

缺登录、API 权限或依赖时，明确列出所缺内容，让用户在本地完成必要认证；不要要求把 Key 发到聊天。未经核实的模型或连接标为待配置。

## 3. 安装并留下打开方式

在仓库根目录先运行只读预检：

```sh
"$RABBIT_PYTHON" -B 原型/install_local.py check --source-dir "$PWD/原型"
```

按[连接配置示例](native-agents.zh-CN.md#持久安装)，把查实的绝对路径写进仓库外的 `$HOME/.white-rabbit-connections.json`。这份文件由安装助手准备，程序不会自动配好；里面不放 Key。先检查：

```sh
"$RABBIT_PYTHON" -B 原型/install_local.py check --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
```

预检的 `status` 显示 `ready_for_installation` 后，再安装并打开页面：

```sh
"$RABBIT_PYTHON" -B 原型/install_local.py install --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
```

安装成功后，再打开页面：

```sh
"$RABBIT_PYTHON" -B "$HOME/.imcodes/one-ai/manage.py" open
```

`check` 只核对基础环境、路径和 17860 端口。`service` 会显示 `free`、`current_installation`、`other` 或 `unknown`。其他服务占用或状态未知时先查清楚，安装器还会检查活动任务和维护状态。空连接会提示 `needs_configuration`，先补好再安装。`ready_for_installation` 仍不证明登录、SDK 版本或真实回答可用。首次使用需接通至少一个普通 Agent，只有专员或空连接还不能聊天。

`install` 会部署程序并启动登录服务，但不会自动打开浏览器。上面的 `open` 才负责打开页面；安装失败时不要继续运行它。

预检的 `visible_commands` 可帮助定位当前 PATH 中的程序，再逐一核对实际安装位置与原 Agent 的状态；它不是完整扫描，也不代表程序已经可用。不要读取或打印凭据。

程序和数据保存在 `$HOME/.imcodes/one-ai`，安装器会注册当前用户的登录启动服务。不需要安装 IM.codes。

以后打开 Finder，按 **Command + Shift + G**，输入 `~/.imcodes/one-ai`，再双击 **打开小白兔.command**。这个目录默认隐藏，安装器不会自动在桌面放入口。用户需要时，可为该文件建立桌面替身，原文件留在原处。也可以用安装时选定的 Python 打开，下面的路径需要替换：

```sh
"/absolute/path/to/python3.11" -B "$HOME/.imcodes/one-ai/manage.py" open
```

更新已有安装时，省略 `--native-config` 会保留连接配置。传入的 `connections` 会完整替换原配置。增加 Agent 前先合并已有条目，不能只写新增的一个。

涉及数据格式升级时，在没有活动任务时为要迁移的账本做 SQLite 一致备份，并与旧程序版本一起保留；不要在服务运行时只复制 `.sqlite` 文件而漏掉 WAL。安装器的恢复副本只含程序、启动配置和控制脚本，不含数据数据库。Hermes／Pi 的结果保留更新会将各自的 `hermes-native.sqlite`／`pi-native.sqlite` 从 v1 升至 v2，旧程序不能直接读取 v2。`rollback` 只恢复程序；若需恢复迁移前账本，先保留当前数据并说明会失去升级后的新记录，由用户决定，不自动覆盖。

保留解压或克隆得到的源码目录，供查看项目文档及以后更新；运行数据另存在 `$HOME/.imcodes/one-ai`。更新默认沿用已保存的服务 Python，不会因调用安装器的环境不同而更换。如果旧路径不可用，先选定新的稳定 Python 3.11+，分别在 `check` 和 `install` 中加 `--python "$RABBIT_PYTHON"` 明确更换；同时核对它的 Pillow。不要假设会自动迁移解释器。

把小白兔数据与 Agent 的原始资料分开保存，使用绝对路径记录所选环境。安装后确认页面、启动文件及数据目录实际存在；退出当前终端后仍能按上述方式打开。

第一次只配置所选普通 Agent。记忆、明确配置的小白兔专员核心和其他 Agent 可在真实聊天成功后继续，不把可选功能当安装前提。没有 Codex 的用户可以先使用自己的 Hermes 或 DSH；普通 DSH 接入不自动配置其专员角色。是否配置专员、哪种核心已实际核验，都如实说明。手机预览已转为历史，不列为安装选项。

## 4. 从真实界面验收

全新配置优先采用本次已启用、能核对默认参数的原生入口；没有合适默认时再明确选择。已有用户保存的默认不自动改动，也不为了完成安装强行接入 Codex。

1. 打开已安装的正式入口 `http://127.0.0.1:17860/`，选择刚配置的真实原生 Agent，建立一段新的验收对话。
2. 发送一句包含随机短代号的普通请求，例如“这次验收代号是纸船7318，请简短确认；不用调用工具”。确认收到原 Agent 的真实回答，并记录其实际报告的模型信息；未报告的型号或强度不猜。
3. **页面刷新：** 刷新后打开同一段对话，确认问答仍在；再问“刚才的验收代号是什么？”，核对同一原生会话的连续回答。
4. **关页面后重开：** 关闭浏览器页面和安装终端，再用留下的 `打开小白兔.command` 或 `manage.py open` 入口返回。确认能找到同一段对话和已保存内容，不再发送来检查。关页面或终端不会停止后台服务，这一步只证明重新进入。
5. **服务重启：** 仅在没有活动任务、沿用既有维护护栏实际正常重启服务后，单独记录这项结果；再用留下的入口打开并核对同一段对话和内容。不能正常执行或确认重启时标为未验证，不能用关页面重开替代。
6. **注销后登录启动：** 仅在实际注销、重新登录同一 macOS 账号后，核对登录服务启动及同一份数据仍可用，单独记录结果。当前不便注销登录时标为未验证，临时目录或当前用户已运行的会话不能替代。

第 3–6 步分别记录结果和证据。没有新账号或另一台 Mac 时不要求新增，也不借用或复制凭据把已有环境冒充新用户验收。

重开后的 Hermes 原会话若显示“恢复原会话”，先说明原生恢复也可能继续未完成任务，由用户明确恢复，再核对已有内容；不通过发送重复问题来让历史出现。

请求结果不明时，核对原编号，不用重发来判断是否成功。原生登录、授权与审批由用户决定。浏览器不可用或缺少必要权限时，把这项检查留给用户完成。

## 5. 简短交接

只交代这五项：**怎样打开、数据在哪里、接入了哪个 Agent／模型、真实验收结果、还缺什么。**

完成程度分别说清：演示能运行／程序已安装／真实聊天已通过。不要用“环境检查通过”替代“真实聊天已通过”，也不把支持列表写成已在这台设备配置完成。
