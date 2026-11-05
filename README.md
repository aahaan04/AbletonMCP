# AbletonMCP (aahaan04 fork)

**Claude can produce a full track in Ableton Live end to end, from a single chat prompt.**

This fork of [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) extends the MCP server and Live Remote Script so that Claude Desktop, using only MCP tools, can take an empty Set to an arranged, mixed and saved track.

Fork built and maintained by Aahaan Sheth ([github.com/aahaan04](https://github.com/aahaan04), [linkedin.com/in/aahaan04](https://www.linkedin.com/in/aahaan04)).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Demo

> **Video:** _link coming soon_

In the demo, Claude starts from an empty Live Set and a single prompt. It sets the tempo, builds drums, bass, chords and a hook, adds a reverb return and a master limiter, writes a filter sweep into the build, arranges about 64 bars with named locators, checks levels per section, and saves the Set. The only manual step is exporting the audio (File > Export Audio/Video), which Live's API does not offer.

---

## What's new in this fork

Compared with the original project:

**Note writing and editing**
- Compact notes: `[pitch, start, duration, velocity]` arrays instead of verbose objects.
- `repeat_every_beats` tiles a one-bar pattern across a clip.
- `create_clip` can name and fill a clip in the same call.
- `modify_notes` and `delete_notes` edit notes in place by note ID or by pitch and time range.
- `get_clip_notes(compact=true)` reads clips back about 5x smaller.

**Drum Rack pads**
- `get_drum_pads` lists filled pads with their MIDI note, name and devices.
- `write_drum_pattern` writes drums from step strings per pad, such as `{"Kick": "x...x...", "Hat": "o.x.o.X."}`, with accents, ghost notes and velocity levels.
- Devices inside any rack can be read and set per pad or chain.

**Mixer**
- Volume in exact dB, pan, sends, mute and solo on tracks, returns and the master.

**Returns and master**
- Create and delete return tracks.
- Load, read, set and delete devices on returns and the master.

**Automation**
- Draw, read and clear clip envelopes for mixer and device parameters, including devices inside racks.

**Meters and levels**
- `measure_levels` plays the Set and reports peak, mean and clipping per track, return and the master, optionally per locator section.
- It skips the first seconds of Live's held peaks, and it can measure a whole song across several calls.

**Arrangement and locators**
- Fill sections from Session clips by bar range.
- Read back the layout with gaps and overlaps.
- Set the loop and named locators.

**Grooves**
- Load grooves, assign them to clips and set amounts.
- `clear_clip_groove` makes clips play straight.
- The tools warn when the global groove amount is 0%.

**Deletes**
- Delete tracks, return tracks, devices, nested devices and Arrangement clips.
- An optional `expected_name` guard refuses to delete the wrong thing.

**Browser search**
- `search_browser` finds instruments, kits, effects and grooves by name.
- Listings take a name filter and a result limit.

**Saving (Windows only)**
- `save_set` saves the open Set, or does a Save As to a folder and name, through Live's own Save command.
- It verifies that Live is the foreground window before sending any key, and never overwrites an existing file.

**Reliability fixes**
- A server started before Live no longer locks out newer tools: gated tools re-check the Remote Script version before refusing.
- Connection fixes: timeouts are preserved and failed sockets are closed (upstream #139).
- Every command runs on Live's main thread through a bounded queue (upstream #140).
- Locators are now placed at the right bar. The old single-call method could drop them at the previous playhead.
- `set_arrangement_time` reports the playhead position Live actually applied.
- Tool results no longer stop the run to ask about dataset recording.

---

## Quickstart

Development and testing were done on Windows with Live 12. macOS uses the same steps with macOS paths; `save_set` is Windows only.

### 1. Install uv

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS**
```bash
brew install uv
```

### 2. Clone the repo

```bash
git clone https://github.com/aahaan04/AbletonMCP.git
```

### 3. Add the server to Claude Desktop

Open Claude Desktop's config file. In current versions it's under **Settings > Desktop app > Local MCP servers > Edit config**; older versions have it under **Settings > Developer > Edit Config**. The file is at:
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add an entry under `mcpServers` that runs the server from your clone:

**Windows**
```json
{
  "mcpServers": {
    "AbletonMCP": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uv.exe",
      "args": ["--directory", "C:\\Users\\<you>\\dev\\AbletonMCP", "run", "ableton-mcp"],
      "env": { "UV_PYTHON_INSTALL_DIR": "C:\\Users\\<you>\\.local\\share\\uv\\python" }
    }
  }
}
```

`UV_PYTHON_INSTALL_DIR` matters for the Microsoft Store (MSIX) build of Claude Desktop. That build redirects writes under `%APPDATA%`, which breaks the Python install uv makes there.

**macOS**
```json
{
  "mcpServers": {
    "AbletonMCP": {
      "command": "uv",
      "args": ["--directory", "/Users/<you>/dev/AbletonMCP", "run", "ableton-mcp"]
    }
  }
}
```

If Claude Desktop can't find `uv`, use its full path (`which uv`).

### 4. Install the Remote Script

```bash
uv --directory <path-to-clone> run ableton-mcp-install-script
```

This copies the Remote Script into your Ableton User Library's `Remote Scripts` folder. Add `--list-targets` to preview where it goes.

### 5. Restart Live and select the control surface

1. Fully quit Live and start it again. Re-selecting the control surface does not load new Remote Script code.
2. Open **Settings > Tempo & MIDI** (older versions call it **Link, Tempo & MIDI**).
3. Under **Control Surface**, select **AbletonMCP**.
4. Set its **Input** and **Output** to **None**.

### 6. Restart Claude Desktop

Claude Desktop starts the server once, when the app starts, and keeps it until the app fully quits. Closing the window is not enough.
- **Windows:** right-click the tray icon > **Quit**, then reopen.
- **macOS:** **Claude > Quit Claude** (Cmd+Q), then reopen.

Do this again after every server update. Ask Claude to run `get_remote_script_info` to confirm that the server and Live report the same Remote Script version (currently 1.11.1).

---

## Example prompt

> Build a complete, original 124 BPM house track in F minor in this empty Set. Use a Drum Rack, bass, chords and a hook, a reverb return and a limiter on the master. Add swing on the hats and a filter sweep on the build. Arrange about 64 bars (intro, build, drop, break, drop, outro) with locators. Mix it so the drop is the loudest section with nothing clipping, check every clip and the levels per section, then save the Set as "My House Track" in my Music folder. Don't ask me questions.

---

## Tool reference

71 tools.

| Category | Tools |
|---|---|
| Session and transport | `get_session_info`, `get_session_snapshot`, `get_remote_script_info`, `set_tempo`, `start_playback`, `stop_playback`, `measure_levels`, `save_set` (Windows) |
| Tracks | `get_track_info`, `create_midi_track`, `create_audio_track`, `create_return_track`, `set_track_name`, `delete_track`, `delete_return_track` |
| Mixer | `get_mixer`, `set_track_volume`, `set_track_panning`, `set_send`, `set_track_mute`, `set_track_solo` |
| Clips and notes | `create_clip`, `create_audio_clip`, `add_notes_to_clip`, `write_drum_pattern`, `get_clip_notes`, `clear_notes_from_clip`, `modify_notes`, `delete_notes`, `set_clip_name`, `delete_clip`, `fire_clip`, `stop_clip` |
| Clip automation | `write_clip_automation`, `get_clip_automation`, `clear_clip_automation` |
| Devices and browser | `get_browser_tree`, `get_browser_items_at_path`, `search_browser`, `load_instrument_or_effect`, `load_drum_kit`, `get_device_parameters`, `set_device_parameter`, `get_drum_pads`, `get_nested_device_parameters`, `set_nested_device_parameter`, `delete_device`, `delete_nested_device` |
| Arrangement | `switch_to_arrangement_view`, `fill_arrangement`, `fill_arrangement_section`, `get_arrangement_layout`, `get_arrangement_clips`, `duplicate_to_arrangement`, `set_arrangement_clip_name`, `set_arrangement_loop`, `set_arrangement_time`, `set_locator`, `delete_locator` |
| Grooves | `get_groove_pool`, `load_groove`, `assign_groove`, `clear_clip_groove`, `set_groove_amount`, `set_groove_params` |
| Dataset (opt-in) | `set_dataset_consent`, `submit_intent`, `rate_last_action`, `prefer_candidate`, `reject_last_action`, `record_audition` |

Each tool's details are in [docs/CAPABILITIES.md](docs/CAPABILITIES.md).

---

## Known limitations

- **No audio export.** Live's API has no export, so File > Export Audio/Video stays manual.
- **No Arrangement-level automation.** The API can't write track automation in the Arrangement or envelopes on Arrangement clips. Workaround: automate a long Session clip, such as a 16-bar riser, and place it with `fill_arrangement`. The envelope travels with the clip and plays.
- **Grooves can't be removed.** Live rejects clearing a clip's groove, and pool grooves can't be deleted. `clear_clip_groove` assigns a neutral groove with all amounts at 0 instead. Live 12's default Set gives every new clip "Swing 16ths 66" with the global amount at 0%.
- **Claude can't hear the audio.** Meters show level, not tone or balance.
- **Meters are sampled, not true peak.** `measure_levels` polls Live's meters a few times per second (every 0.1 to 0.3 s, depending on the Set). Live's track meters also ignore solo, so read the master for what is actually heard.
- **60-second tool limit.** Claude Desktop drops a tool call after 60 s. `measure_levels` keeps measuring in the background and returns a `job_id`, and follow-up calls collect one combined result.
- **`save_set` is Windows only.** It automates Live's own Save dialog.

Remaining gaps and next steps are tracked in [docs/CAPABILITIES.md](docs/CAPABILITIES.md). The first full-track run and what it changed are in [docs/E2E_RUN_1.md](docs/E2E_RUN_1.md).

---

## Development

The full dev loop is in [docs/DEV_LOOP.md](docs/DEV_LOOP.md). In short:

```powershell
# Remote Script changed: install it, then fully quit and restart Live
powershell -ExecutionPolicy Bypass -File scripts\dev_install.ps1

# Check that Claude Desktop's server runs this checkout's current code
powershell -ExecutionPolicy Bypass -File scripts\check_fresh.ps1

# Unit tests (no Live needed)
uv run --with pytest pytest -q

# Selftests against a running Live, over the raw socket or through the MCP tools
uv run python scripts\ableton_cli.py selftest-notes
uv run python scripts\ableton_cli.py selftest-notes --via-server
```

`check_fresh.ps1` warns when any of these is true:
- the checkout isn't on `dev`;
- it has uncommitted changes;
- it differs from `origin/dev`;
- Claude Desktop's server started before the latest server change.

Every selftest works on throwaway tracks and removes them afterwards. A few things can't be undone through the API, such as grooves left in the pool; DEV_LOOP.md lists them.

**Current counts:**
- 71 MCP tools.
- Remote Script 1.11.1.
- 272 unit tests.
- 18 selftests. 17 run in the regular loop, each both ways. `selftest-save` is run deliberately on a new Set, because it really saves.

---

## Credits and license

Built on [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) by Siddharth Ahuja and its contributors, under the MIT License. The [LICENSE](LICENSE) file is unchanged.

Upstream pull requests merged into this fork or used as a reference:
- [#139](https://github.com/ahujasid/ableton-mcp/pull/139) Connection fixes, by Parker Rex (merged)
- [#122](https://github.com/ahujasid/ableton-mcp/pull/122) Mixer control, by Jorge Domingues Nunes (merged)
- [#140](https://github.com/ahujasid/ableton-mcp/pull/140) Main-thread command queue and output meters, by Parker Rex (merged)
- [#94](https://github.com/ahujasid/ableton-mcp/pull/94) `create_return_track`, by Luke Gimza (ported, with authorship kept)
- [#146](https://github.com/ahujasid/ableton-mcp/pull/146) Studio tools, by Roberto Antunes (reference for clip automation, arrangement filling, locators and grooves)
- [#99](https://github.com/ahujasid/ableton-mcp/pull/99) Extended Live API coverage, by Zachary May (reference for note modification)

**Data:** dataset recording (prompts, MIDI, names and device settings) is off by default and only turns on if you opt in. In a clone of this repo, the Supabase settings file (`MCP_Server/config.py`) is not included, so anonymous usage telemetry is off too. See [TERMS.md](TERMS.md) for what each tier would collect.

This is a third-party integration, not made by or affiliated with Ableton.
