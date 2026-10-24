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

The complete Codex, Hermes, Pi, Claude Code, and DeepSeek Harness programs are not
bundled. Optional packages such as Pillow, the MCP SDK, the Claude Agent SDK, and
Hindsight are also installed separately. Their licenses and service terms apply
independently. The fictional demo does not call any real Agent or model.

完整 Agent 程序和可选依赖不随本项目分发，需自行安装、登录或配置；各自的许可与服务条款
独立适用。演示只使用虚构数据和明确标记的模拟回复。
