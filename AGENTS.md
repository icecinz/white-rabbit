# Installing White Rabbit for a user

For “install and configure White Rabbit,” follow [the installation checklist](docs/install-agent.md) ([中文](docs/install-agent.zh-CN.md)). Start with real-agent mode. `demo.py` is an optional temporary, fictional demo; it is not an installed AI assistant.

- Inspect the OS, Python, existing installation, and the user's installed agents first. Use one agent they already use; do not install all five.
- Keep the original agent's credentials, provider routing, model settings, tools, and approvals. Preserve CC Switch and custom agent homes. Ask about ambiguous paths or accounts rather than choosing a different configuration.
- Check login availability without displaying credential files or copying secrets into chat, command arguments, this checkout, or a report. Missing login or API access is unfinished setup for the user to complete locally.
- Establish a persistent data directory and a repeatable opening method. Use the installer's maintenance guards; do not interrupt active work, answer native approvals, or delete user data to get past an installation error.
- Use `原型/install_local.py check` before `install`. Prepare the finite native JSON from verified paths; it contains no keys. `check` does not verify authentication or real replies. On updates, omit `--native-config` to preserve saved connections, or merge the complete intended set before explicitly replacing it.
- Verify a real native reply, reload, and continue that same conversation. A running HTTP server, indexed history, model catalog, offline tests, or simulated reply does not prove real chat works.
- Deliver the opening method, data location, chosen agent/model, verified result, and remaining work. Distinguish unknown values from confirmed ones. Do not claim complete installation when only the demo or page works.

The application currently targets macOS/POSIX and Python 3.11+. Windows installation is not supported. Optional memory, mobile access, and the Codex specialist can follow after the first real conversation succeeds.

For development, use [CONTRIBUTING.md](CONTRIBUTING.md). Run checks relevant to the change; live scripts and model calls are not automatic offline tests. Keep publication limited to the public file manifest, without local credentials, chat histories, runtime databases, or evaluation artifacts.
