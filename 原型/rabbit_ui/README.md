# rabbit-ui

模块5的静态界面：对话、管理、设置和原生会话的展示。保留现有HTML、CSS及浏览器脚本，不需要Node构建，也不导入宿主或其他Rabbit包。

本目录可独立安装，需要Python 3.11+。包内有43件固定资产，包含`markdown-it`及其许可。Agent身份数据由宿主提供；本包不读取用户资料、凭据、对话库或本机Agent配置。

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

`ASSET_FILES`列出全部43件打包资产；`INLINE_STYLES`、`INLINE_SCRIPTS`及`INLINE_ASSETS`描述自包含页面的固定资源集合。`markdown-it.LICENSE`随包提供，保持原入口行为，不提供HTTP路由。资源目录和叶文件不接受符号链接，叶文件必须为普通文件，单件最多2MiB。

## 工作状态视图

`work_status.js`和`work_status.css`是单独的只读视图。`RabbitWorkStatus.create({api, getWorkspace, onOpen, translate?, getLabel?})`返回`root/show/hide`；宿主先把`root`放入文档，再调用`show()`，离开时调用`hide()`。宿主提供现有主题变量与工作区身份。

视图只读取固定的`/api/management/work-status`，不读取正文、探测Agent或执行管理动作。可见时串行更新，隐藏、换工作区、页面挂起或数据过期时使旧结果失效。这里的空闲仅表示小白兔已持有连接的缓存状态。

`onOpen(row, isCurrent)`由宿主负责原对话导航，不能用新建、连接或发送替代。异步导航须继续核对`isCurrent()`；`row.workspace_id`来自已校验的快照。名称可从宿主已有目录缓存提供，不为显示名称查询正文。

## 宿主责任

宿主负责同源的`/api/*`协议、身份数据、Origin及CSRF检查、HTTP响应头与CSP。首页nonce必须用于对应响应的`script-src`和`style-src`，不能放宽为`unsafe-inline`或`unsafe-eval`。页面需要现有小白兔HTTP协议；独立安装不等于脱离后端聊天，也不保证兼容其他聊天API。

界面目前使用根路径URL、固定DOM节点、依次加载的`window.Rabbit*`组件，以及按工作区和来源隔离的浏览器恢复记录。IM.codes预览的Cookie、下载和来源处理保留在原浏览器脚本中。它们属于一套界面合同，不能当成已可任意拼换的独立控件。
