# rabbit-runtime

模块3的独立运行核心。包含通用JSONL进程传输、Codex原生会话，以及Pi、DSH、Claude和Hermes的原生运行底座。普通Codex和小白兔专员复用同一个执行持有者；角色资料、管理工具和界面由宿主提供。

本目录可直接安装，需要Python 3.11+，面向macOS／POSIX。运行核心只用标准库；Claude工作进程另需原官方Python SDK，`claude`安装附加项声明已验证版本。不包含原生Agent程序、模型或账号。Pi、DSH、Claude和Hermes分别从各自子包导入，运行包本身不加载它们或启动Agent。

## 接口

| 入口 | 责任 |
| --- | --- |
| `CodexRpc` / `BoundedRpc` | 明确argv与cwd启动原生进程，收发JSONL、限制队列与保留提交状态；`stats`不含消息正文 |
| `NativeSessionStore` | 显式路径的独立执行账本，登记工作目录身份、原生绑定及操作回执 |
| `NativeSessionLease` | 与账本相邻的会话租约，防止同一原生会话被并行持有 |
| `CodexNativeSession` | 初始化、创建／恢复会话、提交、审批和中断；由调用者消费事件 |
| `SpecialistService` | 持有单一事件读取者，提供`open`、`send`、`view`、审批及断开；名称沿用旧实现，普通Codex也使用它 |
| `CodexNativeConnection` | 原生默认启动参数和惰性持有者；不添加小白兔管理扩展 |
| `maintenance_state` | 持有者和账本分别公开只含计数的维护快照，避免宿主读取内部锁或请求 |
| `CodexModelObservation` | 已打开连接的模型目录身份守卫；向宿主返回不透明快照，只公开epoch，不暴露RPC或锁 |

文件检查、严格JSON、模型参数校验分别由公开子模块提供。通用工具只有一份实现；宿主可以使用自己的HTTP、内容库和记忆系统，不需要导入小白兔服务。

## 可选外部工作读取

`rabbit_runtime.hapi_work`提供独立的`HapiWorkReader`和纯投影函数`project_sessions`，只依赖标准库，没有小白兔宿主依赖。它只读取明确本机Hub的固定工作列表；构造不会联网，不读取账号或配置文件。

```python
from rabbit_runtime.hapi_work import HapiWorkReader, HapiWorkError

snapshot = HapiWorkReader(origin, jwt).snapshot()  # 调用者提供地址和内存JWT
```

结果范围为`hapi_hub_summary`，状态仅代表Hub所报；保留原生身份未知，不提供发送、审批、停止或恢复。失败抛固定错误代码，不返回缓存。JWT的取得与更新、原网页登录和手机可达性归宿主处理。本模块不启用HAPI，也不改变现有运行核心。

固定HAPI 0.30.7的实际列表投影及隔离安装已验证。正式宿主现通过独立的 `external_work.py` 调用本模块，手动绑定和刷新；不改变运行核心或 SDK 工作进程。最初合同与宿主接线分别记录。

## 装配例子

```python
from pathlib import Path
from rabbit_runtime import CodexNativeConnection, NativeSessionStore

data = Path("/absolute/local/data")       # 已存在的本地目录
work = Path("/absolute/local/workspace") # 已存在的工作目录
store = NativeSessionStore(data / "native.sqlite")
holder = CodexNativeConnection().create(
    store, cwd=work, workspace_id="a" * 64, session_key="example/codex",
)
try:
    holder.open()  # 到这里才启动Codex；会访问原生配置和在线服务
    holder.send("unique-client-id", [{"type": "text", "text": "你好"}])
    view = holder.view()  # 输出、审批与运行状态；完成需按状态确认
finally:
    holder.close()
    store.close()
```

账本文件、会话键和工作目录需要保持原身份。重新启动时再次提供同一路径和键，恢复原绑定；不要复制账本后冒充同一执行者。构造连接不会启动Agent，导入只在内存SQLite中计算预期schema，不打开磁盘账本。

## Pi底座

`rabbit_runtime.pi`提供`PiNativeConnection`、`PiRpc`、`PiNativeStore`、`validate_session_file`、`PiModelCapabilities`及消息投影／原文读取函数。协议容量和原生模型目录转换由`material_protocol`、`profile_catalog`子模块提供。

```python
from rabbit_runtime.pi import PiNativeConnection

connection = PiNativeConnection(
    node="/absolute/node",
    cli_script="/absolute/pi-coding-agent/dist/cli.js",
)
# 两个目录由宿主明确提供并事先创建。这里才启动原生Pi。
with connection.create_rpc(cwd="/absolute/work", session_dir="/absolute/sessions") as rpc:
    state = rpc.request("get_state")
```

恢复时明确传入原有`session_file`；缺失文件不能用新会话代替。启动前的文件核验不会锁住原生文件，持有者仍须在发送前核对`get_state`的原生会话ID和文件路径。`PiNativeStore`只保存绑定、请求指纹和保守回执，不保存提示词正文；同编号重放返回旧记录，未知提交跨重启保留，调用者只对新认领的记录尝试一次发送。RPC确认不代表原生一轮已经完成。

Pi保留原生配置、工具、扩展询问和原始事件。连接使用明确的Node、CLI、工作目录与会话目录，原生进程沿用调用环境，不另找账号或重写全局设置。一个RPC只由一个上层持有者消费事件；本底座不会自动批准询问、选择模型、补造完成事件或重试未知提交。

`PiModelCapabilities`从明确的CLI安装定位原SDK，调用同包的`thinking_levels.mjs`查询原生思考等级；该离线helper需要支持`--permission`的Node。它使用固定文件读取范围和独立环境，不启动Pi Agent、不查询账号或联网，也不是任意JavaScript的通用沙箱。

`validate_session_id`保留Pi原生ID规则；`decode_record`与`file_stamp`让宿主复用同一份JSONL解码与文件版本规则。单条解码不替代`validate_session_file`对目录、完整树、容量和稳定性的核验。

## DSH底座

`rabbit_runtime.dsh`提供`DshNativeConnection`、`DshRpc`、`DSHNativeStore`、`validate_session_id`及原有错误类型。连接需明确提供Node、原版CLI与安装根；可选原有DSH目录、固定版本和preset。`create_rpc(cwd=..., session_id=...)`只构造连接，`start()`才启动随包的`worker.mjs`和原版CLI。它沿用原有原生配置与协议，不另造Agent循环。

同一个worker既收发JSONL，也作为原版CLI的插件；包内路径必须保持，不能只复制一个Python连接文件。RPC原样保留`rpcId/result`和原生事件，单一调用者依次消费`next_event`。提示词必须有预分配的`rpc_id`，通信超时与原生执行完成分别判断；`close()`只有在进程、读取线程和持有进程组都确认退出时才算清理完成。

`DSHNativeStore`使用明确路径记录会话绑定、原始来源身份与请求回执，不保存提示词正文或模型凭据。新认领的操作仅允许发送一次，重开后的未知结果不补发；原生结束事件与flush持久化证明分别记录。`maintenance_state()`只返回三项有界计数和是否完整，不公开内部锁或任意SQL。公开的`validate_session_id`保持连接原有ASCII编号规则，账本原有ID规则另行保持。

`rabbit_runtime.process`公开`invoke`、`ProcessError`和`observe_process_group`，供一次性JSONL工作进程复用。观察结果为存活、已退出或未知；未知不能作为清理成功证明。这一层只管理输入输出、期限、中止和进程组，不处理模型、账号或消息含义。

## Claude底座

`rabbit_runtime.claude`提供`ClaudeNativeConnection`、`ClaudeRpc`、`ClaudeNativeStore`、材料codec、原记录／原图投影及原会话文件核验。SDK工作进程保留原CLI、SDK版本、工作目录、设置来源和一次审批；模型与权限默认值继续由原生Agent决定。

**在连接所选的SDK Python里正常安装同一版运行包。** 宿主运行包、SDK工作进程和分发元数据必须同版；不能只把Python文件复制到项目里。现有SDK可用离线wheel加`--no-deps`安装运行包，保留其他包；新环境可选`claude`附加项，CLI仍需单独安装。安装、更新和回滚时分别核对解释器与运行包，不由第一次发送偷偷装软件。

连接用原SDK解释器的`-B -P -m rabbit_runtime.claude.native_worker`入口；`-P`排除新增的当前目录搜索，继承其余原环境和user-site，不添加`PYTHONPATH`或改写`sys.path`。工作进程先核对`--expected-runtime`，再核SDK和CLI，版本不符不启动Agent。子包入口不预先导入工作进程，避免模块重复执行。

账本`validate_uuid`保留原错误类型与精确UUID规则；`maintenance_state()`只返回原三项有界维护计数。原历史仍由同一个SDK定位，保留原文件、目录身份和完整内容核验。上层对话、HTTP、材料／记忆事务和原生发送协调由宿主提供。

Claude 账本 V2 可在 `finish(..., result=record)` 的原终态事务内冻结成功 SDK Result 的有限投影，正文最多 128 KiB。它与原请求、session、epoch、用户及 Result UUID 一起保存，不保存 SDK 的费用、用量等额外字段。普通回执仍只有元数据；`completed_result(chat_id, client_id)` 单独读取保存结果，不调用 Agent，也不把历史 epoch 说成当前执行者状态。

只有精确匹配的 V1 库可在写入者首次打开时原子升级；旧行结果保持空，不从历史补填。只读 V1 不升级。损坏或身份不符会拒绝读取，不用最新回答替代。V2 库不能直接交回旧 V1 程序，部署和回滚须保留对应数据库快照。

## Hermes底座

`rabbit_runtime.hermes`提供`HermesNativeConnection`、`HermesRpc`、`HermesNativeStore`、模型／消息codec与原生修订核验。宿主明确提供Node、原有Hermes Python、源码根、工作目录和可选home、commit及扩展开关。原生配置、profile初始化、工具、审批和后台服务仍由Hermes决定；启动原生连接不等于只读操作。

连接按包内相邻路径启动`worker.mjs`，保留官方gateway客户端和原进程组关闭检查。可选`native_bootstrap.py`继续由所选Hermes Python直接执行，先以原生源码为导入来源、完成原profile环境初始化，再按相邻路径加载`session_settings.py`、`image_queue.py`与可选`result_cache.py`。它们保持自包含，不要求给Hermes解释器安装运行包，也不新增`-I`、环境修复或替代Agent循环。

普通聊天继续使用原`prompt.submit`。交办任务可先探测当前连接结果扩展，再用单次提交编号读取原生完成文本；缓存不提供跨重启恢复或耐久去重。扩展与原生工作树不兼容时，已有设置、图片和普通聊天继续运行；交办任务不得自动降级补发。

账本公开`validate_session_id`，消息codec公开`reference_value`，都沿用原有算法与错误类型。`maintenance_state()`只读原`hermes_operations`的pending计数，继续使用原锁和文件身份；未知请求不重发。对话、HTTP、profile／材料／记忆事务、附件协调与历史adoption仍归宿主。

运行包附带`vendor/hermes_gateway/`的生成MJS、MIT许可和原始`upstream.json`，三者保留原字节。TS源码和构建入口留在项目的`原型/vendor/hermes_gateway/json-rpc-gateway.ts`与`原型/build_hermes_gateway.mjs`；构建器从该源码位置读取TS，向包内输出MJS。`upstream.json`中的旧相对路径是保留的历史来源记录，不作为当前构建入口。

## 不变的边界

- 未知提交不会自动重发。断线、超时和失败不等于原生工具已退出。
- 审批绑定当前连接与请求，只能回答一次；保留原生Agent的权限决定。
- 低层会话与执行持有者二选一，不同时消费一个RPC的事件。
- 维护统计是有界、非阻塞的观察；不完整快照不能当成空闲证明。
- 模型目录刷新只观察已经打开的连接，前后核对同一身份；不会为了列模型创建会话。

小白兔宿主仍管理聊天目录、五种Agent的HTTP入口、各Agent上层持有者和配置／材料／记忆协调、专员角色和设备连接。底座独立不等于上层发送协调已独立；本包独立也不代表整个模块3已拆完，或所有Agent能力均已验收。

> 公开说明：内部验收记录不随公开源码分发。
