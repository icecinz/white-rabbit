# Third-party notices / 第三方部件

White Rabbit includes these small third-party components. Their original licenses
and attribution must remain with redistributed copies.

小白兔随附下列小型第三方部件。再分发时请保留原许可证和署名。

| Component / 部件 | Included files / 随附文件 | License and source / 许可与来源 |
| --- | --- | --- |
| markdown-it | `原型/rabbit_ui/assets/markdown-it.min.js` and adjacent `markdown-it.LICENSE` | MIT; the complete license is preserved beside the bundled script. |
| Hermes shared gateway client | Original TypeScript: `原型/vendor/hermes_gateway/json-rpc-gateway.ts`; generated JavaScript, `LICENSE`, and `upstream.json`: `原型/rabbit_runtime/hermes/vendor/hermes_gateway/` | MIT; Copyright (c) 2025 Nous Research. Source: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), pinned commit `145c713f1691b44bc485ce0ce4818cdb0e9c396b`. Exact source and output fingerprints are preserved in `upstream.json`. |
| DeepSeek Harness history scanner | `原型/dsh_history_worker.mjs` | Frame scanning and path handling are adapted from `@deepseek-ai/dsh-session-persistence-jsonl` version `0.1.0-rc.6`; MIT, Copyright (c) 2026 DeepSeek. The file header preserves the source reference and complete original license. Other session helpers are loaded from the user's own `@deepseek-ai/dsh-session` installation. |

The Hermes conversion entry is `原型/build_hermes_gateway.mjs`. Rebuilding its
generated artifact requires the exact Node version recorded in the manifest
(`v24.19.0`) and the experimental built-in TypeScript conversion API. Normal use
consumes the checked-in JavaScript and requires no conversion step or additional
npm dependency for this gateway client. The historical relative paths recorded in
the unchanged upstream manifest are provenance records; the current builder uses
the source and runtime paths listed above.

Hermes 的转换入口为 `原型/build_hermes_gateway.mjs`。仅重新生成产物时需要清单记录的
准确 Node 版本和实验性内置转换 API；普通使用直接读取已随附的 JavaScript。

## White Rabbit adapters and separately installed services / 自有适配与独立安装的服务

These integration files belong to White Rabbit's adapter layer. The programs and
SDKs they connect to are separate installations; this repository does not vendor
those complete upstream programs or change their licenses.

下列文件是小白兔的适配层。所连接的程序和 SDK 由用户独立安装，本仓库不随附这些上游
程序的完整实现，也不改变其原有许可。

| Integration / 接入 | White Rabbit files / 小白兔文件 | External component and boundary / 外部部件与边界 |
| --- | --- | --- |
| HAPI work discovery | `原型/rabbit_runtime/hapi_work.py`, `原型/external_work.py`, and `工具/hapi_work_connect.py` | The adapter follows the HTTP contract of [tiann/hapi](https://github.com/tiann/hapi) `v0.30.7`: an explicitly supplied local Hub, a fixed session-list request, and a manual local authentication exchange. HAPI is an independently installed service governed by its upstream license. Its upstream program, web interface, account files, databases, and credentials are not bundled. White Rabbit's MIT license applies to its adapter code, not to HAPI itself. |
| Mem0 read-only recall | `原型/rabbit_memory/mem0_memory.py` and `原型/mem0_memory_worker.py` | The optional worker checks the separately installed `mem0ai==2.2.1` SDK. Mem0's SDK and its dependencies are not bundled; their licenses apply separately. Model, embedding, credential, and storage settings must be supplied explicitly by the user. No private configuration or memory database is distributed. |
| Pi current-connection result observation | `原型/rabbit_runtime/pi/result_tracking.mjs`, `tracked_rpc_bootstrap.mjs`, and `tracked_rpc_pin.json` | White Rabbit's observation layer targets the separately installed `@earendil-works/pi-coding-agent` `0.79.1`. The pin file records upstream file hashes, and the bootstrap uses small source-text match anchors to insert observation hooks into the loaded process. It does not bundle the complete Pi program, modify installed upstream files, or replace Pi's execution loop. The installed Pi package retains its own license. |

HAPI 接入只展示 Hub 报告的工作摘要；不会取得原生执行者身份，也不提供发送、审批、
停止或恢复。连接凭据只在明确绑定时由用户提供，不随源码分发。Mem0 当前仅提供只读
检索。Pi 的结果观察只针对固定版本与当前连接，不承诺跨重启的结果恢复或耐久去重。

The complete Codex, Hermes, Pi, Claude Code, and DeepSeek Harness programs are not
bundled. Optional packages such as Pillow, the MCP SDK, the Claude Agent SDK, and
Hindsight, as well as the Mem0 SDK and HAPI service described above, are installed
separately. Their licenses and service terms apply
independently. The fictional demo does not call any real Agent or model.

完整 Agent 程序和可选依赖不随本项目分发，需自行安装、登录或配置；各自的许可与服务条款
独立适用。演示只使用虚构数据和明确标记的模拟回复。
