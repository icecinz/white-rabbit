# Connect a native agent

[English](native-agents.md) · [简体中文](native-agents.zh-CN.md) · [Home](../README.md)

Start with **one agent you already use**. White Rabbit's native routes call the agent's own CLI, SDK, or protocol; they do not include a replacement agent or a model account. This snapshot targets macOS/POSIX and Python 3.11+.

## Demo and real mode are different

`python3 -B demo.py` uses temporary invented data and rule-based replies. The commands below start the real application: it can index local native histories, store application records, launch agents, and use their current credentials and model settings. A new White Rabbit data directory does not disable native-history discovery. Online agents may transmit the material you send to their providers; normal native startup can also create logs, caches, or background work.

Keep credentials in the original agent's configuration. Do not put them in startup arguments, this repository, chat messages, or project documents.

## Base command

Run from the repository root. Use a dedicated local data directory, not an agent's original transcript directory:

```sh
python3 -B 原型/server.py \
  --data-dir "$HOME/.white-rabbit" \
  --port 17860
```

Append **one of the argument groups below to the same command**. Replace all `/absolute/path/to/...` examples with paths on your own machine. Open <http://127.0.0.1:17860/>, choose the configured native agent/profile, and send a short request deliberately. The base command alone does not enable these native connections.

For the complete current argument list, run `python3 -B 原型/server.py --help`.

### Codex

For the basic native route, provide your installed, authenticated Codex CLI:

```sh
--native-codex-cli "/absolute/path/to/codex"
```

To attach White Rabbit's application-management MCP tools instead, use an existing Python environment with the Python `mcp` SDK:

```sh
--native-chat-mcp-python "/absolute/path/to/mcp-venv/bin/python"
```

That route finds Codex through the environment's `PATH`. The two Codex startup flags are mutually exclusive. The management extension changes the available application tools; it is separate from the basic native connection.

### Pi

Provide Node and the CLI script from your installed, configured Pi package:

```sh
--native-pi-node "/absolute/path/to/node" \
--native-pi-cli "/absolute/path/to/pi-coding-agent/dist/cli.js"
```

Both arguments are required together. The connection uses Pi's original RPC and session files. Node requirements follow your Pi installation; the model-capability helper also needs a Node release supporting `--permission`.

### Hermes

Provide a working upstream Hermes environment, including its server dependencies:

```sh
--native-hermes-node "/absolute/path/to/node" \
--native-hermes-python "/absolute/path/to/hermes-agent/venv/bin/python" \
--native-hermes-root "/absolute/path/to/hermes-agent"
```

These three arguments are required together. Use Hermes's own supported Python environment and current native login/configuration. `--native-hermes-home` can explicitly select a Hermes data directory. The optional `--native-hermes-session-settings` enables an additional version-bound settings check; it is off by default.

The generated gateway client is bundled under `原型/rabbit_runtime/hermes/vendor/hermes_gateway/`, together with its MIT notice and upstream fingerprint. Its TypeScript source and build script are included separately. You do not need to rebuild it for normal use; rebuilding checks the exact Node version recorded in `upstream.json`.

### Claude Code

Provide both your installed CLI and a Python environment containing the official Claude Agent SDK:

```sh
--native-claude-python "/absolute/path/to/claude-sdk-venv/bin/python" \
--native-claude-cli "/absolute/path/to/claude"
```

This snapshot's connection checks **Claude Agent SDK 0.2.152** and **Claude CLI 2.1.87** by default. Other versions require an explicit compatibility change, not just a renamed file.

Install this repository's matching `rabbit-runtime` package into that same SDK environment before connecting:

```sh
"/absolute/path/to/claude-sdk-venv/bin/python" -m pip install --no-deps ./原型/rabbit_runtime
```

This command installs the local runtime package; it does not install or update the SDK. The worker checks the installed distribution version and rejects mismatches. Agent installation, credentials, and model settings remain your responsibility; the first send does not silently install missing software.

### DeepSeek Harness

Provide Node, the installed native CLI entry, and the package root:

```sh
--native-dsh-node "/absolute/path/to/node" \
--native-dsh-cli "/absolute/path/to/deepseek-harness/lib/bin.js" \
--native-dsh-root "/absolute/path/to/deepseek-harness"
```

All three arguments are required together. This snapshot's default package-version check is **0.1.0-rc.6**. It uses the original profile, tools, and protocol. Optional `--native-dsh-home` and `--native-dsh-history-root` explicitly select data/history roots; they require the configured native route.

### Management assistant (optional)

The separate management assistant needs an existing Python environment with the `mcp` SDK and an authenticated Codex CLI:

```sh
--specialist-mcp-python "/absolute/path/to/mcp-venv/bin/python" \
--specialist-codex-cli "/absolute/path/to/codex"
```

It can call the supported application-management interfaces; it is not a general permission bypass or a device-control service. Its current role requests a fixed model/reasoning configuration, so availability also depends on the native account. The optional work-document mode requires separately supplied project role documents and is not needed for this starting path.

## What to expect

Text conversations, compatible full-text materials, and session continuation have implementations for the five routes. Recovery depends on the original session, working directory, and native files remaining available. Images are limited to selected Codex and Hermes configurations; Pi's local-image route is not fully validated, and Claude/DSH image inputs are not enabled here. Shared-memory behavior also varies; the DSH follow-up path has known failed validation records.

Agents preserve their own transcripts, tools, permissions, and configurations. A selected text/material handoff does not migrate complete internal state. Offline tests, a successful connection, and a finished real task are different evidence; this release does not promise every tool, extension, approval shape, or recovery case.

The server binds to loopback. External mobile access currently depends on your separately configured IM.codes connection to the same online Mac. Do not treat it as a public hosted service or cross-computer scheduler.
