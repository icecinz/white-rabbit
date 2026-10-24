# rabbit-preparation

模块2的独立准备核心：接收明确提供的消息、材料引用和图片元数据，返回输入文字与可检查的准备结果。不启动Agent，也没有内置的文件或数据库位置。

- `build_context(messages, ...)`：准备共同对话，核对材料版本与容量。
- `prepare_file_context(messages, ...)`：准备文本文件任务。
- `prepare_text_file_messages(..., read_snapshot=reader)`：通过宿主注入的读取口取得冻结文本快照，再交给上下文准备。
- `rabbit_preparation.handoff`：用明确提供的聊天快照准备“换AI接着聊”的完整交接包。保留 `candidates`、`prepare`、`material_plan`、`with_materials`、`quoted_reference_json` 和 `HandoffConflict`；材料预览与编码器由宿主提供，包内不补读文件、不启动Agent。
- `validate_material_refs`：共用严格的图片／文本引用规则，只校验ID、归属字段和SHA，不读取材料、不授予访问。
- `validate_attachment_ids` / `validate_native_attachments`：直接引用`rabbit_content.attachments`的唯一实现，核对不含路径的图片引用与原生位置；函数身份保持相同，不保留旧`rabbit_preparation.attachments`模块。
- `rabbit_preparation.text_file_context.validate_materials`：核对已传入的完整UTF-8文本快照。

`TextSnapshotReader`是一个callable合同：接收材料ID，以及`chat_id`、`expected_sha256`和`content`。`content=False`返回含`artifact`的元数据，默认调用还返回完整`text`；读取方负责归属、版本和读取限制，不可用版本抛出`rabbit_content.FileTaskStoreError`。宿主可以使用自己的存储实现，包内不直接打开文件或连接数据库。

交接材料引用无效或超限时，`with_materials`抛出现有的`MaterialReferenceError`（`invalid_request` / `capacity`）；宿主负责转成自己的状态码和提示。交接的记录顺序、版本冲突、完整容量检查和记忆选择规则保持原样。

已经准备好的完整正文也可以直接传给`build_context`。缺失材料、超限和必带记录仍按原有合同明确报告，不自行搜索路径或补取其他版本。

直接安装本目录，并提供`rabbit-content>=0.1.1,<0.2`；需要Python 3.11+。它是唯一的跨包依赖，本包不依赖小白兔宿主、界面或Agent运行器。
