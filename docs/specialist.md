# Meet the Rabbit specialist

[English](specialist.md) · [简体中文](specialist.zh-CN.md) · [Home](../README.md)

The Rabbit specialist is the application's management assistant: **one continuous application conversation, with Codex or DSH as its execution core**. Each core retains its own native session, role, and received-text position. Its job is to understand an instruction, inspect the relevant state, and use the owning module's supported interfaces.

This describes the **0.2.0-alpha.2 early source snapshot**. Configure the management prerequisites and the selected native connection, preserving its original account, provider, tools, and approvals.

It is responsibility 7, reusing the runtime foundation from responsibility 3 and the UI from responsibility 5. Data, settings, and validation remain in their modules. There is no second settings database or unrestricted management bypass.

## A small conversation entry

Type an instruction in the upper-left specialist input without leaving the page you are viewing. Click the specialist's title to open its fixed history, operation records, and any native confirmation. Replies default to brief explanations; a guide alongside the conversation shows supported options and example phrases.

When a supported conversation is open, the application can attach that target's type, workspace, and exact ID to the management instruction, without attaching the chat body. If the target changes while preparing the request, the instruction is retained rather than silently applied to a different chat.

The application retains the same management history across refreshes. Codex and DSH keep separate native identities and model/provider settings, rather than taking ordinary AI-profile selection as a request to replace the specialist. Native login, tools, and approval rules remain with the selected core.

## Switch the specialist core

Use **Settings → Rabbit specialist → Specialist core** to choose an available Codex or DSH connection. Switching does not create another visible management chat. Existing Codex binding, history, role documents, and user edits are retained.

The next turn supplies the chosen core with current target metadata and conversation prose it has not received. Native tools, approvals, and execution receipts stay with their original holder; old tool calls are records, not commands to replay. Incomplete/nontext carryover or text beyond the supported capacity blocks the operation instead of silently dropping context.

A successful switch writes exactly one boundary in the application history. Running, pending-approval, or unknown requests block switching; failure to prepare the new connection retains the previous core, history, and draft. Uncertain persistence keeps the original operation ID for checking. Native approval and failure behavior still depend on the selected agent; a shared coordinator does not make every tool or recovery path interchangeable.

A supported management pattern is: list profiles, identify the exact target, rename it using its ID and current version, then reread the saved result. Both cores use the same bounded management MCP; this does not imply unrestricted configuration access.

**DSH specialist shared-memory selection is not connected**; ordinary DSH next-turn read-only memory is a separate supported route. Hermes is a future specialist option, not a third available core. Core switching retains application prose and native identities, rather than promising full model/tool equivalence or restart recovery.

## What you can ask

The application-interface catalog describes supported operations. The selected core, tool connection, exact target, and native permissions determine what can run.

| Example | Supported scope |
| --- | --- |
| “Use the dark theme.” | Shared, versioned application settings |
| “Show replies as raw text.” | An infrequent display setting listed in the readable catalog; stored content is unchanged |
| “Create a profile called Project work.” | Existing AI-profile create/edit interfaces, including starting a new profile from an existing one; candidates and saved parameters still need native validation |
| “Change only this chat's next configuration.” | Codex/Pi/Hermes saved next-turn settings; Claude model selection with native reasoning settings retained; DSH's local model setter remains unconnected |
| “Use project memory read-only next turn.” | Next-turn read-only memory selection for the five native routes; it does not erase the session's existing context |
| “Find Project discussion; don't send a message.” | Exact conversation/history discovery, preserving source identity |
| “Put this organizing document in the existing Work group.” | Supported Markdown/reference grouping; moving an existing file retains its name and contents within the supported local root |
| “Check and update.” | Manual program facts → tool-free AI analysis → saved report; not an automatic computer-wide scan or repair |
| “Give this task to the existing Codex conversation and retrieve its answer.” | A bounded native delegation path tied to the original target and request ID |

Memory profile parameters, document-memory edits, connection checks, and actual retrieval/saving are distinct operations. A saved profile or a healthy connection does not prove memory content was retrieved or written. Checks need not read private memory text.

## Manual status report

Use **Check and update** beside the specialist guide when you want a new report. It is not a scheduled inspection. The current pipeline keeps three things separate:

| Step | What happens | Evidence boundary |
| --- | --- | --- |
| Program facts | Read saved AI configuration, native model catalogs, the specialist's tool-connection metadata, and the explicitly selected memory's connection metadata | Each fact retains its source, observation/read times, identity, and evidence digest. When no memory is selected, that fact is skipped; unavailable evidence remains unverified. |
| AI analysis | Send those four frozen facts to one available tool-free text route for structured judgments and short explanations | Each item must refer to the original evidence ID and digest. An unknown fact cannot be upgraded to verified; a disabled feature is not automatically a fault. |
| Saved report | Validate the response, make one bounded second metadata observation, and save facts, judgments, explanation, and analysis source locally | Changed evidence is marked inapplicable; unverified or changed evidence yields a partial report, while failed analysis remains incomplete. The report does not change configurations, create a target chat, execute tools, recall private memory, or send test tasks. |

The selected **Codex/DSH management core** is separate from status analysis, which currently uses the application's **existing Claude-configured text adapter**. The current tool-connection fact still observes the Codex-side specialist holder; it is not a complete DSH tool-health check. The disposable analysis route has tools and native conversation persistence disabled, and receives only the supplied metadata facts. It can use the provider/model already configured for that route; it is not proof of an Anthropic backend or a particular underlying model. The report distinguishes a requested model label from a connection-reported label. If the route is unavailable or analysis fails, the collected program facts remain available and the explanation is marked incomplete; there is no automatic fallback or retry. Analysis may use the online service configured for that connection. Report labels and generated notes currently remain mainly Chinese.

Refreshing or reopening the panel reads the saved report under its original check ID. Lightweight identity/version comparisons can mark an earlier judgment as no longer applicable; ordinary reads do not repeat the tool/memory RPC checks or call the model again. Browser recovery preserves a pending check instead of silently creating a second one. A service generation change leaves an unfinished check unconfirmed rather than resubmitting it. The report is an observation with sources and times, not continuous proof that an account, model, tool, or service is currently usable.

## How a change stays scoped

1. Find the exact target and read the current supported values/version.
2. Reuse an appropriate saved profile, or create a dedicated one when only one chat should change.
3. Submit the bounded change through the same business interface the UI uses.
4. Read the outcome. On conflict reread; on uncertainty keep the original request rather than sending another one.

An instruction to change one conversation must not edit a shared profile used by other conversations. A next-turn selection does not rewrite an already sent request or its receipt.

The profile UI says **Start a new profile from this profile**, rather than implying that a native conversation is duplicated. The previous **Set as default for new chats** action is no longer shown; select the intended saved profile in the composer. Existing saved choices and records are preserved.

## Codex draft and conversation settings

Codex supports a local model/reasoning adjustment for **one existing conversation** or the **unsent new-conversation draft**. Human controls and specialist management use the same save rules. Repeated edits update one internal setting; they do not create more named profiles or alter the global default. Selecting a named profile exits the local adjustment. Other agents retain saved-profile selection; their local fine-tuning is not connected in this release.

For an unsent draft, the specialist receives its ID and configuration metadata, not the unsent message or files. Its suggestion remains pending until the **originating browser page** saves against the expected version and returns an application receipt. Submission alone is not completion. Existing text, files, and memory choice remain; first Send creates the conversation and binds the setting to it. A changed configuration or a send already in progress prevents an older suggestion from overwriting the new choice.

Navigation or refresh retains the original target. A new tab does not silently take over a pending suggestion; use the explicit review/apply action when needed. An unknown save/application result is checked under the original ID, not proposed or applied again. Model-catalog evidence must be available for supported fine-tuning; refreshing the UI does not secretly start an agent to fill a missing catalog.

Some infrequent settings have no individual graphical switch. Their readable catalog is still available to the person; the specialist or another supported program interface performs the change. Purple describes this implemented access path, not approval to act. [Color and settings design](design.md)

## Delegation and its current limits

Native target input/result routes exist for all five agents. A complete delegation also needs compatible material, an exact target/request identity, and an available result path; an input interface alone is not a promise that every core/tool combination can complete it.

Pi/Hermes save eligible complete results after the first exact successful retrieval, allowing later reads by the original task ID after service reconstruction. Before that first read, a broken connection may leave the answer unavailable. A saved result is explicitly previous evidence, not a current-owner observation.

Claude can retain a bounded final-answer body in its **local operation ledger**: a successful, non-truncated, top-level SDK final Result of at most 128 KiB, with its original request identity and SHA-256. A matching stored result can be read after the connection owner is closed or rebuilt. It is labeled `stored_sdk_result`, not presented as evidence of a live connection. Older receipts without a saved body, failures, subtasks, oversized/truncated results, corruption, or identity mismatches do not produce a successful saved answer. This is not unlimited result storage or recovery of the complete native session state.

The target's complete final answer must be linked to the original request. An accepted submission, progress fragment, or most recent answer is not a substitute. When the required exact cache or stored evidence is unavailable, return unavailable; do not guess or resend under a new ID.

Native permissions are still handled by the person. A verified delegation does not mean unattended approvals, desktop-application takeover, or control of all devices.

## What remains outside this assistant

- Login, personal authorization, and native approval decisions.
- Direct takeover of live Codex desktop or WorkBuddy work.
- GUI-only application operation and the future AI/mixed interface modes.
- Cross-computer scheduling and remote wake-up.
- An automatic global model switcher or independent settings/execution system.

The specialist can explain unconnected areas and prepare a supported next step. It should not label an installed program, readable tool catalog, or completed plan as an operational integration.
