# Connect a native agent

[English](native-agents.md) · [简体中文](native-agents.zh-CN.md) · [Home](../README.md)

Start with **one agent you already use**. White Rabbit's native routes call the agent's own CLI, SDK, or protocol; they do not include a replacement agent or a model account. This snapshot targets macOS/POSIX and Python 3.11+.

These routes create or recover supported native sessions managed by White Rabbit. They do not automatically attach to a task already running in Codex desktop, WorkBuddy, or any other window. Reading history, recovering a saved session, and controlling a live external executor are separate capabilities.

## Demo and real mode are different

`python3 -B demo.py` uses temporary invented data and rule-based replies. The commands below start the real application: it can index local native histories, store application records, launch agents, and use their current credentials and model settings. A new White Rabbit data directory does not disable native-history discovery. Online agents may transmit the material you send to their providers; normal native startup can also create logs, caches, or background work.

Keep credentials in the original agent's configuration. Do not put them in startup arguments, this repository, chat messages, or project documents.

## Persistent installation

When Codex, Hermes, or DSH is installing for a user, follow [the installation checklist](install-agent.md) to complete one real route first. This JSON contains local paths, not accounts or keys. Put **one of these groups** in `$HOME/.white-rabbit-connections.json`, outside the checkout, replacing every `/absolute/path/to/...` with a verified absolute path. JSON does not expand `$HOME` or `~`.

Codex:

```json
{"schema_version":1,"connections":{"codex":{"cli":"/absolute/path/to/codex"}}}
```

Hermes:

```json
{"schema_version":1,"connections":{"hermes":{"node":"/absolute/path/to/node","python":"/absolute/path/to/hermes-agent/venv/bin/python","root":"/absolute/path/to/hermes-agent"}}}
```

DeepSeek Harness:

```json
{"schema_version":1,"connections":{"dsh":{"node":"/absolute/path/to/node","cli":"/absolute/path/to/deepseek-harness/lib/bin.js","root":"/absolute/path/to/deepseek-harness"}}}
```

From the repository root, preflight and install:

```sh
python3 -B 原型/install_local.py check --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
python3 -B 原型/install_local.py install --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
```

Preflight checks basic requirements and paths without starting an agent or calling a model. Authentication, SDK/CLI compatibility, and real conversation still need verification. The installer keeps a private program copy and data under `$HOME/.imcodes/one-ai` and registers a per-user login service; IM.codes is not required. Later, double-click `打开小白兔.command` there or run `python3 -B "$HOME/.imcodes/one-ai/manage.py" open`.

For an existing installation, omit `--native-config` on update to retain saved connections. Supplying JSON replaces the complete `connections` set; merge existing entries before adding an agent. Get one real conversation working before adding memory, the specialist, or more agents.

## Foreground debugging command

Run from the repository root. Use a separate development data directory and port, apart from the installed service and the agent's original transcripts:

```sh
python3 -B 原型/server.py \
  --data-dir "$HOME/.white-rabbit-dev" \
  --port 17861
```

Append **one of the argument groups below to the same command**. Replace all `/absolute/path/to/...` examples with paths on your own machine. Open <http://127.0.0.1:17861/>, choose the configured native agent/profile, and send a short request deliberately. The base command alone does not enable these native connections.

This mode runs in the current terminal for development or troubleshooting; it does not provide login startup. For the complete current argument list, run `python3 -B 原型/server.py --help`.

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

It keeps a separate fixed native conversation and calls the same supported application-management interfaces as the UI. Its current role requests a fixed model/reasoning configuration, so availability also depends on the native account. The optional work-document mode requires separately supplied project role documents and is not needed for this starting path. See [what the specialist can do](specialist.md).

The manual **Check and update** report has a separate analysis dependency: an available, already configured Claude text adapter with tools and native session persistence disabled. It analyzes only the four program-supplied metadata facts, records its requested/connection-reported model source, and does not acquire the Codex specialist's management tools. If this optional route is missing, facts can still be saved while the explanation remains incomplete. Refreshing a saved report does not trigger another analysis or change settings. See the [report pipeline](specialist.md#manual-status-report); configuring this route is not required for the first real-agent installation.

## What to expect

Text conversations, compatible full-text materials, and session continuation have implementations for the five routes. Recovery depends on the original session, working directory, and native files remaining available.

| Route | Saved next-turn settings | Material / result boundary |
| --- | --- | --- |
| Codex | Model and supported reasoning level | Full text; original images only for supported configurations |
| Hermes | Model and supported reasoning level | Full text and compatible images; exact delegated results can depend on the current connection cache |
| Pi | Model and supported reasoning level | Full text; image use needs a verified vision-capable model and route; delegated results are current-connection scoped |
| Claude Code | Model selection; reasoning retains native settings | Full text; images are not enabled here; eligible final SDK results can be read from bounded local receipts after disconnect |
| DeepSeek Harness | Local model override remains unconnected because the existing native setter may change global defaults | Full text and next-turn read-only memory selection; images are not enabled here |

The five routes can independently select supported read-only memory for the next turn. Hindsight, Hermes document memory, and the optional Mem0 bridge have different configuration and read/write capabilities. A selected memory profile does not erase context already in the native session or disable the agent's own memory system.

Native target input/result routes are not equivalent to every complete specialist delegation path being validated. Keep the original request/session/turn identity when reading a result; a recent answer is not an exact result. Missing or cold connection state must not trigger an automatic resend.

Claude's local operation ledger can now contain **final-answer text**, not only status metadata. It stores only successful, non-truncated, top-level SDK Results with UTF-8 text at most 128 KiB, together with SHA-256 and the original chat/client/session/epoch identity. Matching reads after disconnect or rebuild are labeled `stored_sdk_result`; they do not assert that the original connection is currently alive. Older receipts without stored text are not backfilled, and failures, subtasks, truncated/oversized results, corruption, or identity mismatch cannot be returned as a successful stored answer. Pi/Hermes still require their current-connection result caches. This limited saved body does not replace the agent's original full transcript or restore its internal state.

Agents preserve their own transcripts, tools, permissions, and configurations. A selected text/material handoff does not migrate complete internal state. Offline tests, a successful connection, and a finished real task are different evidence; this release does not promise every tool, extension, approval shape, or recovery case.

The server binds to loopback. External mobile access currently depends on your separately configured IM.codes connection to the same online Mac. Do not treat it as a public hosted service or cross-computer scheduler.

## Observe current work and HAPI (optional)

**Management → Agent → Current work** shows metadata for native connections White Rabbit already owns. Opening an item returns to the same conversation. An empty list does not prove other applications or background tools are idle.

If you already run a local HAPI Hub, the included connector can manually bind its read-only work-discovery route:

```sh
python3 -B 工具/hapi_work_connect.py --origin http://127.0.0.1:3006
```

Replace the port with your Hub's port. The tool accepts only a literal loopback HTTP origin, asks for the original HAPI access token without echoing it, and exchanges it for a JWT kept in White Rabbit's memory. Use `--rabbit-port` if the White Rabbit server uses another port. No HAPI process or agent is started by the connector.

Click the manual refresh action under **Management → Agent** to fetch Hub-reported summaries; opening an item goes to HAPI's original page. The statuses are HAPI reports, not proof of White Rabbit control or full native state. Sending, approvals, and stopping remain in HAPI. Service restart or expired authentication needs a fresh manual connection; `--clear` removes only White Rabbit's in-memory binding.
