# Codex in The Chat Place: project review and implementation plan

Issues: #184 (additional providers) and #171 (shared session architecture).

Reviewed October 11, 2026 against `origin/main` at
`08473e38a6860d73447a4a0a3b3fcbe75a8f2d73`.
Requirements: [issue #184](https://github.com/TheIdeaPlace/AIChat/issues/184).
Related architecture work: [issue #171](https://github.com/TheIdeaPlace/AIChat/issues/171).

## Recommended outcome

Keep one session list containing Claude and Codex chats. Selecting a chat
automatically selects its backend for reading, replying, stopping and answering
requests. New Session adds a provider choice, remembered for the next new chat.
Provider filters are optional conveniences, not separate windows.

Implementation stays on the separate feature branch. Keep changes small and
independently reviewable; leave the shipping branch unchanged.

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
baseline was initially unverified.

Resolved on October 11: GUI tests run with desktop access outside the sandbox;
all GUI modules now use one session-scoped `wx.App`, and the DPI test respects
macOS logical window coordinates on Retina screens. No application code needed
changing for these fixes. The full suite subsequently completed with 1,337
passes, 69 platform-specific skips and exit code 0, without a native crash.

## Implementation progress

The first foundation change introduces a discovery protocol and shared snapshot
in `providers/base.py`, moves existing Claude discovery to `providers/claude.py`,
and routes `hub.collect` through it. Claude keys, sorting, cache hooks and
ownership metadata remain compatible. `SessionInfo` now carries a provider and
backend-aware identity; foreign sessions cannot resolve Claude transcript paths
or desktop links.

Focused validation: 207 tests passed across provider routing, sessions, terminal
and Cowork discovery, last messages, activity, stores, groups, hidden sessions
and titles. The subsequent full-suite run passed as recorded above.
This is the beginning of phase 2's Claude boundary, not completion of that phase:
reader and turn routing, the controller, persistence migration and Codex support
remain to be implemented. No Codex UI option is enabled yet.

October 12: added a window-independent `SessionController` and conversation
provider/reader contracts. The main window now routes chat loading, unloaded
session export, history for Properties, and background reply lookup through
the controller. Claude's reader and bounded tail parsing live in its adapter.
Unknown providers are rejected rather than falling back to Claude. Existing
window layout, Claude wording and session keys are retained.

Six new tests cover backend routing with colliding IDs, unavailable and duplicate
providers, missing history, read-only Claude history access and rejection of
foreign chats by the Claude adapter. Full-suite validation: 1,343 passed,
69 skipped, exit code 0.
Remaining reader work includes cross-session Find, backend-neutral message
labels and history availability checks; turn routing and Codex execution are
still pending. The installed local CLI reports `0.162.0-alpha.17.2`; its
app-server has stdio and schema-generation commands. Compatibility with a
released CLI remains to be verified before selecting a supported minimum.

## Reviewed checkpoints on October 12

The reader boundary now covers cross-session Find, backend-specific assistant
labels, properties and unloaded exports without requiring Claude files.
Reader construction runs on a worker thread. Generation checks preserve the
current session when an older factory finishes; tests cover both successful
and missing stale readers. Two older GUI tests now wait for the asynchronous
load announcement rather than depending on callback timing.

Independent review found no Claude regression in the foundation or reader
changes. It suggested the stale-reader tests, which were added. This is still
a checkpoint within the Claude-boundary phase: turn routing, capabilities,
provider-aware persistence and last-message cache routing remain outstanding.

A read-only `CodexProvider` now converts protocol session summaries and history
through an injected, initialized RPC connection. It lists all pages and archived
chats, excludes child chats, distinguishes unavailable runtime status from idle,
and reads history without resuming. Synthetic fixtures follow the generated
`0.162.0-alpha.17.2` schema. The adapter is not yet registered in the window.

Independent review identified partial-history acceptance and omitted MCP
outputs. Both were fixed and re-reviewed: partial/paginated history fails with
an explicit error until pagination is supported; tool activity preserves MCP
text/errors and file diffs. The follow-up review reported no blocking findings.

The development-only `tools/probe_codex.py` verified the local stdio handshake,
ChatGPT account type and listing of five active chats, zero archived chats.
It prints aggregate counts only, sends no prompts and reads no message bodies.
The reviewer checked its cleanup/privacy logic but did not repeat the real probe.
Windows executable discovery, cross-client status fidelity, paginated history,
forking and subscription execution still need compatibility checks.

Validation for these checkpoints: 1,361 tests passed, 69 platform-specific tests
skipped, process exited successfully. The independent reviewer also ran the
33 focused adapter/search/controller tests successfully. No new dialogs or
Codex controls are enabled. The Windows visual-probe gate remains part of the
later UI phase; no screenshot comparison was performed for this checkpoint.

The next discovery checkpoint adds a background-only `DiscoveryRegistry` that
merges and sorts provider snapshots, preserves Claude ownership metadata, and
retains private copies of the last successful results during an outage. Results
identify stale providers and first failures so callers can report each outage
once. Raw backend exception text is omitted from UI diagnostics.

Independent review caught malformed metadata being cached before merge/sort
validation. Validation now precedes cache replacement, with regression cases
for every sort order and malformed merge fields. Focused validation: 139 tests
passed across discovery, provider routing and Claude/Codex session adapters.
The follow-up independent review found no remaining blockers and passed all
57 discovery tests. Successful results and fallback results use the same
validated private cache copy, as suggested in that review.
The registry is not wired into the window yet. Before activation, stale results
must be excluded from live completion announcements and execution decisions;
production RPC transport, capabilities and opt-in settings remain outstanding.

## Managed transport checkpoint

Added a managed read-only stdio connection for the background provider workers.
The connection initializes once, serializes writes, matches concurrent responses
by request ID, bounds frames/requests and closes all pending requests on timeout
or protocol failure. Notifications are ignored in this read-only phase;
unexpected server requests close the connection without approving any action.
No start, resume, fork, inference or automatic replay methods are exposed.

Native executable discovery and process lifecycle live in `platform_paths.py`.
The child uses the existing process-tree owner, with no shell and no stderr
payloads in public diagnostics. Close waits for cleanup already running on
another worker. Discovery checks PATH and Mac Homebrew locations; desktop
bundled copies are not automatically selected. Resolving npm installations and
choosing a supported released minimum remain compatibility work.

The development probe now uses this same transport. Its live Mac check passed
initialization, reported ChatGPT account type and found five active chats and
zero archived chats. It printed no conversation bodies, IDs or tokens and
started no turn. This does not establish subscription execution or Windows
compatibility.

Independent review caught a deeply nested JSON frame that could terminate the
reader before cleanup. It now fails closed, with a regression test. Follow-up
review passed all 24 transport/launch tests with no blocking findings. The
broader focused suite passed 142 tests. Production connection ownership and
reconnect integration, subscription guards, capability routing and UI activation
remain next steps. The feature branch also includes the latest upstream sign-in
fix; the shipping branch itself was not modified.

Final full-suite validation for this checkpoint: 1,443 passed, 69 skipped,
process exited successfully. No UI or layout changes are part of this checkpoint.

## Connection ownership checkpoint

Codex providers can now create a managed connection lazily, with no process
startup in provider construction or reader creation. Concurrent discovery and
history requests share one initialized server. Deferred initialization lets the
owner cancel a blocked handshake during shutdown. A later read can reconnect
after a failed process is cleaned up; the failed operation is never replayed.
The session controller closes every registered backend, even if another
backend's cleanup fails, and rejects operations after shutdown begins.

Independent review caught cleanup failures losing ownership in late startup
and in reconnects. Both now retain the connection and report a redacted failure;
replacement startup is prevented until cleanup succeeds. Regression tests
cover those failures, eventual recovery, shutdown during initialization and
concurrent controller shutdown. Follow-up review passed all 113 focused
lifecycle/discovery/provider/controller tests with no remaining blockers.
Window shutdown wiring, capabilities, authentication guards and UI activation
are still pending.

Kelly authorized the best checks available on this Mac and requested a Windows
handoff issue. [Issue #226](https://github.com/TheIdeaPlace/AIChat/issues/226)
records CLI discovery, process cleanup, packaging, visual baseline and keyboard/
screen-reader validation for an agent on Windows. Windows validation remains
outstanding; it does not stop independent backend or Mac validation work.

Final full-suite validation for connection ownership: 1,457 passed, 69 skipped,
process exited successfully. No UI controls or layout changes are enabled by
this checkpoint.

## Stale discovery and recovery checkpoint

Finished-turn detection now excludes stale provider snapshots. The window also
drops stale or unknown Codex states from its comparison baseline, so recovery
cannot announce a chat as finished from a state remembered before the outage.
Codex must explicitly become idle; waiting for approval or unavailable status
does not count as a finished turn. Existing Claude behavior is preserved.

Independent review found no actionable defects and passed all 62 discovery
tests. Broader validation passed 562 hidden-window/session/view tests with six
platform skips; the additional end-to-end refresh/recovery regression passed
separately. No mixed-list option is enabled by this checkpoint.

The Mac visual probe was attempted for main-start, main-own, main-own-working
and main-desktop, but native screen capture failed for all four windows. No
screenshots or visual-parity verdict are available. Issue #226 records the
remaining Windows probe and screenshot review. The prior macOS packaging
failure at disk-image detach passed on retry; both platform test jobs and
Windows packaging passed for the published lifecycle checkpoint.

## Window shutdown checkpoint

Accepted window close now closes registered conversation backends on a dedicated
worker. The worker survives wx exit and cannot be cancelled with queued history
jobs; shutdown never blocks the UI thread. A vetoed close leaves the controller
usable. Cleanup still attempts every backend after a failure.

Independent review found no actionable defects. Validation passed both new
hidden-window cleanup tests and 104 controller, transport and discovery tests.
The published stale-state checkpoint passed both platform test and packaging
jobs. No Codex option or layout change is enabled yet; Windows visual and
hands-on accessibility checks remain tracked in issue #226.
