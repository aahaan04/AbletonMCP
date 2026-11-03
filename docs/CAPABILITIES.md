# AbletonMCP capabilities audit

Snapshot of `dev` as of 2026-11-03 (package 1.4.5, Remote Script 1.7.1), measured
against the goal: Claude produces a full track in Live end to end with no manual steps.

## 1. What the MCP can do today

37 MCP tools (`MCP_Server/server.py`). Session-view clips are addressed by
`(track_index, clip_index)`; only regular tracks are reachable (no returns/master
except where noted).

**Session / transport**
- `get_session_info` — tempo, time signature, track/return counts, master volume/pan, transport and loop state.
- `get_remote_script_info` — Remote Script version + capability handshake.
- `get_session_snapshot` — full dump: tracks, mixer values, devices (+params), session and arrangement clips, notes.
- `set_tempo` — set song BPM.
- `start_playback` / `stop_playback` — global transport.

**Tracks**
- `get_track_info` — name, type, mute/solo/arm, volume/pan (read only), clip slots, top-level devices.
- `create_midi_track` / `create_audio_track` — insert a track at an index.
- `set_track_name` — rename a track.

**Clips and notes**
- `create_clip` — empty MIDI clip in an empty slot.
- `create_audio_clip` — import an audio file into an empty audio slot (Live ≥ 12.0.5).
- `add_notes_to_clip` — append notes (pitch, start, duration, velocity, mute).
- `get_clip_notes` — read every note of a Session clip.
- `clear_notes_from_clip` — remove every note (pairs with add for read → modify → replace).
- `set_clip_name` — rename a Session clip.
- `delete_clip` — delete a Session clip.
- `fire_clip` / `stop_clip` — launch / stop a Session clip.

**Devices and browser**
- `get_browser_tree` — browser category tree.
- `get_browser_items_at_path` — list items under a browser path.
- `load_instrument_or_effect` — load a browser item by URI onto a track.
- `load_drum_kit` — load a Drum Rack, then the first kit found at a browser path.
- `get_device_parameters` — params (name, value, min, max) of a top-level track device.
- `set_device_parameter` — set one param of a top-level track device by index.

**Arrangement**
- `switch_to_arrangement_view` — show Arrangement.
- `set_arrangement_time` — move the playhead.
- `get_arrangement_clips` — list a track's arrangement clips.
- `duplicate_to_arrangement` — copy a Session clip to a beat position in Arrangement.
- `set_arrangement_clip_name` — rename an arrangement clip.
- `create_locator` — named cue point at a beat (upstream PR 146 reports this fails in practice; see §3).

**Dataset / preference recording (opt-in, Supabase)**
- `set_dataset_consent`, `submit_intent`, `rate_last_action`, `prefer_candidate`,
  `reject_last_action`, `record_audition` — trajectory/preference logging; no effect on Live.

Remote Script commands with no MCP tool: `map_rack_magnitude`, `inspect_rack`,
`get_browser_item`, `get_browser_categories`, `get_browser_items`, `drain_passive_events`.

## 2. Gaps vs. the Live Object Model, prioritised for "full track, no manual steps"

| # | Gap | Why it blocks a finished track | LOM surface | Upstream coverage |
|---|-----|------|-------------|------|
| 1 | **Mixer write**: volume, pan, mute, solo, sends; return tracks; master | Can build parts but cannot balance them; no reverb/delay buses | `track.mixer_device.volume/panning/sends`, `track.mute/solo`, `song.return_tracks`, `song.master_track`, `song.create_return_track` | #122 (vol/pan/sends, tracks+returns+master, dB-exact), #146 (`set_track_mixer`: + mute/solo) |
| 2 | **Drum Rack pad access**: pad names/notes, per-pad chains, devices and params | Beats are written blind (which note is the snare?); no per-drum tone shaping | `RackDevice.drum_pads[n].name/note/chains`, `chain.devices[i].parameters` | #146 `get_drum_pads` (names + notes, nested racks); **per-pad devices/params: nobody** |
| 3 | **Delete track / device / arrangement clip** | No way to undo a bad choice; sets accumulate junk | `song.delete_track`, `track.delete_device`, `track.delete_clip` (arrangement) | #146 (all three), #110 (track + arrangement clips) |
| 4 | **Clip automation envelopes** (filter sweeps, volume rides) | Arrangement stays static; no builds/drops | `clip.automation_envelope(p)`, `create_automation_envelope`, `envelope.insert_step` | #146 (write points/ramps on Session clips); arrangement-track automation: nobody |
| 5 | **In-place note editing** (move/transpose/velocity on a subset; note IDs) | Workaround exists (read → clear → add), but loses note IDs and is chatty for big clips | `clip.apply_note_modifications`, `remove_notes_extended`, `get_notes_extended` IDs | #99 `apply_note_modifications` (conflicting), #146 transforms server-side then rewrites |
| 6 | **Track / master meters** | Can't check levels or clipping without a human listening | `track.output_meter_left/right/level` | #140 (meters in session info + snapshots), #119 `get_output_meter_levels` (conflicting) |
| 7 | **Grooves / swing** | Everything quantised-straight; human feel needs manual work | `song.groove_pool.grooves`, `clip.groove`, `groove.amount/timing/random/velocity` | #146 (groove-pool round trip + server-side swing/humanize) |

Also missing, lower priority: scenes (create/fire/name), clip loop/length edit,
track colour, routing (sidechain), undo/redo, device params inside racks, arrangement
recording/render. #99 and #146 cover most of these.

**Top 3 next features: 1) mixer + returns, 2) Drum Rack pads (incl. per-pad devices),
3) deletes (track/device/arrangement clip).** Those three plus today's tools are enough
to build, balance, and iterate on a beat without touching Live. Automation (#4) is the
next step after that.

## 3. Open upstream PRs that implement these gaps

From `gh pr list --repo ahujasid/ableton-mcp` (33 open). Upstream base is `9dddc7b`,
same as our `main`. None have maintainer reviews.

| PR | Adds | Gaps hit | Mergeable | Verdict |
|----|------|----------|-----------|---------|
| [#139](https://github.com/ahujasid/ableton-mcp/pull/139) | Connection fixes: per-command timeouts survive the receive loop (65 s audio import no longer dies at 15 s), fragmented UTF-8, close failed sockets. +147/-48, 10 tests | reliability | yes | **Safe, merge first.** Small, server-only, well-tested. |
| [#122](https://github.com/ahujasid/ableton-mcp/pull/122) | `get_mixer`, `set_track_volume` (dB via `str_for_value` bisection), `set_track_panning`, `set_send` for tracks/returns/master. +363/-2 | 1 (no mute/solo) | yes | **Safe, merge.** Small and additive. Bump Remote Script version + capability list when merging. |
| [#140](https://github.com/ahujasid/ableton-mcp/pull/140) | Run every command on Live's main thread via a bounded queue drained in `update_display`; cancel on timeout/disconnect; output meters in session info/snapshots; script → 1.7.2. +934/-383 | 6 + reliability | yes | **Worth it, review carefully.** Fixes real thread-safety bugs (reads currently run on socket threads) but rewrites dispatch, so it will conflict with every Remote Script PR. Test in Live 12 with the harness. |
| [#146](https://github.com/ahujasid/ableton-mcp/pull/146) | "Studio tools": `set_track_mixer`, `get_drum_pads`, `copy_clip`, `fill_arrangement`, `delete_arrangement_clips`, delete track/device, clip automation, groove pool, swing/humanize, audio render + analysis ("ears"), skills. Script → 1.8.0. +11153/-167 across 31 files | 1, 2, 3, 4, 5, 7 | yes | **Don't merge wholesale.** Covers the most gaps but is 11k unreviewed lines with new deps and skills. Best used as a reference to port pieces (mixer, drum pads, deletes) into small commits. Also independently makes the same re-handshake fix as ours. |
| [#119](https://github.com/ahujasid/ableton-mcp/pull/119) | Guarded mixer/device batch edits, `replace_clip_notes` with rollback, meters, capability probes. +4050/-164 | 1, 5, 6 | conflicts | Good ideas (fail-closed mutations); pins `mcp==1.4.0` and adds `config.py`. Port selectively. |
| [#110](https://github.com/ahujasid/ableton-mcp/pull/110) | `delete_track`, `delete_arrangement_clips`, `create_volume_fade`, batch arrangement duplication | 3, 4 (volume only) | conflicts | Small enough to port the delete handlers by hand. |
| [#99](https://github.com/ahujasid/ableton-mcp/pull/99) | ~60 commands: mixer, returns, scenes, routing, `apply_note_modifications`, undo/redo, quantize | 1, 5 | conflicts | Broad but stale; mine for `apply_note_modifications` / routing / undo. |
| [#84](https://github.com/ahujasid/ableton-mcp/pull/84), [#94](https://github.com/ahujasid/ableton-mcp/pull/94), [#41](https://github.com/ahujasid/ableton-mcp/pull/41), [#50](https://github.com/ahujasid/ableton-mcp/pull/50), [#23](https://github.com/ahujasid/ableton-mcp/pull/23) | Older mixer/scene/device/rack/delete attempts | 1, 3 | conflicts | Superseded by the above. |
| [#133](https://github.com/ahujasid/ableton-mcp/pull/133), [#143](https://github.com/ahujasid/ableton-mcp/pull/143) | Record/analyse Live's output; real-time resample export | "hearing" the mix | yes | Later: useful once mixing works. |

**Merge order suggestion:** #139 → #122 → (#140 after testing in Live 12) → port
drum pads + deletes from #146.
