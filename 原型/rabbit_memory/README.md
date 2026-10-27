# rabbit-memory

长期记忆接口的独立包：版本化配置及回执、固定来源绑定、Hindsight、Hermes 文档记忆及可选 Mem0 只读连接。宿主负责明确提供数据目录、原核心和凭据配置；包不发现 HOME，也不自行安装服务。基础包仅用 Python 标准库，Python 3.11+。

HermesFilesMemory(root, core_factory=..., core_scope=...)复用宿主提供的原 MemoryStore。每配置一个独立 home，只开放 MEMORY.md／USER.md；显式创建、手动文档编辑和只读载入分开。保存与原核心使用同一文件锁，检查 SHA 并保留旧版，保证合作写者之间的版本核对；不宣称锁住任意外部编辑器，也不自动写回聊天。

Mem0Memory(python=..., config_path=..., root=..., worker_path=...)只在真正检索时启动固定 SDK worker。缺凭据或独立环境时不可用；元数据检查不构造 Mem0、不调用模型。固定 Mem0 2.2.1，配置模板见mem0-config.example.json；可选依赖见mem0-requirements.txt。程序不借用Hindsight或环境中的Key，状态可用也不证明外部调用已通过。只读查询输出同一有界事实包；异步写入队列仍仅供 Hindsight 使用。

MemoryProfiles构造器在自己持有的连接中升级旧Hindsight单程序约束，保留行、回执、关联、索引和触发器；失败整笔回滚。initialize_schema(db)仍只参加调用者已有事务，不在调用者事务内切换外键设置。旧快照保持原九字段和原哈希口径。

从 rabbit_memory 导入 MemoryProfiles、NativeMemoryBindings、HindsightMemory、NativeHindsightReadOnly、对应错误和校验函数。原有六个同名子模块保留为公开合同；配置数据库格式与既有实现一致。

宿主必须提供数据库、配置文件路径和来源读取函数。configured_url(path)、HindsightMemory.from_config(path)、read_native_source(path)没有默认路径；probe_native_source(..., source_getter=...)和NativeHindsightReadOnly(..., source_getter=...)必须注入读取口。包不查找 Hermes 的默认 home 目录。数据库路径仍保留 iCloud、链接和普通文件保护。

    from pathlib import Path
    from rabbit_memory import HindsightMemory, read_native_source

    config = Path('/absolute/path/to/memory-config.json')
    transport = HindsightMemory.from_config(config)
    source = read_native_source(config)

MemoryProfiles.get_in(db, profile_id)在调用者现有事务中只读一份配置；连接须使用 sqlite3.Row，调用者负责校验标识、事务和快照。它不开新连接、不提交或回滚。memory_profiles.initialize_schema(db)与native_memory_bindings.initialize_schema(db)也参加调用者事务，不自行提交。

HindsightMemory和NativeHindsightReadOnly公开只读authentication_fingerprint属性，返回现有服务认证的SHA256摘要；不返回凭据，也不改变原传输的认证来源。

宿主接其他驱动时也必须取得这一明确摘要；缺少摘要时应拒绝绑定，不把未知认证当作空认证。

传输只支持明确的本机 Hindsight HTTP 地址。创建传输对象不发请求；调用健康检查、检索或保存才会访问服务。原生只读通路不创建库、不写记忆或配置，但服务可能记录检索日志。检索和写入算法沿用原实现；写入确认只表示排队，宿主需要先持久保存任务身份，并查询原回执处理未知结果。

`write_jobs`和`write_runner`提供持久写入队列与有界同步执行；根入口公开`MemoryWriteJobs`、`MemoryJobConflict`、`enqueue_completed_in`和`MemoryWriteRunner`。它们不开线程、不自动发现连接，也不选择模型。

`write_jobs.initialize_schema(db)`参加调用者事务；同一个SQLite库必须已有`runs(id, chat_id, status, request_snapshot, user_message_id)`和`messages(id, chat_id, role, status, content)`；runner按聊天读取回执还需`runs.message_id`。`enqueue_completed_in(db, run_id, *, connection_fingerprint, request_snapshot_validator)`须在回答完成的原事务内调用，传入快照校验函数；该函数接收有界JSON对象，必须返回完整、经过宿主校验的请求快照，不能只信任其中的`memory_profile`。与小白兔宿主相同的合同应传入`profiles.validate_request_snapshot`；旧回执及既有前置检查在先，校验函数仅在原快照校验位置调用。该函数不开连接、不提交或回滚调用者事务。宿主仍负责完成消息与队列入队的SAVEPOINT及失败标记。

`MemoryWriteJobs.revision_for_chat(chat_id)`在只读事务中计算该聊天写入状态的版本。队列保留原任务身份、载荷、连接指纹和未知状态；未知提交只查询原回执，不重新提交。

`MemoryWriteRunner(jobs, driver_getter, fingerprint_getter, ..., admission=None)`接收宿主的驱动、当前连接指纹、时钟与准入门。`tick(max_jobs=...)`在任何校验或锁之前进入`admission.lease('memory')`；无准入门时独立运行。宿主负责调度tick与提供驱动。`execution_lock`保护tick和recover，宿主仅可非阻塞观察，不得替执行器释放或长期占用。`recover(service_lock_held=True)`要求宿主已经持有应用独占锁并证明旧工作进程不存在；包本身无法证明这把锁的所有权。

共同聊天仍由宿主持有，整个应用的长期记忆流程尚未全部独立。包不提供Agent、界面、自动发现或后台服务。配置读取使用安全文件描述符，面向macOS / POSIX；Windows暂未验证。
