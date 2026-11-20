# MCP Guardian architecture

This document explains how the pieces fit together, with extra detail on the
Phase 4 inter-process link. It is written so the design can be explained in an
interview without reading the code line by line.

## What problem the project solves

Claude Desktop can call tools on an MCP server (read a file, run a command, and
so on). MCP Guardian sits in the middle:

1. Claude talks to the Guardian proxy as if the proxy were the MCP server.
2. The proxy asks a person before it forwards a `tools/call`.
3. Only an explicit **Allow** reaches the real (upstream) server.
4. Block, timeout, cancel, a crash, or a missing window all mean **do not run
   the tool**.

That last rule is called **fail closed**. Silence is never approval.

## The four phases

| Phase | What it added | What it deliberately did not do |
|---|---|---|
| 1 | Domain model, state machine, async coordinator | No network, no UI, no tool execution |
| 2 | PySide6 dialogs, one per request ID | No MCP connection |
| 3 | STDIO proxy using the official `mcp` Python SDK | Approval was deny-all, allow-all, or a test script |
| 4 | Loopback IPC so a real `tools/call` opens the Phase 2 dialog | No Claude Web / remote gateway |

Phases 1–3 stay in place. Phase 4 adds a transport between processes. It does
not move the allow/deny rules into the dialog.

## Who owns the decision

```text
Claude Desktop
    |  MCP over stdin/stdout (JSON-RPC only)
    v
Guardian proxy process
    |  creates a PENDING request (Phase 1 coordinator)
    |  sends a redacted copy over loopback IPC
    v
Guardian UI process
    |  shows one always-on-top dialog per request ID
    |  Allow / Block / close call the Phase 1 coordinator
    |  sends that decision back, tagged with the same request ID
    v
Guardian proxy process
    |  records the decision on ITS coordinator
    |  forwards the original tool call upstream only if the decision
    |  is an explicit approve
    v
Upstream MCP server
```

There are **two** coordinators, one in each process. They do not share memory.
The request ID (a UUID4) is the only link between them.

The dialog widget does not approve anything by itself. It emits “the human
pressed Allow for this ID.” `ApprovalController` (Phase 2) calls
`coordinator.approve` / `deny` / `cancel`. The proxy applies the matching
action on its own coordinator and then checks `decision.allows_execution`.
Only `DecisionAction.APPROVE` is true. That check already existed in Phase 1
and Phase 3.

## Why loopback TCP (and not a shared Future)

The proxy is started by Claude Desktop. The UI is started by the person.
They are different operating-system processes, so:

- An `asyncio.Future` cannot be shared. It lives inside one process.
- A global variable or a Python object cannot be shared either.
- The UI can restart, start late, or close while a call is waiting. The proxy
  has to notice that and **block the tool**.

The IPC mechanism is a **TCP connection to `127.0.0.1` only**, with
newline-delimited JSON and a shared secret.

Why this, and not the alternatives:

| Option | Why it was not the default |
|---|---|
| In-memory coordinator | Impossible across processes. |
| Windows named pipe | Good local IPC, but asyncio has no built-in pipe server. Doing it well means `pywin32` or a blocking API. Phase 4 avoids new dependencies. |
| `multiprocessing.connection` | Standard library and supports an auth key, but the API is blocking and awkward next to the asyncio proxy and the Qt loop. |
| A normal network socket (`0.0.0.0` or a LAN IP) | Would expose approval messages beyond this computer. Rejected. |
| Loopback TCP on `127.0.0.1` | Works on Windows 11, uses the asyncio the project already has, and never binds a routable interface. Easy to test without opening a Qt window. |

`127.0.0.1` is the IPv4 loopback address. Packets to it stay on this computer.
The server passes `family=AF_INET` and `host="127.0.0.1"` and then checks
`getsockname()`. If the socket is not bound to `127.0.0.1`, it closes. The
word `localhost` is intentionally not used: on some machines it also resolves
to IPv6, and a dual-stack socket is easier to misconfigure.

The UI **listens**. Every proxy **connects**. Claude may start several proxies
(one per MCP server). One UI can serve all of them. Each proxy has its own
connection. A decision is written only to the connection that created that
request ID.

## The message rules

Each message is one JSON object, then a newline. Messages are capped (64 KiB).
The first message on a connection must be `hello` with the secret token.
`hmac.compare_digest` checks the token so a wrong guess does not exit early
on the first differing character. Any other first message closes the socket.

Later messages are either:

- `request` — proxy to UI. Contains the request ID, server name, tool name,
  **redacted** arguments, and how long the dialog should wait.
- `decision` — UI to proxy. Contains the same request ID and exactly one of
  `approve`, `deny`, `cancel`, `timeout`.
- `error` — a rejected message. Never means approve.

The proxy keeps a map of `request ID → waiting Future`. A decision completes
**that** Future and no other. Unknown IDs, duplicates, and decisions that
arrive after the waiter is gone are logged and ignored.

The UI keeps the same kind of map from request ID to connection. It will not
apply a second decision to an ID it has already answered.

## Security assumptions

These are the assumptions a reviewer should be able to repeat.

**Trusted.** The person at the keyboard, and the Windows user account that
started both processes, are the decision-maker. The design stops accidents
and stops other computers. It does not stop malware that is already running
as the same user (that malware can click Allow, or read the process memory).

**Not trusted.** Claude’s tool arguments, the upstream server, other computers,
and any local program that does not know the current token.

Concrete protections:

1. **Bind address.** Listen and connect only on `127.0.0.1`. A file or flag
   that names any other host is rejected before a socket is opened.
2. **Token.** The UI generates a new `secrets.token_urlsafe(32)` value when it
   starts and writes `ipc-endpoint.json` under `%LOCALAPPDATA%\mcp-guardian\`.
   The file’s ACL is tightened to the current user (on other systems the mode
   is `0600`). The proxy must present that token in `hello`. The token is not
   logged and is not put in later messages.
3. **The endpoint file is deleted on a clean UI exit** if it still contains
   this process’s token, so a new process cannot reuse a dead session.
4. **Request IDs are lowercase UUID4 strings.** The proxy creates them. The UI
   must use the same ID. A decision whose ID does not match the request it was
   sent for is not an approval.
5. **Duplicates and late answers lose.** The Phase 1 state machine only leaves
   `PENDING` once. A second Allow is `DuplicateDecisionError`. After a timeout
   the state is `TIMED_OUT`, and a late Allow cannot change it.
6. **The dialog clock is a little shorter than the proxy clock** (about two
   seconds when the timeout is several seconds). The buttons disable when the
   dialog gives up, so the window cannot sit on “Allowed” after the proxy has
   already refused.
7. **Redaction before the wire and again before the pixels.** The proxy sends
   `redact_arguments(...)` output, not the original tool arguments. The UI runs
   the same redaction again when it builds the text box. The original
   arguments stay in the proxy process and are what the upstream server
   receives after Allow. They are not written to the log.
8. **Malformed input fails closed.** Bad JSON, a too-large frame, a bad
   version, or an unknown action closes that connection or cancels the
   request. Nothing in those paths calls `approve`.
9. **Disconnect and shutdown fail closed.** If the UI socket closes, the proxy
   cancels the pending request. If the UI process shuts down, it cancels
   pending dialogs and tells the proxy (or the socket simply drops). The
   upstream tool is not called.
10. **Stdout stays MCP.** The proxy speaks MCP only on stdout. Diagnostics go
    to stderr. IPC uses the TCP socket, not stdout.
11. **Upstream command comes from the proxy’s own CLI**, never from tool
    arguments. That rule is unchanged from Phase 3.
12. **No new third-party package.** IPC uses the Python standard library plus
    the PySide6 / qasync / mcp dependencies the earlier phases already had.

Remaining limitations (be honest about these):

- Same-user malware can read the endpoint file, draw its own window, or click
  the real one. Loopback IPC is not a security boundary between programs of
  the same Windows user.
- Redaction is name-based (`api_key`, `token`, `password`, …). A secret placed
  in a field named `message` is shown. That is the Phase 2 rule, unchanged.
- Very large argument objects are replaced with a fixed “omitted” placeholder
  rather than truncated, so a cut-off string cannot leak half of a secret.
  The reviewer then has less context.
- Claude Desktop itself has not been part of the automated tests. See
  `docs/manual-claude-desktop.md`. Do not claim that compatibility until that
  procedure has been run.
- There is still no remote / Claude Web gateway.

## How a popup stays responsive

Qt only paints and delivers clicks on the GUI thread. The coordinator only
works on the asyncio loop that created its Futures. Phase 2 already runs both
on one thread with **qasync**.

The IPC server uses `asyncio` streams on that same loop:

- Reading a socket is `await reader.readuntil(...)`, so Qt can repaint while
  we wait.
- Showing a dialog happens in a Qt **slot**. A signal delivered on the GUI
  thread runs the slot there, which is the thread widgets allow.
- The slot does not `await` and does not run the tool. It calls
  `ApprovalController.open_request` and returns. The controller schedules the
  coordinator work with `create_task`, exactly as a button click already did.

## Startup order

The UI must be listening before a person expects a popup. The proxy can start
later (Claude starts it). On each tool call the proxy retries the connection
for a few seconds so a UI that is still opening can win. If it cannot say
hello, the tool call is cancelled and the upstream server is not contacted.
Listing tools does not need approval; only `tools/call` does.

Commands and the Claude Desktop checklist live in the README and in
`docs/manual-claude-desktop.md`.
