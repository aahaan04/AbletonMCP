# Local dev loop

The MCP server runs from this checkout with uv, and the Remote Script is copied
from it into Live's User Library, so code changes take effect without publishing.

## Day to day

```powershell
# 1. Remote Script changed? Install it, then re-select AbletonMCP in Live
#    (Settings > Link, Tempo & MIDI > Control Surface) or restart Live.
powershell -ExecutionPolicy Bypass -File scripts\dev_install.ps1

# 2. Test against Live directly (no Claude Desktop needed).
uv run python scripts\ableton_cli.py ping
uv run python scripts\ableton_cli.py selftest-notes                # raw socket
uv run python scripts\ableton_cli.py selftest-notes --via-server   # through MCP_Server tools
uv run python scripts\ableton_cli.py get_track_info track_index=0  # any command

# 3. Unit tests (no Live needed).
uv run --with pytest pytest -q
```

MCP server changed? Quit Claude Desktop fully (tray icon > Quit) and reopen it;
it starts a fresh `uv run ableton-mcp` from this checkout.

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
