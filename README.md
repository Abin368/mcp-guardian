# MCP Guardian

Human-in-the-loop approval for Model Context Protocol (MCP) tool calls.

Claude Desktop talks to the Guardian **proxy**. The proxy asks the Guardian
**window** before it forwards a tool call. Only **Allow** runs the tool.
Block, timeout, cancel, a closed window, or a missing window all mean the
upstream tool does not run.

| Phase | What it is |
|---|---|
| 1 | Fail-closed state machine and async coordinator |
| 2 | PySide6 approval dialogs (one per request ID) and redaction |
| 3 | MCP STDIO proxy (`mcp` 2.3) with deny-all, allow-all, and scripted approvals |
| 4 | Loopback IPC so a real `tools/call` opens the Phase 2 dialog |

The design and the security assumptions are in
[docs/architecture.md](docs/architecture.md). A hands-on Claude Desktop
checklist is in [docs/manual-claude-desktop.md](docs/manual-claude-desktop.md).
Manual testing was performed with Claude Desktop on Windows 11.
Verified scenarios include:
- Successful tool execution after Allow.
- Tool execution prevented after Block.
- Multiple tool calls.
- Timeout with fail-closed behavior.
- Upstream failure handling.

These manual checks are separate from the automated test suite.
See docs/manual-claude-desktop.md for the testing procedure.

## IPC in one paragraph

The UI and the proxy are separate processes, so they cannot share an
`asyncio.Future`. The UI listens on **`127.0.0.1` only** (never a LAN address).
The proxy connects, says hello with a random token, and sends each tool call
with a UUID. The window shows the server name, the tool name, and **redacted**
arguments. Allow or Block is sent back with that same UUID. The proxy runs the
original call only when its own coordinator records an explicit approve.
Named pipes would also stay off the network, but they need an extra library or
a blocking API. Loopback TCP uses the asyncio the project already has, and the
bind address is checked after `listen`.

## Requirements

- Python 3.11+
- Windows 11 for the manual Claude Desktop procedure
- PySide6 and qasync for the window (included in the `dev` extra)

## Setup

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Run tests

```bash
pytest -v
```

The IPC tests use a fake upstream server with an execution counter. They do
not open a visible window. A separate offscreen Qt test checks the dialog.

## Start the approval window

Start this **before** you expect a popup. From the activated virtual environment:

```bash
python -m mcp_guardian.presentation
```

Or, after install:

```bash
mcp-guardian-ui
```

The window listens on `127.0.0.1:47321` and writes the port and a new token to:

```text
%LOCALAPPDATA%\mcp-guardian\ipc-endpoint.json
```

That file is created by Guardian. It is not the Claude Desktop config, and
these commands do not edit Claude's config. The token is not printed. If the
port is already taken, the window says so and **will not approve anything**.

Useful flags:

```bash
python -m mcp_guardian.presentation --sample
python -m mcp_guardian.presentation --no-ipc
python -m mcp_guardian.presentation --port 47322
```

`--sample` also opens one local demo dialog. That demo does not run a tool.
`--no-ipc` is the Phase 2 demo with no listener.

Close the window (and any dialogs) to stop. Pending proxy calls are then
cancelled.

## Start the proxy

Default approval mode is `deny-all` (every tool call is blocked). To ask the
window:

```powershell
python -m mcp_guardian.proxy `
  --server-name harmless-echo `
  --upstream-command .venv\Scripts\python.exe `
  --upstream-arg=-m `
  --upstream-arg=mcp_guardian.manual.echo_server `
  --approval ipc
```

`allow-all` still exists and must be chosen on purpose. It is not the default.
`deny-all` and the scripted provider used by tests are unchanged.

The proxy reads the endpoint file the UI wrote. You can instead pass
`--ipc-host 127.0.0.1 --ipc-port PORT --ipc-token TOKEN` together. Any other
host is rejected.

MCP JSON-RPC stays on the proxy's **stdout**. Logs go to **stderr**. Argument
values and the IPC token are not logged.

## What the window shows

Approval dialog, always on top, titled **MCP Guardian — server / tool**:

- Request ID, server name, tool name
- Tool arguments with names such as `api_key`, `token`, and `password` shown
  as `***REDACTED***`
- **Block** and **Allow**. Neither is the default button, so Enter does not
  approve. The buttons disable after the first decision.
- Closing the dialog cancels that request only.

A dialog that came from the proxy says that Allow lets the proxy run **this**
tool. A sample dialog still says that the sample itself does not run a tool.

## Layout

```text
src/mcp_guardian/
  domain/            Phase 1 models and state machine
  application/       Phase 1 PendingRequestCoordinator
  presentation/      Phase 2 dialogs, plus the Phase 4 UI bridge
  proxy/             Phase 3 STDIO proxy and the Phase 4 IPC provider
  ipc/               Loopback protocol, client, and server
  manual/            Harmless echo server for a manual test
docs/
  architecture.md
  manual-claude-desktop.md
tests/
  unit/              Phase 1
  ui/                Phase 2 dialogs and the Phase 4 window
  proxy/             Phase 3
  ipc/               Phase 4 provider, protocol, and CLI
```

## Scope

| Included | Not included |
|---|---|
| Fail-closed coordinator and state machine | Remote / Claude Web gateway |
| PySide6 dialogs, one per request ID | Editing Claude Desktop's config for you |
| Redaction of obvious secret **names** | Redaction of secrets hidden in ordinary fields |
| STDIO proxy for one upstream server | A claim that Claude Desktop was tested here |
| Loopback IPC with a per-launch token | Protection from malware running as your user |
