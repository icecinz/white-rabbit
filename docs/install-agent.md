# Checklist for an installing agent

[English](install-agent.md) · [简体中文](install-agent.zh-CN.md) · [Home](../README.md)

When asked to “install and configure White Rabbit,” the goal is **a repeatable opening method and a continuing conversation with at least one real agent**. Running `demo.py` or loading a page is not completion.

## 1. Inspect the existing environment

- Check the OS and Python. Python 3.11+ is required; use the macOS route for persistent local installation. Windows is not supported.
- Check for an existing White Rabbit installation and use of port 17860. Identify an existing service before changing it; do not overwrite its data or interrupt work.
- Locate one agent the person already uses, including its exact CLI, runtime, and data home. Reuse it; do not require all five agents.
- Use the original agent's login-status interface without displaying keys or whole credential files. Ask only for the missing choice when environments, accounts, or data homes are ambiguous.

## 2. Choose one real route

Use [native-agent setup](native-agents.md) to confirm this agent's paths and version requirements. Preserve its effective provider, model, tools, and permissions. For example, keep Claude Code's existing CC Switch configuration instead of replacing it with another default.

If login, API access, or dependencies are missing, name the missing requirement and let the user complete necessary authentication locally. Do not ask for a key in chat. Mark unverified models or connections as needing setup.

## 3. Install and leave an opening method

From the repository root, run the read-only preflight first:

```sh
python3 -B 原型/install_local.py check --source-dir "$PWD/原型"
```

Use [the native-connection JSON examples](native-agents.md#persistent-installation) to write verified absolute paths into `$HOME/.white-rabbit-connections.json`, outside the checkout. You prepare this file; the program does not discover the paths automatically. Do not put keys in it. Then run:

```sh
python3 -B 原型/install_local.py check --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
python3 -B 原型/install_local.py install --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
```

`check` verifies basic requirements and paths, not authentication, SDK versions, or real model replies. First installation needs at least one ordinary agent; an empty connection set or specialist alone is not configured chat.

The installer keeps a private program copy and data under `$HOME/.imcodes/one-ai` and registers a per-user login service. The directory name does not require IM.codes. Later, double-click `打开小白兔.command` there, or run:

```sh
python3 -B "$HOME/.imcodes/one-ai/manage.py" open
```

On update, omitting `--native-config` preserves saved connections. An explicitly supplied `connections` object replaces the whole set; merge existing entries before adding another agent instead of supplying only the new one.

Keep White Rabbit's data separate from the agent's original files and record selected environments with absolute paths. Check that the page, launcher, and data directory exist and that the opening method still works after this terminal closes.

Configure only the chosen agent first. Memory, phone access, the Rabbit specialist, and additional agents can follow after real chat works. A person without Codex can start with their existing Hermes or DSH; report that the Codex specialist remains unconfigured.

## 4. Verify through the real interface

1. Open the installed application, select the configured real native agent, and create a new acceptance conversation.
2. Send a short request with a random code, such as “This acceptance code is paperboat7318. Briefly confirm it; do not use tools.” Confirm an actual reply from the original agent. Record its reported model information; do not guess an unreported model or reasoning level.
3. Reload the page and open the same conversation. Confirm the exchange remains, then ask “What was the acceptance code?” Check the continuing answer in the same native session.
4. Reopen using the method you will give the user, without reconstructing a long command. If the service itself was not restarted, report page-reload verification rather than claiming restart recovery.

When a send result is unknown, check its original identifier rather than resending to find out. Login, authorization, and native approvals belong to the user. If browser access or a required permission is unavailable, explicitly leave that acceptance step for them.

## 5. Hand over a short result

Give only these five things: **how to open it, where data lives, the configured agent/model, the real acceptance result, and anything still missing**.

Distinguish a working demo, an installed application, and verified real chat. Environment checks do not substitute for a real reply, and a supported-agent list is not a list of agents configured on this machine.
