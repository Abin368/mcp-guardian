# Manual test on Windows 11 (Claude Desktop)

This document records manual integration testing of MCP Guardian with
Claude Desktop on Windows 11. The automated test suite does not launch
Claude Desktop, so these checks verify the integration separately.

Use the harmless echo tool below. Do not start with a filesystem server, and
do not point a server at your home directory.

Guardian does not edit Claude's configuration. You paste the snippet yourself,
after making a copy of the file.

## 0. One-time setup

From the project directory:

```bat
cd /d C:\Users\abinm\OneDrive\Desktop\MCP
.venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -q
```

Leave the virtual environment's `python.exe` path handy:

```text
C:\Users\abinm\OneDrive\Desktop\MCP\.venv\Scripts\python.exe
```

Create a folder for the execution ledger and empty it if you are repeating
the test:

```bat
mkdir %LOCALAPPDATA%\mcp-guardian
del %LOCALAPPDATA%\mcp-guardian\echo-ledger.txt
```

## 1. Start the Guardian window

In a terminal you will leave open:

```bat
cd /d C:\Users\abinm\OneDrive\Desktop\MCP
.venv\Scripts\activate
python -m mcp_guardian.presentation
```

You should see:

- A window titled **MCP Guardian**
- The line `Listening on 127.0.0.1:47321. Waiting for the proxy.`
  (the port is 47321 unless you passed `--port`)

That terminal is the UI log. It prints request IDs, server names, and tool
names. It does not print argument values or the IPC token.

If the line says the port is already in use, another Guardian is running, or
something else owns `47321`. Close the extra copy or start again with
`--port 47322`. A window that failed to listen will not approve calls.

## 2. Point Claude Desktop at the proxy

Quit Claude Desktop first so it reloads configuration.

Copy the existing file, then edit it:

```text
%APPDATA%\Claude\claude_desktop_config.json
```

Add a server entry. Do not remove your other servers. Use your real python
path. The proxy is what Claude launches. The proxy launches the echo tool.

```json
{
  "mcpServers": {
    "guardian-echo": {
      "command": "C:\\Users\\abinm\\OneDrive\\Desktop\\MCP\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "mcp_guardian.proxy",
        "--server-name",
        "harmless-echo",
        "--upstream-command",
        "C:\\Users\\abinm\\OneDrive\\Desktop\\MCP\\.venv\\Scripts\\python.exe",
        "--upstream-arg",
        "-m",
        "--upstream-arg",
        "mcp_guardian.manual.echo_server",
        "--upstream-env",
        "MCP_GUARDIAN_ECHO_LEDGER=C:\\Users\\abinm\\AppData\\Local\\mcp-guardian\\echo-ledger.txt",
        "--approval",
        "ipc",
        "--log-level",
        "INFO"
      ]
    }
  }
}
```

`--approval ipc` is required. Without it the proxy stays on `deny-all` and
every call is blocked with no popup.

Start Claude Desktop again.

## 3. Check that the proxy connected

In Claude, open the connector / MCP tools list for this chat. `guardian-echo`
should show a tool named `echo`.

If it is disconnected, open the newest file under:

```text
%APPDATA%\Claude\logs
```

Look for a name that contains `guardian-echo`. That file is the proxy's
stderr (and the echo process's stderr, because the proxy forwards it).

A healthy start looks like:

```text
approval_mode=ipc — tool calls wait for the Guardian UI on 127.0.0.1
starting proxy server_name=harmless-echo
```

You should **not** see tool argument values or the IPC token in that log.

If the log says the endpoint file is missing, the UI was not started, or it
was started by a different Windows user. Start the UI from step 1 and restart
Claude.

## 4. Allow one call

Ask Claude, in plain language:

```text
Use the guardian-echo tool named echo with message hello-from-claude.
```

An always-on-top dialog should appear:

- Server: `harmless-echo`
- Tool: `echo`
- Arguments include `hello-from-claude`
- **Allow** and **Block** are both enabled

Press **Allow** once.

Expect:

- Claude shows a result containing `echo:hello-from-claude`
- The dialog buttons disable and the text says the proxy may run the tool
- `%LOCALAPPDATA%\mcp-guardian\echo-ledger.txt` contains one line: `executed`
- The Claude log contains `tools/call approved; forwarding upstream` and
  `echo_tool_executed`

`echo_tool_executed` is written only when the echo process actually runs. It
does not include the message text. The ledger line does not either.

## 5. Block one call

Ask Claude to call `echo` again with message `this-must-not-run`.

Press **Block**.

Expect:

- Claude shows an error from MCP Guardian saying the call was blocked or denied
- The reply does **not** contain `echo:this-must-not-run`
- The ledger file still has **one** line (no new `executed`)
- The Claude log contains `tools/call blocked; upstream not contacted`
- The log does **not** contain a second `echo_tool_executed` for this call

That is the check that the upstream tool did not run. The message Claude got
back is not enough on its own; the ledger and `echo_tool_executed` are the
evidence from the upstream process.

## 6. Optional checks

- Close the dialog with the title-bar X instead of Block. The ledger must not
  gain a line. The proxy log should show a cancel, not `forwarding upstream`.
- Close the Guardian window while a dialog is still waiting, or stop the UI
  process. The waiting call must fail, and the ledger must not gain a line.
- Ask for two echo calls if Claude will issue them together. Each dialog has
  its own request ID. Allow one and Block the other. Only one new ledger line
  should appear.

## 7. Put Claude back

Remove the `guardian-echo` entry from `claude_desktop_config.json` when you
are finished, or leave it if you want the gate to stay. Restoring the copy
you made in step 2 is the safest undo.

Stop the UI by closing its window.

## What this does not prove

- It does not test a filesystem MCP server. Add one later, pointed at an empty
  directory you created for the test, only after echo Allow and Block behave
  as above.
- It does not test Claude on the web.
- A program running as your Windows user can still read the endpoint file or
  click Allow. The socket is not exposed to other computers; it is not a
  boundary between programs of the same user.
