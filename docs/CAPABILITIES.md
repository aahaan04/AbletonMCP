# AbletonMCP capabilities audit

Snapshot of `dev` as of 2026-11-05 (package 1.4.5, Remote Script 1.11.1), measured
against the goal: Claude produces a full track in Live end to end with no manual steps.

## 1. What the MCP can do today

71 MCP tools (`MCP_Server/server.py`). Session-view clips are addressed by
`(track_index, clip_index)`. Mixer, device loading and device deletes also reach
return tracks and the master via `track_type`. Since upstream #140, every command
runs on Live's main thread through a bounded queue (64 pending, 8 per tick, 10 s
deadline; 60 s for audio import).

**Session / transport**
- `get_session_info` — tempo, time signature, track/return counts, master volume/pan and output meters, transport and loop state, global `groove_amount`.
- `measure_levels` — play for N seconds or `bars` (optionally from a bar), discard the first seconds of held peaks, return peak/mean/clipping per track, return and master, optionally per locator section. Uses the one-call `get_meters` command. Up to 600 s, so a whole song: anything over ~40 s (Claude Desktop drops a tool call after 60 s) keeps measuring in a background thread, and the call returns a `job_id`; `measure_levels(job_id=…)` waits up to 40 s more per call, and the last call returns one combined result (`calls` says how many it took). `cancel=true` stops early with what was measured. One measurement at a time. Note: Live's track meters ignore solo, so a soloed-out track still shows its level; read the master for what is actually heard.
- `save_set` — save the open Set (Windows): Ctrl+S, or Save As to a folder + name. UI automation of Live's own Save command; see the note under §2. A missing target folder is created only inside `~\Music` or `~\Documents`, and is removed again if the save fails; anywhere else it is refused before Live is touched.
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
- `create_clip` — MIDI clip in an empty slot; optionally `name` and `notes` (compact, with `repeat_every_beats`) in the same call; reports the groove Live assigned.
- `create_audio_clip` — import an audio file into an empty audio slot (Live ≥ 12.0.5).
- `add_notes_to_clip` — append notes as compact `[pitch, start, duration, velocity(, muted)]` arrays or objects; `repeat_every_beats` tiles a pattern across the clip; warns about notes past the clip end.
- `write_drum_pattern` — drums from step strings per pad (`{"Kick": "x...x...", "Hat": "o.x.o.X."}`), pads resolved by name, number or note name via `get_drum_pads`; accents, ghosts, 1-9 levels, exact per-step velocities; rows repeat to fill the clip.
- `get_clip_notes` — read every note of a Session clip (`compact=true`: `[pitch, start, duration, velocity]` rows, about 5x smaller).
- `clear_notes_from_clip` — remove every note (pairs with add for read → modify → replace).
- `set_clip_name` — rename a Session clip.
- `delete_clip` — delete one Session clip, or one Arrangement clip with `view="arrangement"`.
- `fire_clip` / `stop_clip` — launch / stop a Session clip.

**Clip automation** (Session clips; times in beats from the clip start)
- `write_clip_automation` — draw an envelope from breakpoints (`value` in the parameter's units or `normalized` 0..1; linear or step), for mixer volume/pan/sends or any device parameter, including devices inside racks.
- `get_clip_automation` — sample an envelope at given beats; reports `has_envelope`.
- `clear_clip_automation` — remove one parameter's envelope, or all with `all_parameters=true`.
- **Arrangement limitation (verified in Live 12.4):** clip envelopes cannot be created on Arrangement clips; Live refuses with "Not a session clip or parameter belongs to another track". The LOM also has no API to write track-level Arrangement automation. Workaround: automate the Session clip, then `fill_arrangement` / `duplicate_to_arrangement`; the envelope travels with the clip and plays (verified by playback), though it cannot be read back there.

**Devices, racks and browser**
- `get_browser_tree` — browser category tree.
- `get_browser_items_at_path` — list items under a browser path; `name_filter`, `limit` (default 100).
- `search_browser` — find instruments, presets, kits, effects and grooves by name words, with a result limit (searched inside Live).
- `load_instrument_or_effect` — load a browser item by URI onto a track, return (`track_type="return"`) or the master.
- `load_drum_kit` — load a Drum Rack, then the first kit found at a browser path.
- `get_device_parameters` / `set_device_parameter` — params of a top-level device on a track, return or the master (`track_type`); set by `parameter_name` or index, out-of-range refused; rack reads are shallow unless `include_nested=true` (909 kit: 171 KB → 2 KB), with `name_filter` and Live's display string per parameter. Many parameters are 0..1 internally while displaying s/Hz/dB.
- `get_drum_pads` — filled Drum Rack pads: MIDI note, note name (36 = C1), pad name, chain index, devices per pad. Tool text tells Claude to call it before writing drum notes.
- `get_nested_device_parameters` / `set_nested_device_parameter` — a device inside any rack (Drum/Instrument/Effect), addressed by `pad_note` or `chain_index` + `nested_device_index`, with `path` pairs to go deeper; set by parameter name or index, out-of-range refused.
- `delete_device` — delete one top-level device (track, return or master).
- `delete_nested_device` — delete one device inside a rack chain.

**Arrangement** (new tools work in bars: bar 1 = beat 0, ranges inclusive)
- `fill_arrangement` — repeat one Session clip back-to-back over a bar range on its own track; refuses partial repeats and clips in the way (`replace=true` removes only clips wholly inside the range).
- `fill_arrangement_section` — fill one section for several (track, clip) pairs in one call; all-or-nothing.
- `get_arrangement_layout` — per track: clips as bar ranges, gaps, overlaps; plus loop and locators.
- `set_arrangement_loop` — loop brace by bar range, on/off.
- `set_locator` / `delete_locator` — named section markers at a bar (playback must be stopped). Built from three Remote Script primitives sent as separate commands, because Live applies a playhead move only after the callback that made it; the old single-shot `create_locator` dropped cues at the previous playhead and is removed.
- `switch_to_arrangement_view`, `set_arrangement_time` (playhead, beats), `get_arrangement_clips`, `duplicate_to_arrangement` (one copy at a beat), `set_arrangement_clip_name`.
- Clip envelopes travel with `fill_arrangement` copies (verified by playback), but Live does not expose Arrangement clips' envelopes for reading or writing.

**Note editing**
- `modify_notes` — edit notes in place by `note_ids` or pitch/time range: absolute `pitch`/`velocity`/`start_time` or relative `transpose`/`velocity_delta`/`shift`, plus `duration`, `mute`. All-or-nothing validation; note IDs kept.
- `delete_notes` — delete notes by the same selection (empty selection refused).

**Grooves**
- `get_groove_pool` — grooves in the Set (timing/random/velocity/quantize) and global amount.
- `load_groove` — add a `.agr` from Core Library or User Library > Grooves, by URI or name; reuses one already in the pool.
- `assign_groove` — set the groove of one or several Session clips (`clip_indexes`); warns when the global amount is 0%; shown per clip in `get_track_info`.
- `clear_clip_groove` — make clips play straight by assigning "AbletonMCP No Groove" (Live's "Quantize 16" loaded once per Set with all amounts 0, then renamed).
- `set_groove_amount` (global 0–130%), `set_groove_params` (per groove).
- **Live 12 default Set (verified in `DefaultLiveSet.als` and in Live):** the Groove Pool holds "Swing 16ths 66" as the *default groove*, so every new clip gets it, and the global groove amount starts at **0%**, so nothing swings until it's raised. The API can't change the pool's default groove; `create_clip` reports the groove and `clear_clip_groove` removes it. `clip.groove = None` is rejected and pool grooves can't be deleted. Don't use `set_groove_amount(0)` to neutralise one clip: it silences every groove.

**Dataset / preference recording (opt-in, Supabase)**
- Recording is **off** unless the user opts in. No tool result asks about it; the option is mentioned once, as information, in the server's MCP instructions.
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
| Arrangement building (sections, loop, locators, layout read-back) | ✅ | `fill_arrangement(_section)`, `set_arrangement_loop`, `set/delete_locator`, `get_arrangement_layout` |
| In-place note editing | ✅ | `modify_notes`, `delete_notes` (by note id or range) |
| Grooves / swing | ✅ | Groove pool tools; `clear_clip_groove` via a neutral groove (pool grooves still can't be deleted) |
| Writing notes cheaply (E2E run 1, rank 1) | ✅ | Compact arrays, `repeat_every_beats`, `write_drum_pattern`, `create_clip(name, notes)` |
| Consent prompt in tool results (E2E rank 2) | ✅ | Removed; mentioned once in the server instructions |
| Windowed level measurement (E2E rank 3) | ✅ | `measure_levels` + `get_meters` |
| Device params on master/returns; 500 KB rack dumps (E2E rank 4) | ✅ | `track_type`, `parameter_name`, shallow reads |
| Groove inheritance / hidden 0% amount (E2E rank 5) | ✅ | Explained (Live 12 default Set), surfaced, `clear_clip_groove` |
| Browser limits, stale Arrangement copies, playhead read-back, call counts, auto-name drift (E2E ranks 7-11) | ✅ | `search_browser`/`name_filter`/`limit`, docs, `set_arrangement_time`, `create_clip(name, notes)` + `clip_indexes`, docs |
| Compact clip read-back and checks (E2E rank 6) | partly | `get_clip_notes(compact=true)`; no one-call `check_clips` yet (gap 6 below) |
| Saving the Set | ✅ (Windows) | `save_set`; export stays manual |

**About `save_set`:** Live's API has no save, so this is Windows UI automation. Live's
window is found by class and executable path; it's foregrounded without sending any
key; Ctrl+S is sent only after confirming Live is the foreground window; Save As fills
the dialog through `WM_SETTEXT` and presses its own Save button (no typing); existing
files are never overwritten; anything unexpected returns an error. Live saves a new Set
as `<folder>\<name> Project\<name>.als`. Missing folders are created only under the
user's Music or Documents folder (paths are resolved first, so `..` can't escape).
Not available on macOS.

## 2. Remaining gaps, prioritised for "full track, no manual steps"

| # | Gap | Why it matters for a finished track | LOM surface | Upstream coverage |
|---|-----|------|-------------|------|
| 1 | **Export audio** | Live's API has no export; the user still runs File > Export Audio/Video | none; UI automation of the export dialog is possible but riskier than Save | #143 records a bounce via resampling (real time), not a true export |
| 2 | **Long-form automation across clips** | Track automation in the Arrangement can't be written; envelopes live in clips, so a 16-bar riser needs a 16-bar clip | clip envelopes only | none |
| 3 | **Routing / sidechain / groups** | No sidechain pumping, no group buses, no resampling routes | `track.output_routing_type`, `input_routing_type`, compressor sidechain params | #99 routing (conflicting), #146 sidechain |
| 4 | **Hearing the result** | Meters show level, not tone or balance; Claude can't judge if the mix sounds right | resample recording + file analysis | #143 resample export, #133 record/analyse, #146 "ears" |
| 5 | **Clip properties** (loop/length, warp, transpose, gain) and **scenes** | Needed for audio loops and Session-to-Arrangement workflows | `clip.loop_start/end`, `warp_mode`, `pitch_coarse`, `song.create_scene` | #146 `set_clip_properties`, #99 scenes |
| 6 | **Verifying a whole track cheaply** | Per-clip read-back works but a full self-check is still many calls | server-side `check_clips(key=...)` over existing reads | none |

Also missing, lower priority: track colour, `duplicate_track`, undo/redo, a
`write_chord_progression` helper.

**Top 3 next features: 1) routing and sidechain, 2) clip properties + scenes,
3) `check_clips` for one-call verification.** Then re-run the E2E brief from Claude
Desktop as run 2 and record any step that isn't an MCP tool call.

## 3. Open upstream PRs that implement these gaps

From `gh pr list --repo ahujasid/ableton-mcp` (33 open; merging into our `dev` does not
close them upstream). Upstream base is `9dddc7b`; none have maintainer reviews.

| PR | Adds | Gaps hit | Status here | Verdict |
|----|------|----------|-------------|---------|
| [#139](https://github.com/ahujasid/ableton-mcp/pull/139) | Connection fixes: timeouts, split UTF-8, socket cleanup | reliability | **merged into dev** | — |
| [#122](https://github.com/ahujasid/ableton-mcp/pull/122) | `get_mixer`, `set_track_volume` (dB), `set_track_panning`, `set_send` | mixer | **merged into dev** (+ mute/solo follow-up) | — |
| [#140](https://github.com/ahujasid/ableton-mcp/pull/140) | All commands on Live's main thread via a bounded queue; cancel on timeout/disconnect; output meters | meters + reliability | **merged into dev** (conflicts resolved by flattening our commands into its dispatcher) | — |
| [#94](https://github.com/ahujasid/ableton-mcp/pull/94) | `create_return_track`, `duplicate_track`, `capture_midi`, `tap_tempo`, `set_track_color` | returns | **`create_return_track` ported** (authored as lukegimza); rest not taken | `duplicate_track` / `set_track_color` are small future ports. |
| [#146](https://github.com/ahujasid/ableton-mcp/pull/146) | "Studio tools": `copy_clip`, `set_clip_properties`, sidechain, render + analysis ("ears"), skills. +11153/-167 | 3, 4, 5 | mergeable upstream; commits authored as "Claude" | **Don't merge wholesale.** Clip automation, arrangement fill/loop and grooves here were written fresh using it as a reference; do the same for sidechain and clip properties. |
| [#119](https://github.com/ahujasid/ableton-mcp/pull/119) | Guarded mixer/device batch edits, `replace_clip_notes` with rollback | — | conflicts | Ideas only; note editing is done. |
| [#99](https://github.com/ahujasid/ableton-mcp/pull/99) | ~60 commands incl. routing, scenes, undo (its `apply_note_modifications` informed `modify_notes`) | 3, 5 | conflicts | Mine for routing and scene handlers. |
| [#110](https://github.com/ahujasid/ableton-mcp/pull/110), [#84](https://github.com/ahujasid/ableton-mcp/pull/84), [#41](https://github.com/ahujasid/ableton-mcp/pull/41), [#50](https://github.com/ahujasid/ableton-mcp/pull/50), [#23](https://github.com/ahujasid/ableton-mcp/pull/23) | Older mixer/delete/device attempts | — | conflicts | Superseded by what's on dev. |
| [#133](https://github.com/ahujasid/ableton-mcp/pull/133), [#143](https://github.com/ahujasid/ableton-mcp/pull/143) | Record/analyse Live's output; real-time resample export | "hearing" the mix | mergeable upstream | Later, after meters. |

**Next:** routing/sidechain (reference #99, #146) → clip properties and scenes
(reference #146, #99) → resample bounce (reference #143).
