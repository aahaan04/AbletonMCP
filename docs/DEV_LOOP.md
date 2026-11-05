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
foreach ($t in 'notes','connection','mixer','drums','deletes','returns','automation','meters',
              'arrangement','noteedit','grooves','notewrite','devparams','levels','search','groovefix',
              'savefolder') {
  foreach ($v in '', '--via-server') { uv run python scripts\ableton_cli.py "selftest-$t" $v } }

# 3. Unit tests (no Live needed).
uv run --with pytest pytest -q
```

MCP server changed? Quit Claude Desktop fully (tray icon > Quit) and reopen it;
it starts a fresh `uv run ableton-mcp` from this checkout. See
[Keeping Claude Desktop current](#keeping-claude-desktop-current).

Remote Script changed? Always go through `dev_install.ps1` (it syncs the bundled
copy *and* installs into Live); copying into the bundle alone does not reach Live.
Bump `SCRIPT_VERSION` for each install you test, so `ping` proves the reload.

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
| `selftest-arrangement` | two temp tracks: intro/main/outro sections (bars 1-8/9-16/17-24) placed and read back exactly; filter envelope verified by playing the section soloed; refusals; locators and loop set, then removed/restored. Aborts if one of your locators sits at bar 1, 9 or 17 |
| `selftest-noteedit` | 12-note drum pattern: transpose by id, velocity by range, shift+duration, delete one, invalid edit refused → exact notes and ids |
| `selftest-grooves` | load "Swing MPC 3000 8ths 57", assign to a hat clip, read back. Leaves the groove in the Set's pool (Live's API can't remove it) |
| `selftest-notewrite` | server tools (same in both modes): `create_clip` with compact notes + repeat, `write_drum_pattern` on the 808 kit by pad name/number, unknown pad refused, compact read-back = raw notes |
| `selftest-devparams` | drum rack read shallow (KB vs full); master Limiter Ceiling and return Reverb Decay Time set by name and read back; out-of-range refused |
| `selftest-levels` | `get_meters` shape; `measure_levels` (server tool, both modes) on a soloed kick loop: track/master peak > 0.05, an empty temp track < 0.01; then with the per-call budget cut to 3 s, an 8 s window returns a job, a second measurement is refused, and ≥3 calls combine into one result. Playhead and solo restored |
| `selftest-savefolder` | `save_set` folder rule without sending keys: a missing folder outside Music/Documents is refused, nothing created, Live's title unchanged; a missing one under Documents is allowed (planned, not created) |
| `selftest-search` | find "808 Core Kit" in drums, limit respected, a result loads; capped listing (via server) |
| `selftest-groovefix` | `groove_amount` in session info; new clips' inherited groove reported; `clear_clip_groove` on two clips → one neutral groove, reused. Leaves "AbletonMCP No Groove" in the pool |
| `selftest-save` | **Not in the loop above; run deliberately.** Sends Ctrl+S / Ctrl+Shift+S to Live. Needs a new Untitled Set. Saves into `ABLETON_CLI_SAVE_FOLDER`, or by default into a new timestamped folder under `Documents\AbletonMCP selftest` (checking save_set creates it): Save As, change, Ctrl+S, existing name refused. Leaves the saved Set behind, and the open Set becomes that file |

Set `PYTHONIOENCODING=utf-8` if the console mangles the multi-byte output.
`selftest-meters` plays audio for a few seconds; `selftest-arrangement` plays the
Arrangement for ~3 s with only a temp track soloed.

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

## Keeping Claude Desktop current

`C:\Users\aahaa\dev\AbletonMCP` is the only checkout. Claude Desktop runs it,
and all development happens in it on `dev`. Don't create other clones or
worktrees for this repo. The `.venv` is an editable install that points at
`MCP_Server\` here, so a commit takes effect as soon as the server restarts.
`uv sync` is needed only when `pyproject.toml` dependencies change.

Claude Desktop starts the server **once, when the app starts**, and keeps that
process and its tool list until the app is fully quit. Closing the window only
hides the app in the tray, and a new chat reuses the same server. After any
change under `MCP_Server\`, the app's server runs old code (old tools, old
`EXPECTED_REMOTE_SCRIPT_VERSION`) until you choose **tray icon > Quit** and
reopen the app. On Day 5 this showed up as Claude Desktop expecting Remote
Script 1.10.0 with none of the new tools, while Live already had 1.11.1 loaded.

After pulling or committing server code, and before an end-to-end run from
Claude Desktop:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check_fresh.ps1
```

It warns, and exits 1, when any of these is true:
- the checkout isn't on `dev`;
- the checkout has uncommitted changes;
- the checkout differs from `origin/dev`;
- a running `ableton-mcp` server started before the newest `MCP_Server\*.py` change.

It also prints the Remote Script version the server expects. Then
`get_remote_script_info` from Claude Desktop should report that same version
for both the server and Live.

## Versioning

`SCRIPT_VERSION` in `AbletonMCP_Remote_Script/__init__.py` must equal
`EXPECTED_REMOTE_SCRIPT_VERSION` in `MCP_Server/remote_script_install.py`, and
every new command goes in `SCRIPT_CAPABILITIES`. `dev_install.ps1` keeps the
bundled copy (`MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py`)
identical; a test enforces it.
