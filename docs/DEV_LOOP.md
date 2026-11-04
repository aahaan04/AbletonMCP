# Local dev loop

The MCP server runs from this checkout with uv, and the Remote Script is copied
from it into Live's User Library, so code changes take effect without publishing.

## Day to day

```powershell
# 1. Remote Script changed? Install it, then quit and restart Live.
#    Re-selecting AbletonMCP under Control Surface does NOT reload the code:
#    Live keeps the imported module and only re-instantiates the old class.
#    `ableton_cli.py ping` lists capabilities, so you can confirm the reload.
powershell -ExecutionPolicy Bypass -File scripts\dev_install.ps1

# 2. Test against Live directly (no Claude Desktop needed).
uv run python scripts\ableton_cli.py ping
uv run python scripts\ableton_cli.py selftest-notes                # raw socket
uv run python scripts\ableton_cli.py selftest-notes --via-server   # through MCP_Server tools
uv run python scripts\ableton_cli.py get_track_info track_index=0  # any command

# Run every selftest both ways (PowerShell):
foreach ($t in 'notes','connection','mixer','drums','deletes') { foreach ($v in '', '--via-server') {
  uv run python scripts\ableton_cli.py "selftest-$t" $v } }

# 3. Unit tests (no Live needed).
uv run --with pytest pytest -q
```

MCP server changed? Quit Claude Desktop fully (tray icon > Quit) and reopen it;
it starts a fresh `uv run ableton-mcp` from this checkout.

## Selftests

| Selftest | Exercises | Leaves behind |
|----------|-----------|---------------|
| `selftest-notes` | create clip → add → read → clear → read empty → delete clip | track "ableton_cli selftest" (reused) |
| `selftest-connection` | >8 KiB multi-byte replies; error then reconnect | same track, name restored |
| `selftest-mixer` | volume dB, pan, mute, solo, send A → read back → defaults | same track, defaults restored |
| `selftest-drums` | 808 Core Kit pads; pad macro and nested sampler param set/read/restore; empty pad refused | track "ableton_cli drums" with the kit (reused) |
| `selftest-deletes` | throwaway track: delete arrangement clip, session clip, nested device, device (wrong name refused first), track; return delete with wrong name refused | nothing |

Set `PYTHONIOENCODING=utf-8` if the console mangles the multi-byte output.

## Upstream PRs

Fetch and merge with `--no-ff` so the authors' commits stay intact:

```powershell
git fetch upstream pull/<N>/head:pr-<N>
git merge --no-ff pr-<N> -m "Merge upstream PR #<N>: <title>"
```

Upstream PRs usually edit only `AbletonMCP_Remote_Script/__init__.py`; run
`dev_install.ps1` afterwards to resync the bundled copy, and add new commands to
`SCRIPT_CAPABILITIES` so the server's handshake gate knows about them.

## Claude Desktop config

`%APPDATA%\Claude\claude_desktop_config.json`, under `mcpServers`:

```json
"AbletonMCP": {
  "command": "C:\\Users\\aahaa\\.local\\bin\\uv.exe",
  "args": ["--directory", "C:\\Users\\aahaa\\dev\\AbletonMCP", "run", "ableton-mcp"],
  "env": { "UV_PYTHON_INSTALL_DIR": "C:\\Users\\aahaa\\.local\\share\\uv\\python" }
}
```

`UV_PYTHON_INSTALL_DIR` keeps uv's managed Python outside `%APPDATA%`. Claude
Desktop is a packaged (MSIX) app, so its writes under `%APPDATA%` are redirected
into a private folder, which breaks the directory link uv creates there.

## Versioning

`SCRIPT_VERSION` in `AbletonMCP_Remote_Script/__init__.py` must equal
`EXPECTED_REMOTE_SCRIPT_VERSION` in `MCP_Server/remote_script_install.py`, and
every new command goes in `SCRIPT_CAPABILITIES`. `dev_install.ps1` keeps the
bundled copy (`MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py`)
identical; a test enforces it.
