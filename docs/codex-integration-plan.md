# Codex in The Chat Place: project review and implementation plan

Reviewed October 11, 2026 against `origin/main` at
`08473e38a6860d73447a4a0a3b3fcbe75a8f2d73`.
Requirements: [issue #184](https://github.com/TheIdeaPlace/AIChat/issues/184).
Related architecture work: [issue #171](https://github.com/TheIdeaPlace/AIChat/issues/171).

## Recommended outcome

Keep one session list containing Claude and Codex chats. Selecting a chat
automatically selects its backend for reading, replying, stopping and answering
requests. New Session adds a provider choice, remembered for the next new chat.
Provider filters are optional conveniences, not separate windows.

This branch contains the review and plan only. Implement in small, independently
reviewable changes on feature branches; leave the shipping branch unchanged.

## What the current code tells us

| Area | Finding | Required change |
| --- | --- | --- |
| `hub.py` | Already merges Claude desktop, terminal and owned sessions and sorts one snapshot. | Retain merging and sorting; gather through a provider registry. Isolate backend failures. |
| `sessions.py` | `SessionInfo` mixes presentation with Claude transcript paths and desktop links. | Separate common session metadata from backend operations and capabilities. |
| `own_store.py` | Stores are keyed by raw CLI ID, validate Claude options, and assume Claude ownership. | Versioned provider-aware persistence and validation. |
| `transcript.py` | Useful message and transcript models, but assistant labels and parsers assume Claude. | Common message types with provider-aware assistant labels; separate parsers. |
| `ui/main_frame.py` | 5,303 lines; directly imports `TurnRunner`, response builders and provider-specific features. Runners, drafts and other state use raw session IDs. | Introduce a controller between the window and providers, including all per-session state. |
| `groups.py`, `hidden.py`, `titles.py` | Persist session keys. | Preserve existing memberships and overrides during identity changes. |
| `claude_cli.py`, `signin.py`, `usage.py`, `changes.py`, `about_you.py` | Claude-specific behavior and safeguards. | Keep as Claude adapter internals; expose only common results to the controller. |

Adding a Codex subprocess directly to the window would leave these assumptions
in place. The provider boundary is the prerequisite for reliable mixed chats.

## Codex integration choice

Use a managed local `codex app-server` process over stdio. Its documented
protocol supports initialization, `thread/list`, `thread/read`, start/resume/fork,
turn streaming, interruption and approval requests. Generate protocol schemas
from the supported CLI version and record that version in fixtures.

Use explicit top-level source filters: listing defaults omit `appServer` and
`exec`. Fetch every page and archived chats separately. Read history without
resuming. Runtime `notLoaded` must not imply idle in another client.

Use Codex-managed ChatGPT sign-in; verify account/provider configuration before
inference and reject API-key or other billing routes. Rate limits are available
through account methods. Do not copy tokens into TCP's settings or logs.

These protocol findings come from the
[official app-server documentation](https://learn.chatgpt.com/docs/app-server).
The exact installed-version behavior still needs a compatibility probe.

## Proposed boundary

Add `providers/base.py`, `providers/claude.py`, `providers/codex.py` and a
session controller. Start with the small contract needed by the list and reader;
extend it for turns rather than extracting every feature at once.

- Identity: `(provider, native_session_id)`; source and ownership are separate.
  The protocol's model provider is not TCP's agent-provider identity.
- Discovery: session summaries, paging, recoverable errors and refresh signals.
- Reading: neutral messages with stable item IDs, assistant names, attachments,
  activity, errors, questions and file changes.
- Execution: start, continue owned chat, fork external chat, send, interrupt,
  and typed events keyed by session, turn, item and request IDs.
- Requests: typed command/file approvals and questions; provider-specific
  choices and conversion of answers stay in the adapter.
- Options: models, effort, sandbox and approval policies with labels and
  backend validation. Never translate Codex settings into Claude mode names.
- Capabilities: read, send, fork, delete, attachments, live state, approvals,
  usage, instructions, desktop links and remote access, per session where needed.

The controller owns routing, queues, drafts, request ordering and lifecycle.
The UI consumes common data and asks the controller to act. Preserve a host
identity slot for #171 without implementing another-machine transport here.

## Ownership and reliability rules

Externally discovered Codex chats start read-only. Continue Here forks into a
TCP-owned chat, paralleling the Claude safeguard. Do not resume an external chat
to display it. Only offer direct sends to chats TCP owns, with one active turn
per chat. Verify external-client concurrency before relaxing that restriction.

Do not infer another app's active work or waiting state from a timestamp alone.
Show an honest unknown/unavailable state where live information cannot be verified.
Local hide/title/group changes stay TCP-owned. Permanent delete must be a
capability with its own backend implementation, never Claude's path deletion.

One unavailable backend must leave the other's list and replies usable. Cache
the last successful discovery result, mark it stale, and report the error once.
Never replay an interrupted or uncertain send automatically after reconnecting.
Reconcile history and turn IDs first to avoid duplicated prompts and messages.

Pending approvals are session-scoped and use the existing Answer workflow; a
background chat must not steal focus. Unknown requests fail closed with a clear
message. Do not offer approval scopes the installed backend cannot implement.
Interrupt a Codex turn without terminating other chats sharing its process.
On shutdown, resolve/cancel requests and stop TCP-owned work deliberately.

## Persistence and migration

Default old records to Claude. Add a versioned provider identity and a separate
provider-options payload; preserve the original data on failed migration.
Back up and migrate atomically. Either retain legacy Claude keys behind a
resolver or migrate every reference together: own sessions, groups, hidden,
titles, unread state and saved per-session preferences. Choose one approach
before coding; never partially rename keys.

Key runner, draft, queue, permission and cache state by the same canonical
identity. Test equal native IDs from different providers. Disabling Codex hides
its rows without deleting its preferences or group memberships.

## Delivery sequence and acceptance gates

1. **Compatibility probe.** On a development profile, check CLI discovery on
   Windows/macOS, protocol version, subscription sign-in, source coverage,
   history fidelity, external live status and fork behavior. Use synthetic
   fixtures and a disposable real chat, never a user's existing chat for writes.
   Document desktop/cloud coverage explicitly; do not promise chats absent from
   the local app-server catalog. No inference is needed for initial discovery.
2. **Claude boundary.** Introduce common types, controller and Claude adapter.
   Preserve existing list wording, commands, ownership guards and execution.
   Gate: existing suite and parity fixtures pass; no visible Claude regression.
3. **Mixed read-only list.** Add opt-in Codex discovery and history conversion,
   provider column/filter, loading/errors and provider-aware announcements.
   Gate: mixed sorting, groups, hidden, archived, Find, export, formatted reading,
   code blocks and last-message column work; one backend failure is isolated.
4. **Writable Codex chats.** Add New Session, fork/Continue Here, owned resume,
   send, stop, queue handling, approvals/questions and model/policy selection.
   Gate: missing auth, denied requests, process crashes, interrupted turns and
   concurrent chats have correct outcomes and accessible feedback.
5. **Extras and release readiness.** Add supported changes/usage/instructions,
   adapt help and product wording, and disable unsupported Claude commands on
   Codex chats. Run platform packaging and hands-on screen-reader checks before
   enabling by default. Keep Claude Remote Control and Cowork provider-specific.

Each step should be its own reviewable PR. Ship no provider refactor together
with an unverified Codex execution path.

## Validation

Use a fake app-server with recorded protocol fixtures for partial lines,
out-of-order responses, interleaved chats, unknown items, paging, errors,
approvals, disconnects and terminal outcomes. Test item deduplication across
live events and history refresh. Redact all private paths and credentials.

Add migration tests with existing groups/titles/hidden data, colliding IDs and
corrupt stores; parity tests for Claude; and UI routing/capability tests for a
mixed list. Real smoke checks must cover subscription billing, resumed context,
fork source preservation and two chats where stopping one leaves the other
running. JAWS, NVDA and VoiceOver checks cover provider names, focus, request
answers, queued prompts and announcements from background chats.

## Decisions to verify during the probe

- Which local desktop, CLI and IDE chats the installed server actually exposes;
  cloud-only chats require a separate supported discovery route if absent.
- What live status can be trusted across app-server processes.
- Supported CLI minimum/version policy, safe executable discovery and packaging.
- Whether every needed approval/question scope and attachment type is supported.
- Long-history performance without experimental pagination dependencies.

Proceed with the unified-list design. These verification items constrain
capabilities and coverage; they do not require a separate provider window.

## Review validation results

No application code changed. `git diff --check` passed. The initial baseline
suite produced 831 passes, 69 skips and 502 setup errors, beginning with
wxPython being unable to access the Mac display. A retry outside the sandbox
also failed and aborted with exit 134 and a pthread diagnostic. A clean full
baseline therefore remains unverified and must be established in the supported
GUI test environment before the Claude refactor. Retry output is saved locally
at `/private/tmp/aichat-codex-baseline.txt`.
