# Meet the Rabbit specialist

[English](specialist.md) · [简体中文](specialist.zh-CN.md) · [Home](../README.md)

The Rabbit specialist is the application's management assistant: a **separate, fixed native Codex conversation** with its own role and work context. Its job is to understand an instruction, inspect the relevant state, and use the owning module's supported interfaces.

It is responsibility 7, reusing the runtime foundation from responsibility 3 and the UI from responsibility 5. Data, settings, and validation remain in their modules. There is no second settings database or unrestricted management bypass.

## A small conversation entry

Type an instruction in the upper-left specialist input without leaving the page you are viewing. Click the specialist's title to open its fixed history, operation records, and any native confirmation. Replies default to brief explanations; a guide alongside the conversation shows supported options and example phrases.

When a supported conversation is open, the application can attach that target's type, workspace, and exact ID to the management instruction, without attaching the chat body. If the target changes while preparing the request, the instruction is retained rather than silently applied to a different chat.

The specialist keeps one native thread across refreshes/reconnection. Its current connection uses a fixed model/reasoning request policy, separate from ordinary AI profiles. Automatic model switching and a fleet of management agents are not implemented.

## What you can ask

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

The management specialist remains the **fixed native Codex conversation**. Status analysis currently uses the application's **existing Claude-configured text adapter**, not a second tool-enabled management specialist. That disposable text route has tools and native conversation persistence disabled, and receives only the supplied metadata facts. It can use the provider/model already configured for that route; it is not proof of an Anthropic backend or a particular underlying model. The report distinguishes a requested model label from a connection-reported label. If the route is unavailable or analysis fails, the collected program facts remain available and the explanation is marked incomplete; there is no automatic fallback or retry. Analysis may use the online service configured for that connection. Report labels and generated notes currently remain mainly Chinese.

Refreshing or reopening the panel reads the saved report under its original check ID. Lightweight identity/version comparisons can mark an earlier judgment as no longer applicable; ordinary reads do not repeat the tool/memory RPC checks or call the model again. Browser recovery preserves a pending check instead of silently creating a second one. A service generation change leaves an unfinished check unconfirmed rather than resubmitting it. The report is an observation with sources and times, not continuous proof that an account, model, tool, or service is currently usable.

## How a change stays scoped

1. Find the exact target and read the current supported values/version.
2. Reuse an appropriate saved profile, or create a dedicated one when only one chat should change.
3. Submit the bounded change through the same business interface the UI uses.
4. Read the outcome. On conflict reread; on uncertainty keep the original request rather than sending another one.

An instruction to change one conversation must not edit a shared profile used by other conversations. A next-turn selection does not rewrite an already sent request or its receipt.

The profile UI says **Start a new profile from this profile**, rather than implying that a native conversation is duplicated. The previous **Set as default for new chats** action is no longer shown; select the intended saved profile in the composer. Existing saved choices and records are preserved.

Some infrequent settings have no individual graphical switch. Their readable catalog is still available to the person; the specialist or another supported program interface performs the change. Purple describes this implemented access path, not approval to act. [Color and settings design](design.md)

## Delegation and its current limits

Native target input/result routes exist for all five agents. End-to-end specialist delegation has narrower evidence: Codex has a verified complete route, and a Claude material handoff has corroborated input and exact result retrieval. That does not establish every target, tool, or interruption case. Pi/Hermes exact results remain current-connection scoped.

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
