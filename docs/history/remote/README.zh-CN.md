# 手机访问实验的一段往事

[English](README.md) · [简体中文](README.zh-CN.md) · [返回小白兔](../../../README.zh-CN.md)

小白兔曾给一台在线 Mac 开过一扇小窗：在 iPhone 的 IM.codes 里打开本机网页，发消息，再回到同一份已保存的对话。下面列出的具体流程确实走通过；让同一个 AI 接管多台电脑、唤醒离线设备，则没有做成。

应用蓝图 2.0 把重心收回到一站式使用和管理 AI。0.2.0-alpha.1 源码快照已撤手机预览入口与 IM.codes 专用兼容。本页保存这段历史，不提供远程安装指南。

## 真正走通过的流程

以下根据当时的设备核验记录整理。私有记录、账号、对话、图片和访问链接不随本页公开。手机上的观察与本机存储核对各自证明不同部分。

| 时间 | 核验了什么 | 能说明什么 |
| --- | --- | --- |
| 2026 年 10 月 10–11 日 | iPhone 打开独立虚构预览，页面可读，测试消息收到规则模拟回复，刷新后仍在 | 手机预览和传输通路可用；没有调用真实模型 |
| 2026 年 10 月 11 日 | iPhone 经 IM.codes 打开 Mac 正常服务，向 Hermes 发送文字；用户确认收到回答、刷新仍在，本机元数据确认该轮完成并保存 | 在既有账号下，一条真实的手机文字聊天通路；服务仍由在线 Mac 承载 |
| 2026 年 10 月 12 日 | 用户确认能添加和查看图片，发送给 Codex，收到回答，刷新后图片和消息仍在；本机元数据确认对应请求完成且带有图片 | 手机基本图片流程通过；没有另行评估回答的视觉理解是否准确 |

这些真实手机核验使用的是小白兔公开发行前的版本，以及 **IM.codes 2026.8.3638** 和适用于该版本的本机转发修复。它们不是全新安装或独立账号验收。服务和数据都在 Mac，电脑睡眠或关机后，这条通路就暂时不可达。

## 公开旧代码中保留的成果

经筛选的兼容实现后来进入了公开 alpha 快照。下列固定链接无需搬出私有归档即可检查：

| 成果 | alpha.4 中的公开源码 |
| --- | --- |
| 首次 HTML 自包含所需样式、脚本和图片，避免预览加载依赖后续静态资源 Cookie | [界面资源装配](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/原型/rabbit_ui/resources.py#L96-L177) |
| 将外层预览的请求校验与小白兔自己的请求校验分开处理 | [预览请求适配](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/原型/rabbit_ui/assets/app.js#L85-L161) |
| 有界读取转发使用的 HTTP 分块请求正文 | [请求正文读取](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/原型/server.py#L4743-L4788) |
| 内嵌预览导出文字时，提示先在独立标签页打开 | [导出限制说明](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/原型/rabbit_ui/assets/app.js#L2777-L2780) |

对应公开快照为 [alpha.1](https://github.com/icecinz/white-rabbit/commit/b716664a4549ebe67ccbcd16841892c5d1aeb38a) 和 [alpha.4](https://github.com/icecinz/white-rabbit/commit/9e88305e17c4eca95550d86a6cc5fc4298b20296)。[alpha.1 的范围说明](https://github.com/icecinz/white-rabbit/blob/b716664a4549ebe67ccbcd16841892c5d1aeb38a/README.md#L58)已经明确排除跨电脑调度与唤醒；[alpha.4 的范围说明](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/README.md#L34-L60)也把访问同一台 Mac 与未来设备控制分开。

这些提交能证明当时公开了什么，不能替代后续每个版本的 iPhone 实机验收。当时核验用的是限定版本的 IM.codes 本机修复，本页不对后来上游版本作结论，也不复制补丁。链接旧代码是为了保留成果，不把旧兼容层作为新版运行依赖。

## 没有交付的能力

- 让同一个 AI 跨电脑接管工作，或唤醒离线电脑。
- 在手机独立运行 AI、离线聊天服务，或跨设备云端对话库。
- 保证后来的全部界面、上传、共享草稿、下载与跨 Agent 接续都经过真实手机验收。

本机网页服务和访问校验仍是 Mac 应用需要的基础。撤下一项预览集成，不等于删除这层基础，也不负责卸载用户另行安装的软件。

[一封旧信 · 2026 年 11 月 3 日](old-letter.md)——从应用“关于”页读取的同一份随包来源直接导出中文原文。
