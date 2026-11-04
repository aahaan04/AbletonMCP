# AbletonMCP capabilities audit

Snapshot of `dev` as of 2026-11-03 (package 1.4.5, Remote Script 1.9.0), measured
against the goal: Claude produces a full track in Live end to end with no manual steps.

## 1. What the MCP can do today

54 MCP tools (`MCP_Server/server.py`). Session-view clips are addressed by
`(track_index, clip_index)`. Mixer, device loading and device deletes also reach
return tracks and the master via `track_type`. Since upstream #140, every command
runs on Live's main thread through a bounded queue (64 pending, 8 per tick, 10 s
deadline; 60 s for audio import).

**Session / transport**
- `get_session_info` — tempo, time signature, track/return counts, master volume/pan and output meters, transport and loop state.
- `get_remote_script_info` — Remote Script version + capability handshake.
- `get_session_snapshot` — full dump: tracks, mixer values, devices (+params, nested chains), session and arrangement clips, notes.
- `set_tempo` — set song BPM.
- `start_playback` / `stop_playback` — global transport.

**Tracks**
- `get_track_info` — name, type, mute/solo/arm, volume/pan, output meters (0..1), clip slots, top-level devices.
- `create_midi_track` / `create_audio_track` — insert a track at an index.
- `create_return_track` — add a return at the end (every track gains a send to it).
- `set_track_name` — rename a track.
- `delete_track` — delete one track by index; optional `expected_name` guard.
- `delete_return_track` — delete one return track by index; optional `expected_name` guard. Note Live renames an auto-named return after its first device ("C-Return" → "C-Reverb").

**Mixer** (tracks, returns, master)
- `get_mixer` — volume, pan, sends (raw, dB, Live's display), mute, solo.
- `set_track_volume` — fader by exact dB (bisects Live's own readout) or raw value.
- `set_track_panning` — pan −1..1.
- `set_send` — one send level by dB or raw value.
- `set_track_mute` / `set_track_solo` — mute/solo buttons (not on master).

**Clips and notes**
- `create_clip` — empty MIDI clip in an empty slot.
- `create_audio_clip` — import an audio file into an empty audio slot (Live ≥ 12.0.5).
- `add_notes_to_clip` — append notes (pitch, start, duration, velocity, mute).
- `get_clip_notes` — read every note of a Session clip.
- `clear_notes_from_clip` — remove every note (pairs with add for read → modify → replace).
- `set_clip_name` — rename a Session clip.
- `delete_clip` — delete one Session clip, or one Arrangement clip with `view="arrangement"`.
- `fire_clip` / `stop_clip` — launch / stop a Session clip.

**Clip automation** (Session clips; times in beats from the clip start)
- `write_clip_automation` — draw an envelope from breakpoints (`value` in the parameter's units or `normalized` 0..1; linear or step), for mixer volume/pan/sends or any device parameter, including devices inside racks.
- `get_clip_automation` — sample an envelope at given beats; reports `has_envelope`.
- `clear_clip_automation` — remove one parameter's envelope, or all with `all_parameters=true`.
- **Arrangement limitation (verified in Live 12.4):** clip envelopes cannot be created on Arrangement clips; Live refuses with "Not a session clip or parameter belongs to another track". The LOM also has no API to write track-level Arrangement automation. Workaround: automate the Session clip, then `duplicate_to_arrangement` (the envelope travels with the clip).

**Devices, racks and browser**
- `get_browser_tree` — browser category tree.
- `get_browser_items_at_path` — list items under a browser path.
- `load_instrument_or_effect` — load a browser item by URI onto a track, return (`track_type="return"`) or the master.
- `load_drum_kit` — load a Drum Rack, then the first kit found at a browser path.
- `get_device_parameters` / `set_device_parameter` — params of a top-level track device.
- `get_drum_pads` — filled Drum Rack pads: MIDI note, note name (36 = C1), pad name, chain index, devices per pad. Tool text tells Claude to call it before writing drum notes.
- `get_nested_device_parameters` / `set_nested_device_parameter` — a device inside any rack (Drum/Instrument/Effect), addressed by `pad_note` or `chain_index` + `nested_device_index`, with `path` pairs to go deeper; set by parameter name or index, out-of-range refused.
- `delete_device` — delete one top-level device (track, return or master).
- `delete_nested_device` — delete one device inside a rack chain.

**Arrangement**
- `switch_to_arrangement_view` — show Arrangement.
- `set_arrangement_time` — move the playhead.
- `get_arrangement_clips` — list a track's arrangement clips.
- `duplicate_to_arrangement` — copy a Session clip to a beat position in Arrangement.
- `set_arrangement_clip_name` — rename an arrangement clip.
- `create_locator` — named cue point at a beat (upstream PR 146 reports this fails in practice; untested here).

**Dataset / preference recording (opt-in, Supabase)**
- `set_dataset_consent`, `submit_intent`, `rate_last_action`, `prefer_candidate`,
  `reject_last_action`, `record_audition` — trajectory/preference logging; no effect on Live.

Remote Script commands with no MCP tool: `map_rack_magnitude`, `inspect_rack`,
`get_browser_item`, `get_browser_categories`, `get_browser_items`, `drain_passive_events`.

### Done since the first audit

| Was gap | Now | How |
|---------|-----|-----|
| Mixer write (vol, pan, sends, mute, solo; returns; master) | ✅ | Upstream #122 merged + our mute/solo and handshake gating |
| Drum Rack pad access (names, notes, per-pad devices/params) | ✅ | `get_drum_pads`, nested get/set (any rack type) |
| Delete track / device / arrangement clip | ✅ | `delete_track`, `delete_return_track`, `delete_device`, `delete_nested_device`, `delete_clip(view=...)` |
| Connection robustness (long-command timeouts, split UTF-8, socket leaks) | ✅ | Upstream #139 merged |
| Create return tracks; devices on returns and master | ✅ | `create_return_track` ported from #94 (lukegimza); `load_instrument_or_effect(track_type=...)` |
| Clip automation envelopes | ✅ (Session clips) | `get/write/clear_clip_automation`; Arrangement not possible via the API, see above |
| Track / master meters | ✅ | Upstream #140 merged (also moves every command to Live's main thread) |

## 2. Remaining gaps, prioritised for "full track, no manual steps"

| # | Gap | Why it blocks a finished track | LOM surface | Upstream coverage |
|---|-----|------|-------------|------|
| 1 | **Arrangement building** (place clips across a song form, loop/extend sections, scenes → arrangement) | Today Claude can only copy one Session clip at a time to a beat; no song structure in one step, no section loops | `track.duplicate_clip_to_arrangement`, `song.loop_start/length`, scenes | #146 `fill_arrangement`, `set_arrangement_loop`, `copy_clip` |
| 2 | **In-place note editing** (move/transpose/velocity on a subset; note IDs) | Workaround works (read → clear → add) but loses note IDs and is chatty for big clips | `clip.apply_note_modifications`, `remove_notes_extended` | #99 `apply_note_modifications` (conflicting) |
| 3 | **Grooves / swing** | Everything quantised-straight; human feel needs manual work | `song.groove_pool.grooves`, `clip.groove` | #146 (groove-pool round trip + server-side swing) |
| 4 | **Routing / sidechain** | No sidechain pumping, no group buses | `track.output_routing_type`, `input_routing_type`, compressor sidechain params | #99 routing (conflicting), #146 sidechain |
| 5 | **Render / listen** | Meters show level, not tone; no bounce to check the mix | resample recording; file analysis | #143 resample export, #133 record/analyse |

Also missing, lower priority: scenes (create/fire/name), clip loop/length edit,
track colour, undo/redo.

**Top 3 next features: 1) arrangement building (fill a section from Session clips,
set loop), 2) in-place note editing, 3) grooves/swing.** Mixing, sound design and
automation now work; the remaining blocker for an end-to-end track is laying clips
out into a song form.

## 3. Open upstream PRs that implement these gaps

From `gh pr list --repo ahujasid/ableton-mcp` (33 open; merging into our `dev` does not
close them upstream). Upstream base is `9dddc7b`; none have maintainer reviews.

| PR | Adds | Gaps hit | Status here | Verdict |
|----|------|----------|-------------|---------|
| [#139](https://github.com/ahujasid/ableton-mcp/pull/139) | Connection fixes: timeouts, split UTF-8, socket cleanup | reliability | **merged into dev** | — |
| [#122](https://github.com/ahujasid/ableton-mcp/pull/122) | `get_mixer`, `set_track_volume` (dB), `set_track_panning`, `set_send` | mixer | **merged into dev** (+ mute/solo follow-up) | — |
| [#140](https://github.com/ahujasid/ableton-mcp/pull/140) | All commands on Live's main thread via a bounded queue; cancel on timeout/disconnect; output meters | meters + reliability | **merged into dev** (conflicts resolved by flattening our commands into its dispatcher) | — |
| [#94](https://github.com/ahujasid/ableton-mcp/pull/94) | `create_return_track`, `duplicate_track`, `capture_midi`, `tap_tempo`, `set_track_color` | returns | **`create_return_track` ported** (authored as lukegimza); rest not taken | `duplicate_track` / `set_track_color` are small future ports. |
| [#146](https://github.com/ahujasid/ableton-mcp/pull/146) | "Studio tools": `fill_arrangement`, `set_arrangement_loop`, `copy_clip`, groove pool, swing, sidechain, render + analysis. +11153/-167 | 1, 3, 4, 5 | mergeable upstream; commits authored as "Claude" | **Don't merge wholesale.** Clip automation here was written fresh using it as an API reference; do the same for arrangement fill and grooves. |
| [#119](https://github.com/ahujasid/ableton-mcp/pull/119) | Guarded mixer/device batch edits, `replace_clip_notes` with rollback | 2 | conflicts | Port ideas selectively. |
| [#99](https://github.com/ahujasid/ableton-mcp/pull/99) | ~60 commands incl. `apply_note_modifications`, routing, scenes, undo | 2, 4 | conflicts | Mine for note-modification and routing handlers. |
| [#110](https://github.com/ahujasid/ableton-mcp/pull/110), [#84](https://github.com/ahujasid/ableton-mcp/pull/84), [#41](https://github.com/ahujasid/ableton-mcp/pull/41), [#50](https://github.com/ahujasid/ableton-mcp/pull/50), [#23](https://github.com/ahujasid/ableton-mcp/pull/23) | Older mixer/delete/device attempts | — | conflicts | Superseded by what's on dev. |
| [#133](https://github.com/ahujasid/ableton-mcp/pull/133), [#143](https://github.com/ahujasid/ableton-mcp/pull/143) | Record/analyse Live's output; real-time resample export | "hearing" the mix | mergeable upstream | Later, after meters. |

**Next:** arrangement fill/loop (reference #146) → `apply_note_modifications`
(reference #99) → groove pool (reference #146).
