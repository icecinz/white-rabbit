# 连接原生 Agent

[English](native-agents.md) · [简体中文](native-agents.zh-CN.md) · [首页](../README.zh-CN.md)

先选**一个你已经在使用的 Agent**。小白兔通过它原来的 CLI、SDK 或协议连接，不随包提供替代 Agent 或模型账号。当前源码面向 macOS／POSIX，需要 Python 3.11+。

这些通路创建或恢复由小白兔管理的受支持原生会话，不会自动接上 Codex 桌面、WorkBuddy 或其他窗口里正在执行的任务。读取历史、恢复保存的会话、控制外部存活执行者是不同能力。

## 演示与真实模式不同

`python3 -B demo.py` 使用临时虚构资料和规则回复。下面启动的是真实应用：它可以索引本机原生历史、保存应用记录、启动 Agent，并沿用当前凭据和模型设置。新建小白兔数据目录不等于关闭原生历史发现。在线 Agent 会按所选服务传输你发送的材料；原生启动也可能写日志、更新缓存或产生后台任务。

凭据继续留在原 Agent 的配置中，不写进启动参数、仓库、聊天或项目说明。

## 持久安装

让 Codex、Hermes 或 DSH 帮用户安装时，先按[安装清单](install-agent.zh-CN.md)完成一条真实通路。下面的 JSON 只记录本机路径，不存账号或 Key；**任选一组**写到仓库外的 `$HOME/.white-rabbit-connections.json`，把所有 `/absolute/path/to/...` 换成已核实的绝对路径。JSON 内不会展开 `$HOME` 或 `~`。

Codex：

```json
{"schema_version":1,"connections":{"codex":{"cli":"/absolute/path/to/codex"}}}
```

Hermes：

```json
{"schema_version":1,"connections":{"hermes":{"node":"/absolute/path/to/node","python":"/absolute/path/to/hermes-agent/venv/bin/python","root":"/absolute/path/to/hermes-agent"}}}
```

DeepSeek Harness：

```json
{"schema_version":1,"connections":{"dsh":{"node":"/absolute/path/to/node","cli":"/absolute/path/to/deepseek-harness/lib/bin.js","root":"/absolute/path/to/deepseek-harness"}}}
```

在仓库根目录预检并安装：

```sh
python3 -B 原型/install_local.py check --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
python3 -B 原型/install_local.py install --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
```

预检不启动 Agent、不调用模型，只核基础环境及路径；登录、SDK／CLI 兼容和真实对话还需后续核验。安装器保存私有程序副本和数据在 `$HOME/.imcodes/one-ai`，注册当前用户登录启动，不需要另外安装 IM.codes。以后双击该目录的 `打开小白兔.command`，或运行 `python3 -B "$HOME/.imcodes/one-ai/manage.py" open`。

已有安装更新时省略 `--native-config` 可保留原连接。传入 JSON 会完整替换 `connections`；加新 Agent 前先合并原条目。先让一个 Agent 真正聊通，再配记忆、专员或其他 Agent。

## 前台调试命令

在仓库根目录运行。使用单独的调试数据目录和端口，不要与已安装服务或 Agent 正文目录混用：

```sh
python3 -B 原型/server.py \
  --data-dir "$HOME/.white-rabbit-dev" \
  --port 17861
```

将下面**任选一组参数追加到同一条命令末尾**。所有 `/absolute/path/to/...` 都需要换成本机路径。打开 <http://127.0.0.1:17861/>，选择已配置的原生 Agent／配置，再明确发送一条简短请求。只运行基础命令不会启用这些原生连接。

此方式在当前终端前台运行，便于开发或排查，不负责登录启动。完整参数以 `python3 -B 原型/server.py --help` 为准。

### Codex

基础原生通路只需要提供已安装、已登录的 Codex CLI：

```sh
--native-codex-cli "/absolute/path/to/codex"
```

如果还要附加小白兔的应用管理 MCP 工具，改用已安装 Python `mcp` SDK 的解释器：

```sh
--native-chat-mcp-python "/absolute/path/to/mcp-venv/bin/python"
```

该路线通过环境的 `PATH` 找到 Codex。两种 Codex 启动参数不能同时指定；管理扩展与基础原生连接是不同路径。

### Pi

提供已安装、配置好的 Pi 对应的 Node 和 CLI 脚本：

```sh
--native-pi-node "/absolute/path/to/node" \
--native-pi-cli "/absolute/path/to/pi-coding-agent/dist/cli.js"
```

两项必须同时提供，沿用 Pi 的原生 RPC 和会话文件。Node 版本需满足所选 Pi；模型能力查询 helper 还需要支持 `--permission` 的 Node。

### Hermes

需要可正常运行的上游 Hermes 环境，包括其服务端依赖：

```sh
--native-hermes-node "/absolute/path/to/node" \
--native-hermes-python "/absolute/path/to/hermes-agent/venv/bin/python" \
--native-hermes-root "/absolute/path/to/hermes-agent"
```

三项必须同时提供，使用 Hermes 自己支持的 Python 环境、登录和配置。可选 `--native-hermes-home` 明确指定资料目录。`--native-hermes-session-settings` 是额外的版本限定设置校验，默认关闭。

包内 `原型/rabbit_runtime/hermes/vendor/hermes_gateway/` 保留生成的通信客户端、MIT 许可及上游指纹，另附 TypeScript 源码和构建脚本。普通使用不需要重新构建；重新构建会核对 `upstream.json` 记录的精确 Node 版本。

### Claude Code

同时提供原有 CLI 和已安装官方 Claude Agent SDK 的 Python：

```sh
--native-claude-python "/absolute/path/to/claude-sdk-venv/bin/python" \
--native-claude-cli "/absolute/path/to/claude"
```

此快照默认核对 **Claude Agent SDK 0.2.152** 和 **Claude CLI 2.1.87**。其他版本需要明确适配，不能只改文件名绕过。

连接前，在同一个 SDK Python 环境正常安装本仓库的同版 `rabbit-runtime`：

```sh
"/absolute/path/to/claude-sdk-venv/bin/python" -m pip install --no-deps ./原型/rabbit_runtime
```

这条命令只安装本地运行包，不安装或升级 SDK。工作进程会核对分发版本，版本不符时拒绝启动。Agent、账号和模型配置需自行准备，第一次发送不会悄悄安装缺失软件。

### DeepSeek Harness

提供 Node、已安装的原生 CLI 入口和包根目录：

```sh
--native-dsh-node "/absolute/path/to/node" \
--native-dsh-cli "/absolute/path/to/deepseek-harness/lib/bin.js" \
--native-dsh-root "/absolute/path/to/deepseek-harness"
```

三项必须同时提供。此快照默认核对包版本 **0.1.0-rc.6**，沿用原生 profile、工具和协议。可选 `--native-dsh-home`、`--native-dsh-history-root` 明确指定资料／历史目录，必须与已配置的原生入口一起使用。

### 小白兔专员（可选）

独立的管理专员需要已有的 `mcp` SDK Python 和已登录的 Codex CLI：

```sh
--specialist-mcp-python "/absolute/path/to/mcp-venv/bin/python" \
--specialist-codex-cli "/absolute/path/to/codex"
```

它保留独立固定原生对话，调用与界面共用的应用管理接口。当前角色请求固定模型／强度，能否使用还取决于原生账号。可选的工作说明模式另需项目角色文档，入门路径不需要开启。见[专员能做什么](specialist.zh-CN.md)。

手动**核对并更新**报告另有分析依赖：已有、可用的 Claude 文字适配器，关闭工具和原生会话持久化，只分析程序给定的四项元数据，并记录请求／连接报告的型号来源，不取得 Codex 专员的管理工具。缺少这条可选通路时仍可保存程序事实，说明保持未完成；刷新旧报告不重复分析、不改设置。见[报告流程](specialist.zh-CN.md#手动状态报告)。首次安装并接通一个真实 Agent 不要求先配置这条分析路线。

## 使用边界

五条通路已有文字对话、兼容的完整文本材料和会话接续实现。恢复依赖原会话、工作目录与原生资料仍可用。

| 通路 | 下轮保存配置 | 材料／结果边界 |
| --- | --- | --- |
| Codex | 模型与支持的强度 | 完整文本；指定受支持配置才开放原图 |
| Hermes | 模型与支持的强度 | 完整文本与兼容原图；准确交办结果可能依赖当前连接缓存 |
| Pi | 模型与支持的强度 | 完整文本；图片需已验证的视觉模型和通路；交办结果限当前连接 |
| Claude Code | 选择模型，强度沿用原生 | 完整文本；图片未开放；符合条件的 SDK 最终结果可在断开后从有界本机回执读回 |
| DeepSeek Harness | 局部模型覆盖暂未接通，因为现有原生设置接口可能修改全局默认 | 完整文本及下轮只读记忆选择；图片未开放 |

五条通路可以独立选择下轮附加的受支持只读记忆。Hindsight、Hermes 文档记忆和可选 Mem0 桥有不同的配置及读写能力。选择记忆不会清除已进入原生会话的上文，也不是关闭原 Agent 全部记忆的开关。

有原生目标输入／结果接口，不代表所有完整专员交办链都已验收。读取结果需核对原请求、会话与轮次身份，不能取最近一条回答代替；冷连接或缺少缓存也不能触发自动补发。

Claude 的本机操作账本现在可以保存**最终回答正文**，不只有状态元数据。只保存成功、未截断、不属于子任务的顶层 SDK Result，UTF-8 正文最多 128 KiB，同时核对 SHA-256 及原 chat／client／session／epoch 身份。断开或重建后匹配读回标为 `stored_sdk_result`，不声称原连接当前存活。旧回执没有正文不补写；失败、子任务、截断／超限、损坏或身份不符，均不能当作成功的存储回答。Pi／Hermes 仍依赖当前连接结果缓存。这份有限正文不替代原 Agent 的完整历史，也不恢复其内部状态。

Agent 继续保留自己的正文、工具、权限和配置。材料交接不迁移完整内部状态。离线测试通过、连接成功、真实任务完成是不同证据，本版不承诺所有工具、扩展、审批和恢复情况都已经覆盖。

服务只监听本机。手机访问目前依赖另行配置的 IM.codes 及同一台在线 Mac，不是公开托管服务或跨电脑任务调度器。

## 查看当前工作与 HAPI（可选）

**管理 → Agent → 当前工作**显示小白兔已经持有原生连接的元数据，点条目回到同一对话。空列表不证明其他应用或后台工具处于空闲。

已经使用本机 HAPI Hub 时，可以用随包工具手动绑定只读工作发现通路：

```sh
python3 -B 工具/hapi_work_connect.py --origin http://127.0.0.1:3006
```

将端口改为实际 Hub 端口。工具只接受明确的本机 HTTP 地址，终端不回显地询问原 HAPI access token，交换出的 JWT 只留在小白兔内存。小白兔服务使用其他端口时追加 `--rabbit-port`。工具不启动 HAPI 或 Agent。

在**管理 → Agent**手动刷新后读取 Hub 报告的工作摘要；点条目打开 HAPI 原页面。状态只代表 HAPI 所报，不证明小白兔已控制工作或读到全部原生状态。发送、审批和停止继续在 HAPI 处理。服务重启或认证过期后需手动重连；`--clear`只清小白兔内存中的绑定。
