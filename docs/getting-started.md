# Getting started with White Rabbit

[English](getting-started.md) · [简体中文](getting-started.zh-CN.md) · [Home](../README.md)

You can download and install the source today. There is no `.app`, `.dmg`, or double-click installer yet. If you do not use the command line, ask an AI assistant that can work on your Mac to help with setup and checks.

## What you need

- **A Mac.** Windows is unsupported. Other POSIX systems require a manual foreground setup outside this guide.
- **Python 3.11 or newer.** Ask the installing assistant to check it and help install it if needed. For manual installation, use the [Python macOS downloads](https://www.python.org/downloads/macos/), then check its version and your agent's dependencies.
- **One working native AI tool:** Codex, Hermes, Pi, Claude Code, or DSH. It must already be installed locally and have working login or model-provider configuration. White Rabbit does not include an AI account or model.

An agent here is an AI tool that can run tasks on the computer. A browser account alone may not provide a local connection. The Rabbit specialist and extra memory services can wait until basic chat works.

## Ask your assistant to install it

Give this request to an assistant that can read local files and run commands. A web-only chat cannot perform the local installation.

> Install White Rabbit on this Mac: https://github.com/icecinz/white-rabbit . Read AGENTS.md and docs/install-agent.md. Check Python and connect just one AI tool I already use, keeping its account, model, and provider settings. Let me handle login and approvals locally. Open the installed page, verify a real reply, reload, and continue the same conversation. Tell me how to open it later, where data lives, and what remains unconfigured. Create a desktop alias to the installed launcher. Do not stop at the fictional demo.

Start with one real reply and a successful follow-up after reloading. If you do not have a working agent yet, install and configure one first; downloading White Rabbit does not set that up automatically.

## Download the files yourself

[Download the source ZIP](https://github.com/icecinz/white-rabbit/archive/refs/heads/main.zip), or visit the [repository](https://github.com/icecinz/white-rabbit) and choose **Code → Download ZIP**.

Double-click the ZIP to extract it, then move the folder somewhere you will keep it, such as Documents. It is usually called `white-rabbit-main` and contains `README.md`, `demo.py`, and `原型`.

Tell your installing assistant where this folder is. Extraction only gives you the source; configuration and installation still follow. Keep the folder for documentation and updates. Keep connection files and secrets outside it.

For command-line setup, follow the [installation checklist](install-agent.md) and [connection examples](native-agents.md#persistent-installation). Downloading a ZIP does not require Git.

## Open it next time

Double-click the desktop alias if your assistant created one. To find the original launcher:

1. Open **Finder** and press **Command + Shift + G**.
2. Enter `~/.imcodes/one-ai` and press Return.
3. Double-click **打开小白兔.command**. It starts the installed service and opens its local page.

You can visit <http://127.0.0.1:17860/> directly while the service is already running. The legacy `.imcodes` directory name does not require installing IM.codes. White Rabbit's program and data live there; each agent keeps its own original files.

## Check that real chat works

Select your configured agent and start a new conversation:

1. Say “Remember code paperboat7318; briefly confirm without using tools.” Wait for a real reply.
2. Reload, reopen the same conversation, and check that the exchange remains.
3. Ask “What was the code?” and check the continuing answer.
4. Close the page and reopen using the saved launcher, without rebuilding a command.

Login startup needs a separate check after actually logging out of and back into your Mac. A successful page reload does not verify login startup or a service restart.

## If you get stuck

| What happened | Next step |
| --- | --- |
| Python is missing or too old | Ask the assistant to select or install a supported, stable interpreter and use its absolute path |
| The page opens but cannot send | Check the real-agent connection and whether the original tool can log in and reply |
| Replies contain `【模拟回答】` (simulated reply) | This is the local rule-based demo; ask for real-agent configuration using the checklist |
| The port is occupied | Identify any existing White Rabbit service first; do not stop an unknown service or delete data |
| Login or approval is pending | Complete it locally; do not send keys, verification codes, or credential files in chat |
| Images do not work | Check Pillow in the service Python and the chosen agent's image support |

When reporting a problem, include your macOS version, chosen agent, setup step, and error text. Remove secrets, private conversations, and personal paths first.
