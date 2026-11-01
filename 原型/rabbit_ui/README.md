# rabbit-ui

模块5的静态界面：对话、管理、设置和原生会话的展示。保留现有HTML、CSS及浏览器脚本，不需要Node构建，也不导入宿主或其他Rabbit包。

本目录可独立安装，需要Python 3.11+。包内有52件固定资产，包含`markdown-it`及其许可。Agent身份数据由宿主提供；本包不读取用户资料、凭据、对话库或本机Agent配置。

## 公共接口

```python
from rabbit_ui import read_resource, ResourceNotFound

# 宿主已验证并序列化的浏览器初始化脚本。
bootstrap = 'window.RabbitAgentDefinitions=[{"id":"example","label":"Example","initial":"E"}];\n'
try:
    resource = read_resource("/", agent_bootstrap=bootstrap)
except ResourceNotFound:
    pass  # 宿主返回404
else:
    body = resource.data
    content_type = resource.content_type
    nonce = resource.nonce
```

`Resource`是不可变结果，字段为`data: bytes`、`content_type: str`和`nonce: str | None`。`/`及`/index.html`返回带独立nonce的自包含页面；其余固定公开URL返回相应资产。未知、非规范或带查询参数的字符串都不作路径转换，抛`ResourceNotFound`。宿主应先从HTTP请求中分离路径和查询参数。

`self_contained_document(web_root, *, agent_bootstrap)`接受明确的平面资产目录，返回`(bytes, nonce)`，便于离线核验或明确选择另一份完整界面。它按`index.html`声明的顺序打包固定CSS/JS，保留原始UTF-8字节，使用base64避免结束标签截断，不使用eval。`agent_bootstrap`必须是宿主明确提供的字符串；宿主负责其中身份数据的校验。它只加到`agents.js`前面，不会读取宿主目录。

`ASSET_FILES`列出全部52件打包资产；`INLINE_STYLES`、`INLINE_SCRIPTS`及`INLINE_ASSETS`描述自包含页面的固定资源集合。纸纹背景也内嵌于自包含页面。`markdown-it.LICENSE`随包提供，保持原入口行为，不提供HTTP路由。资源目录和叶文件不接受符号链接，叶文件必须为普通文件，单件最多2MiB。

## 对话输入区

`composer_shell.js`与`composer_shell.css`只负责布局。`RabbitComposerShell.mount({root, status, choices, input, materials, support, send, hint, label?})`接收原DOM节点，把铭牌、选择框、输入、材料和底部操作排成统一输入区。首页、Codex、Hermes与Pi共用该部件；各自控制器继续持有原输入节点、草稿、材料、原生编号和回执，不在部件里保存或发送内容。

Pi的同家AI与只读记忆选择继续使用既有配置接口，不提供局部细调或跨Agent接续。宿主通过`getModelLabel`从已经验证的模型目录缓存解析展示名称；没有准确映射时不猜测，不为铭牌另开Agent或查询模型。

首页配置与当前对话配置分开持有。专员建议可在首页隐藏时更新原草稿选择，配置和应用回执写入同一份浏览器快照；不移动正文、材料或当前页面。读取建议复用专员现有刷新，不增加轮询器。页面凭证仅在会话存储，普通恢复快照只保留不含凭证的应用回执。

## 接续准备

`continuations.js`负责独立的接续草稿，不运行Agent。宿主提供来源快照、现有配置目录、浏览器快照读写和明确的创建／提交回调；内容准备仍由`native_text_quote.js`与已有材料预览处理。只保存用户选定的文字及固定材料引用，不保存文件正文或图片像素，也不改首页和来源草稿。

每个工作区的每个来源最多一份准备。第一次发送前保存原创建编号；目标确定后保存来源关系，再把整份草稿交给原生控制器。持久交接标记写入后，准备页不再放入或重发；后续恢复由原生控制器自己的请求记录处理。`native_chats.js`仅协调现有创建、挂载和发送接口，没有第二套执行循环。

`handoff_view.js`只整理原消息的显示，任务和一条来源在外，其余引用及标准材料收进“更多”。未知格式保留原文；原图继续使用已有材料组件，显式打开时核对固定引用，不按消息里的图片路径读取文件。

## 外观主题

`soft_paper.css`最后加载，集中定义墨紫苔绿软纸的浅深色变量、系统字体和控件外观。标题宋体，正文黑体，代码等宽；不增加主题枚举或业务状态。`soft-paper.jpg`仅作静态大背景，自包含页面将这个固定引用改为JPEG数据，不依赖外部素材服务。调整外观不改变已有AI能力标记、导航、草稿或原生Agent协议。

## 工作状态视图

`work_status.js`和`work_status.css`是单独的只读视图。`RabbitWorkStatus.create({api, getWorkspace, onOpen, translate?, getLabel?})`返回`root/show/hide`；宿主先把`root`放入文档，再调用`show()`，离开时调用`hide()`。宿主提供现有主题变量与工作区身份。

视图只读取固定的`/api/management/work-status`，不读取正文、探测Agent或执行管理动作。可见时串行更新，隐藏、换工作区、页面挂起或数据过期时使旧结果失效。这里的空闲仅表示小白兔已持有连接的缓存状态。

`onOpen(row, isCurrent)`由宿主负责原对话导航，不能用新建、连接或发送替代。异步导航须继续核对`isCurrent()`；`row.workspace_id`来自已校验的快照。名称可从宿主已有目录缓存提供，不为显示名称查询正文。

## 宿主责任

宿主负责同源的`/api/*`协议、身份数据、Origin及CSRF检查、HTTP响应头与CSP。首页nonce必须用于对应响应的`script-src`和`style-src`，不能放宽为`unsafe-inline`或`unsafe-eval`。页面需要现有小白兔HTTP协议；独立安装不等于脱离后端聊天，也不保证兼容其他聊天API。

界面目前使用根路径URL、固定DOM节点、依次加载的`window.Rabbit*`组件，以及按工作区和来源隔离的浏览器恢复记录。IM.codes预览的Cookie、下载和来源处理保留在原浏览器脚本中。它们属于一套界面合同，不能当成已可任意拼换的独立控件。
