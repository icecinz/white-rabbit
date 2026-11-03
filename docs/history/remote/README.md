# The remote-access experiment

[English](README.md) · [简体中文](README.zh-CN.md) · [Back to White Rabbit](../../../README.md)

White Rabbit once tried a small window onto an online Mac: open its local web application from an iPhone through IM.codes, send a message, and return to the same saved conversation. That window worked in the specific checks below. It did not become an AI that could take over several computers or wake an offline one.

Blueprint 2.0 refocuses the product on using and managing AI in one place. The 0.2.0-alpha.1 source snapshot removes phone-preview entries and IM.codes-specific compatibility. This page preserves the history, rather than providing a remote-access installation guide.

## What actually worked

These are summaries of contemporaneous device-test notes, reviewed for this history. The private notes, accounts, conversations, images, and access links are not included. Phone observations and local storage checks support different parts of each result.

| When | What was checked | What that establishes |
| --- | --- | --- |
| October 10–11, 2026 | An iPhone opened a separate, fictional preview; the page became readable, a test message received a rule-based reply, and refresh retained it | A working phone preview and transport path. It did not use a real model |
| October 11, 2026 | The iPhone opened the Mac's normal service through IM.codes and sent a text message to Hermes. The user confirmed a reply and persistence after refresh; local metadata confirmed a completed saved turn | One real phone text-chat path to an online Mac, using the existing account |
| October 12, 2026 | The user confirmed adding and viewing an image, sending it to Codex, receiving a reply, and retaining image and messages after refresh. Local metadata confirmed the completed request and attached image | The basic phone image flow. The reply's visual accuracy was not independently assessed |

The real phone checks used a pre-release White Rabbit and **IM.codes 2026.8.3638**, with version-specific local relay fixes. They were not a fresh installation or an independent account test. The Mac hosted the service and stored the work; sleeping or shutting it down made that route unavailable.

## What the published code preserves

The selected compatibility implementation was subsequently included in the public alpha snapshots. These fixed links can be inspected without importing any private archive:

| Selected result | Published source at alpha.4 |
| --- | --- |
| A self-contained first page, bundling required styles, scripts, and images so preview loading did not depend on later static-resource cookies | [UI resource assembly](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/原型/rabbit_ui/resources.py#L96-L177) |
| Separate handling of the outer preview's nonce and White Rabbit's own request nonce | [Preview request adaptation](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/原型/rabbit_ui/assets/app.js#L85-L161) |
| Bounded handling of chunked HTTP request bodies used by the relay | [Request-body reader](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/原型/server.py#L4743-L4788) |
| An embedded-preview warning for text export, directing the person to an independent tab | [Export boundary](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/原型/rabbit_ui/assets/app.js#L2777-L2780) |

Relevant public snapshots are [alpha.1](https://github.com/icecinz/white-rabbit/commit/b716664a4549ebe67ccbcd16841892c5d1aeb38a) and [alpha.4](https://github.com/icecinz/white-rabbit/commit/9e88305e17c4eca95550d86a6cc5fc4298b20296). The [alpha.1 scope](https://github.com/icecinz/white-rabbit/blob/b716664a4549ebe67ccbcd16841892c5d1aeb38a/README.md#L58) already excluded cross-computer routing and wake-up. The [alpha.4 scope](https://github.com/icecinz/white-rabbit/blob/9e88305e17c4eca95550d86a6cc5fc4298b20296/README.md#L34-L60) described phone access to the same Mac separately from future device control.

These commits establish what was publicly shipped, not a new iPhone acceptance result for every later release. The checks used version-specific local IM.codes fixes; this page draws no conclusion about later upstream versions and does not copy those patches. Historical code links preserve the work without making the old compatibility layer a dependency of the new runtime.

## What was never delivered

- One AI taking over work across different computers or waking an offline computer.
- An independent phone AI runtime, offline chat service, or cross-device cloud conversation store.
- A guarantee that every later UI, upload, shared draft, download, or cross-agent continuation flow had passed on a physical phone.

The local web service and its access checks are still necessary for the Mac application. Retiring a preview integration does not mean removing that local foundation or deleting software a user installed separately.

[A letter from White Rabbit, November 3, 2026](old-letter.md) — Chinese original, exported directly from the same bundled source read by the application's About page.
