# Checklist for an installing agent

[English](install-agent.md) · [简体中文](install-agent.zh-CN.md) · [Home](../README.md)

When asked to “install and configure White Rabbit,” the goal is **a repeatable opening method and a continuing conversation with at least one real agent**. Running `demo.py` or loading a page is not completion.

## 1. Inspect the existing environment

- Check the OS and Python. Python 3.11+ is required; use the macOS route for persistent local installation. Windows is not supported.
- Check for an existing White Rabbit installation and use of port 17860. Identify an existing service before changing it; do not overwrite its data or interrupt work.
- Locate one agent the person already uses, including its exact CLI, runtime, and data home. Reuse it; do not require all five agents.
- Use the original agent's login-status interface without displaying keys or whole credential files. Ask only for the missing choice when environments, accounts, or data homes are ambiguous.

Choose an absolute path to a stable Python 3.11 or newer. Do not use a temporary virtual environment that may be deleted: the login service will keep using this interpreter. Check candidates with `command -v python3.13`, `command -v python3.12`, `command -v python3.11`, and `command -v python3`, then run `"/absolute/path/to/python" --version` for each. The system `python3` can be older than 3.11; install a supported version if needed. Record the path and set it for this terminal (replace the example):

```sh
RABBIT_PYTHON="/absolute/path/to/python3.11"
"$RABBIT_PYTHON" --version
```

Image support also needs Pillow in the service Python. Preflight reports `service_python.path` without executing it to check its version or image dependencies. Run that interpreter with `--version`, then `-c "from PIL import Image; print(Image.__version__)"` to verify both. If missing, install Pillow in the selected stable environment and check again; report image support as unavailable if only setting up text.

## 2. Choose one real route

Use [native-agent setup](native-agents.md) to confirm this agent's paths and version requirements. Preserve its effective provider, model, tools, and permissions. For example, keep Claude Code's existing CC Switch configuration instead of replacing it with another default.

The persistent connection JSON supports `home` only for Hermes and DSH. The login service does not automatically save `CODEX_HOME` or `CLAUDE_CONFIG_DIR`; setting them in a terminal does not establish that the login service uses the same home. If the original account and routing environment cannot be confirmed, pause that route or use another already verified existing agent. Do not fall back to defaults, migrate credentials, or copy secrets.

If login, API access, or dependencies are missing, name the missing requirement and let the user complete necessary authentication locally. Do not ask for a key in chat. Mark unverified models or connections as needing setup.

## 3. Install and leave an opening method

From the repository root, run the read-only preflight first:

```sh
"$RABBIT_PYTHON" -B 原型/install_local.py check --source-dir "$PWD/原型"
```

Use [the native-connection JSON examples](native-agents.md#persistent-installation) to write verified absolute paths into `$HOME/.white-rabbit-connections.json`, outside the checkout. You prepare this file; the program does not discover the paths automatically. Do not put keys in it. Then run:

```sh
"$RABBIT_PYTHON" -B 原型/install_local.py check --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
"$RABBIT_PYTHON" -B 原型/install_local.py install --source-dir "$PWD/原型" --native-config "$HOME/.white-rabbit-connections.json"
```

`check` verifies basic requirements, paths, and port 17860: `service` is `free`, `current_installation`, `other`, or `unknown`. Investigate another service or an unknown state first. This is one read-only observation; the installer still checks active work and maintenance state. It does not verify authentication, SDK versions, or real model replies. First installation needs at least one ordinary agent; an empty connection set or specialist alone is not configured chat.

The preflight's `visible_commands` can help locate commands on the current PATH. Verify each actual installation and the original agent's status; this is not a complete scan or proof that an agent works. Do not read or print credentials.

The installer keeps a private program copy and data under `$HOME/.imcodes/one-ai` and registers a per-user login service. The directory name does not require IM.codes. Later, double-click `打开小白兔.command` there; no terminal variable needs to persist. Alternatively, use the absolute Python path chosen at installation (replace the example):

```sh
"/absolute/path/to/python3.11" -B "$HOME/.imcodes/one-ai/manage.py" open
```

On update, omitting `--native-config` preserves saved connections. An explicitly supplied `connections` object replaces the whole set; merge existing entries before adding another agent instead of supplying only the new one.

For a data-format upgrade, make a consistent SQLite backup of the affected ledger while no work is active and retain it with the old program version. Do not copy only a running database's `.sqlite` file and omit its WAL. The installer's recovery snapshot includes the program, launch configuration, and control scripts, but no data databases. The Hermes/Pi result-retention update upgrades `hermes-native.sqlite`/`pi-native.sqlite` from v1 to v2; the old program cannot read v2 directly. `rollback` restores only the program. Before restoring a pre-upgrade ledger, preserve the current data and explain that newer records would be lost; let the user decide rather than overwriting automatically.

Keep the source checkout for documentation and updates. An update retains the saved service Python even when the installer runs under another interpreter. If the saved path is unavailable, select a new stable Python 3.11+ and pass `--python "$RABBIT_PYTHON"` to both `check` and `install`; also verify its Pillow installation. Interpreter changes are never inferred automatically.

Keep White Rabbit's data separate from the agent's original files and record selected environments with absolute paths. Check that the page, launcher, and data directory exist and that the opening method still works after this terminal closes.

Configure only the chosen ordinary agent first. Memory, an explicitly configured Rabbit specialist core, and additional agents can follow after real chat works. A person without Codex can start with their existing Hermes or DSH; ordinary DSH access does not automatically configure its specialist role. State which specialist core, if any, has actually been configured and verified. Phone-preview integration is historical, not an installation option.

## 4. Verify through the real interface

A fresh configuration prefers an explicitly enabled native entry whose default parameters can be checked. Select one explicitly when none qualifies. Preserve an existing user's saved default; Codex is not an installation prerequisite.

1. Open the installed application, select the configured real native agent, and create a new acceptance conversation.
2. Send a short request with a random code, such as “This acceptance code is paperboat7318. Briefly confirm it; do not use tools.” Confirm an actual reply from the original agent. Record its reported model information; do not guess an unreported model or reasoning level.
3. Reload the page and open the same conversation. Confirm the exchange remains, then ask “What was the acceptance code?” Check the continuing answer in the same native session.
4. Reopen using the method you will give the user, without reconstructing a long command. If the service itself was not restarted, report page-reload verification rather than claiming restart recovery.

If a reopened Hermes conversation shows “Restore original session,” explain that native recovery may also continue unfinished work. Let the user explicitly restore it, then check the existing content; do not resend a question to make history appear.

When a send result is unknown, check its original identifier rather than resending to find out. Login, authorization, and native approvals belong to the user. If browser access or a required permission is unavailable, explicitly leave that acceptance step for them.

## 5. Hand over a short result

Give only these five things: **how to open it, where data lives, the configured agent/model, the real acceptance result, and anything still missing**.

Distinguish a working demo, an installed application, and verified real chat. Environment checks do not substitute for a real reply, and a supported-agent list is not a list of agents configured on this machine.
