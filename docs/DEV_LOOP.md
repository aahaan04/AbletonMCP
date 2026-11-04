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
foreach ($t in 'notes','connection','mixer','drums','deletes','returns','automation','meters') {
  foreach ($v in '', '--via-server') { uv run python scripts\ableton_cli.py "selftest-$t" $v } }

# 3. Unit tests (no Live needed).
uv run --with pytest pytest -q
```

MCP server changed? Quit Claude Desktop fully (tray icon > Quit) and reopen it;
it starts a fresh `uv run ableton-mcp` from this checkout.

## Selftests

Every selftest builds on a throwaway track ("ableton_cli temp") and deletes it,
plus any return or master device it added, so the Set is left as it was found.
Deletes use `expected_name`, so a stale name refuses rather than deleting
something else; if a run is interrupted, look for "ableton_cli …" tracks or
extra returns.

| Selftest | Exercises |
|----------|-----------|
| `selftest-notes` | create clip → add → read → clear → read empty |
| `selftest-connection` | >8 KiB multi-byte replies; error then reconnect |
| `selftest-mixer` | volume dB, pan, mute, solo, send A → read back |
| `selftest-drums` | 808 Core Kit pads; pad macro and nested sampler param set → read back; empty pad refused |
| `selftest-deletes` | arrangement clip, session clip, nested device, device (wrong name refused first), a new return track, the track |
| `selftest-returns` | new return + Reverb, send to it, Limiter on master → read back → removed |
| `selftest-automation` | 8-bar Auto Filter sweep + volume fade → sampled back → clear one, clear all; probes Arrangement (Live refuses) |
| `selftest-meters` | kick loop: track/master meters > 0.05 while playing, fall < 0.01 after stop (Live holds the peak up to ~3 s) |

Set `PYTHONIOENCODING=utf-8` if the console mangles the multi-byte output.
`selftest-meters` plays audio for a few seconds.

## Batching Remote Script changes

Each Remote Script change costs a full Live restart. Build and unit-test several
changes, run `dev_install.ps1` once, restart once, then run every selftest both
ways. A capability that only adds parameters to an existing command goes in
`SCRIPT_FEATURES`, not `SCRIPT_CAPABILITIES` (which must list exactly the
dispatchable commands; a test enforces it).

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
