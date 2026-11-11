# End-to-end run 1: full house track, post-mortem

Run date: 2026-11-04. `dev` at `ae832e2` (package 1.4.5, Remote Script 1.10.0), Live 12.

> **Status:** all ranked gaps except audio export were addressed afterwards; see [CAPABILITIES.md](CAPABILITIES.md).

**Brief:** build a complete, original 124 BPM minor-key house track in an empty Set
with no manual steps and no questions. It had to include drums (Drum Rack), bass,
chords and a hook; a reverb return; a master limiter; Session clips per section;
swing on the hats; a filter sweep on the build; a ~64-bar arrangement with
locators; a mix where the drop is loudest; and a self-check.

**Outcome:** the track was finished and passed every self-check: a 64-bar arrangement,
5 locators, no unintended gaps or overlaps, drum notes all on real pads, pitched notes
all in F minor, nothing muted, and meters peaking at ~0.85 with nothing clipping.
**But the run was done from Claude Code, not Claude Desktop, and it leaned on Bash and
`scripts/ableton_cli.py` for the heaviest steps.** A Claude Desktop user has only the
MCP tools, so every item in §1 is a gap. The Set was also left unsaved (no API).

## 1. Actions that did not go through the MCP tools

| # | What | How it was done | Why not MCP |
|---|------|-----------------|-------------|
| A | **Wrote all note data: 12 clips, 782 notes** (drums 421, bass 87, chords 208, hook 66). Later re-wrote the 28-note build snare roll | Python generated the notes into JSON. A script imported `ableton_cli.Client` and sent `add_notes_to_clip` over the raw socket | Writing 782 notes as tool-call arguments is ~55 KB of JSON at ~70 chars per note (~18k output tokens). The largest clip (Drums Drop, 147 notes) alone is ~11 KB. **This was not attempted through the MCP**, so this is a cost and reliability judgement, not a measured failure. See §2.1 |
| B | **Named all 12 clips** | Same script, `set_clip_name` | Convenience: it was already in the loop. MCP `set_clip_name` works, but costs 12 extra calls |
| C | **Measured levels: peaks per section during playback** | Scripts polled `get_session_info` and `get_track_info` ×4 in a tight loop for 40–130 s, binning by `current_song_time`. Four runs, several hundred commands each | No MCP tool samples meters over a time window. One MCP round trip (plus model latency) is seconds, so Claude can take a handful of samples per section at best. Those samples are also skewed by stale held peaks (§2.4) |
| D | **Verified every clip after the build**: notes vs intent, pads exist, in key, no note past clip end, groove per clip, mute/solo | Script calling `get_drum_pads`, `get_track_info`, `get_clip_notes` and diffing against the generated JSON | Possible via MCP, but it needs ~25 calls returning ~100 KB of verbose JSON, then Claude diffing it by eye. No bulk or compact read-back exists |
| E | **Filtered the Bass preset list** | `grep` over the saved tool result | `get_browser_items_at_path("sounds/Bass")` returned 59 KB and overflowed the tool-result limit. There is no search or filter parameter |
| F | Waited for background playback scripts | Harness `Monitor` / background Bash | Only needed because C ran as a script |

Everything else did go through the MCP: tempo, tracks, devices, drum pads, clips, groove,
automation, sends, arrangement, locators, levels and transport. Those worked well;
`fill_arrangement_section` and `get_arrangement_layout` in particular were reliable.

## 2. Root causes and fixes

### 2.1 Large note payloads
**What the limits actually are:**
- Remote Script: `MAX_REQUEST_BYTES = 16 MiB`, a 10 s per-command deadline on Live's main thread and 8 commands per tick. A 150-note add is a few KB and finishes well inside the deadline.
- MCP server: a 15 s socket timeout for modifying commands.
- Tool schema: `notes` is an array of untyped objects (`additionalProperties: {}`). The verbose `{"pitch":..,"start_time":..,"duration":..,"velocity":..,"mute":false}` shape is the only form accepted, and nothing describes the keys to the model.
- **The binding limit is the model having to emit every note as tool-call JSON.** That is slow, burns the output-token budget, and grows the risk of a typo in note 112. Transport is not the bottleneck.

**So auto-chunking `add_notes_to_clip` would not help.** The fix is to make notes cheaper to express. These are all MCP-server-only changes, because they expand to the existing Remote Script command:
1. **Compact note form**: accept `notes` as `[[pitch, start, dur, vel], ...]` and keep the dict form for compatibility. That is about 3× fewer tokens. Type the schema.
2. **Tiling**: `repeat_every_beats`/`fill_to_length` on `add_notes_to_clip`. Write one bar and the server tiles it across the clip. Most of the drum and chord notes in this run were bar repeats.
3. **`write_drum_pattern`**: step strings per pad name, e.g. `{"Closed Hi Hat": "-x-x-x-x-x-x-x-x"}` plus velocity accents, resolved through `get_drum_pads`. This also fixes the "guess the pad note" risk at the source.
4. Optionally **`write_chord_progression`**: voicings plus one rhythm pattern.
5. **`create_clip(name=, notes=)`**: create, name and fill in one call.

Before building this, **measure the baseline**: have Claude Desktop write the 147-note Drums Drop clip through the current tool and record whether it succeeds and how long it takes.

### 2.2 Device parameters on master and return tracks
- **Root cause:** `get_device_parameters` and `set_device_parameter` take only `track_index`, and the Remote Script indexes `self._song.tracks` only (`_get_device_parameters`). Loading and deleting devices already accept `track_type`; reading and setting parameters were never extended. So the master Limiter's ceiling and gain, and the Reverb's decay and mix, can't be read or set.
- **Second problem from the same call:** `_serialize_device(..., include_params=True)` recurses into rack chains. `get_device_parameters` on the 909 Drum Rack returned **504 KB**, which is useless as a tool result.
- **Fix:**
  - Add `track_type` ("track" | "return" | "master") to both commands.
  - Make get default to top-level parameters only, with `include_nested=false`.
  - Let set accept `parameter_name` as well as an index.
- **Effort:** small. Remote Script change, so Live restart; add to `SCRIPT_FEATURES`.

### 2.3 Grooves
**What happened:** the Set's pool already held "Swing 16ths 66", and **every new MIDI clip came back with it assigned**: drums, bass, chords and hook. `_create_clip` does not touch grooves, so this is Live or Set behaviour. The global groove amount was **0%**, so no swing played at all until it was raised. The likely source is an earlier session following the earlier advice in CAPABILITIES.md ("neutralise with `set_groove_amount(0)`"); the Set had been used before. **The cause was not confirmed during the run** (later traced to Live 12's default Set; see CAPABILITIES.md).

**Workaround used:**
- Loaded "Swing 16ths 54" and assigned it to the four drum clips.
- Set the 66 groove's timing to 0 so it does nothing on the other clips.
- Set the global amount to 100%.

This works, but it took three discoveries the tools didn't surface.

**What's possible and the fix:**
- **"No groove" equivalent:** Live rejects `clip.groove = None` and can't delete pool grooves. Ship a neutral groove file (e.g. `AbletonMCP Straight.agr`, all amounts 0) that the installer copies to User Library > Grooves. Add `clear_clip_groove(track, clip)` that loads it if needed and assigns it. A per-set neutral groove made by zeroing a library groove is fragile, because it changes every clip sharing that groove.
- **Surface state:**
  - Add `groove_amount` to `get_session_info`.
  - Have `create_clip` return the clip's groove.
  - Have `assign_groove` warn when the global amount is 0 ("assigned, but global groove amount is 0%, so nothing will swing").
- **Docs:** stop recommending `set_groove_amount(0)` as the way to neutralise a groove. It silently disables every groove in the Set for the next session.
- **Effort:** small to medium. Shipping the .agr is an installer change. The warnings and the `get_session_info` field mostly need the Remote Script, so Live restart.

### 2.4 Other workarounds, retries and awkward spots
1. **Consent prompt injected into `get_session_info`.** The first call's result ended with *"[Ask the user this now, before continuing. Do not answer it yourself… relay their reply verbatim.]"* (`MCP_Server/dataset/consent.py`). In an autonomous "don't ask me anything" run, a compliant agent has two bad options: stop and break the brief, or ignore a tool instruction. Imperative text inside tool output is also exactly the pattern agents are trained to treat as prompt injection, so a careful agent may distrust it. The agent ignored it and recording stayed off.
   - **Fix:** ask for consent through an explicit, once-per-install path, such as an installer step, a dedicated `dataset_consent_status` tool or the server's instructions. Or phrase it as information ("Dataset recording is off; the user can opt in with set_dataset_consent") and never append it to an unrelated read.
   - **Effort:** small, server only.
2. **No windowed level measurement.** See C.
   - **Fix:** `measure_levels(start_bar, end_bar)`. It moves the playhead, plays, discards the first ~3 s (Live holds peaks up to ~3 s, per `selftest-meters`), samples all track, return and master meters at ~10 Hz, stops, and returns peak and mean per track per locator section.
   - It can be built server-side today as a loop over existing commands, but a lightweight Remote Script `get_meters` (every track's meter in one call, no clip slots) would make it cheap. Today `get_track_info` returns all 8 clip slots on every poll.
   - **Stale meters caused a real misreading:** one pass showed the Hook track at 0.744 during the Build, where it has no clips, and put the Build's master peak above the Drop's.
3. **`set_arrangement_time` reports a stale position.** It returned "Playhead moved to beat 240.0" when asked for 136, and "256.4" when asked for 136 later. `current_song_time` is read back in the same callback, before Live applies the move. This is the same Live behaviour documented for locators. It cost one misdiagnosed playback run.
   - **Fix:** report the requested time and say it applies on the next tick, or re-read it in a second command like `set_locator` does.
   - **Effort:** small, server only.
4. **Arrangement clips are copies.** After editing the Session "Drums Build" clip, the Arrangement still played the old notes until `fill_arrangement(replace=true)` was run. Nothing warns about this.
   - **Fix:** document it in `add_notes_to_clip`, `modify_notes` and `delete_notes`. Optionally add `refresh_arrangement(track, clip)` that re-places every Arrangement copy of a Session clip by name and position.
   - **Effort:** small, server only.
5. **Browser listings are unbounded.** `sounds/Bass` returned 59 KB and overflowed. `drums` was ~25 KB.
   - **Fix:** `search_browser(query, category, limit=50)`, or `filter` and `limit` on `get_browser_items_at_path`. Filter server-side.
   - **Effort:** small, server only.
6. **No bulk or compact read-back for verification.** See D.
   - **Fix:** `get_clip_notes(compact=true)` returning `[[p,s,d,v],...]`. Also `check_clips(key=, scale=)` reporting per clip: note count, out-of-key pitches, drum notes with no pad, notes past the clip end, muted notes, groove, and track mute/solo.
   - **Effort:** small to medium, server only.
7. **`delete_track` with `expected_name` and auto-named tracks.** Inserting two MIDI tracks renamed "3-Audio" and "4-Audio" to "5-Audio" and "6-Audio", so the guard refused the names read seconds earlier. The guard behaved correctly, but it took a retry.
   - **Fix:** mention in the tool text that Live's auto-names change when tracks are inserted, and re-read names first.
8. **Call count for routine steps.** Making 12 named, filled clips needs 36 calls (create, add, name). Raising 4 clips' groove needs 4 calls. Both are covered by 2.1(5) and a `clip_indexes` list on `assign_groove`.
9. **Saving.** As already documented, Live's API has no save or export, so the run ended with a manual Ctrl+S. That still blocks "zero manual steps".

## 3. Gaps ranked by impact on "Claude Desktop builds a full track with zero manual steps"

| Rank | Gap | Impact | Effort | Live restart? |
|------|-----|--------|--------|---------------|
| 1 | Note writing is too verbose: no compact form, tiling, drum-step or create-with-notes tool (2.1) | **Blocking in practice.** Writing a full track's notes as tool JSON is the step most likely to stall or drift in Desktop | Medium | No (server only) |
| 2 | Consent prompt appended to `get_session_info` (2.4.1) | **High.** It instructs the agent to stop and ask on the first read of every autonomous run | Small | No |
| 3 | No windowed level measurement; stale held peaks; heavy `get_track_info` polling (C, 2.4.2) | **High.** "Mix and check meters" can't be done reliably from Desktop | Medium (server loop); small extra for `get_meters` | No for the loop; yes for `get_meters` |
| 4 | Device params on master and returns; recursive 500 KB device dumps (2.2) | **High.** Can't verify or set limiter or reverb, and a drum-rack read blows the context | Small | Yes |
| 5 | Grooves: inherited groove on new clips, global amount hidden at 0%, no "no groove" (2.3) | **Medium–High.** Swing silently does nothing, or is applied to everything | Small–Medium | Yes (plus installer for the .agr) |
| 6 | No bulk or compact clip read-back and checks (D, 2.4.6) | **Medium.** The self-check is possible but costs ~25 calls and ~100 KB | Small–Medium | No |
| 7 | Unbounded browser listings (E, 2.4.5) | **Medium.** The obvious way to pick a bass preset fails | Small | No |
| 8 | Arrangement copies silently go stale after Session edits (2.4.4) | **Medium.** A fix applied in Session view doesn't reach the song | Small | No |
| 9 | `set_arrangement_time` stale readback (2.4.3) | **Low–Medium.** Misleads verification | Small | No |
| 10 | Call count for routine steps (2.4.8) | **Low.** Slow, not blocking | Small | No |
| 11 | `delete_track` auto-name drift (2.4.7) | **Low** | Small (docs) | No |
| — | Saving or exporting the Set (2.4.9) | **Blocking**, but no LOM API exists; needs UI automation outside Live | Large / out of scope | — |

**Planned follow-up:**
- Ranks 1, 2, 6, 7, 8, 9 and 10 are all server-only, so they can be built and unit-tested without restarting Live.
- Then batch the Remote Script changes into one install and restart: device params by `track_type` and shallow dumps, `get_meters`, groove state fields and warnings.
- After that, re-run this same brief **in Claude Desktop** as E2E run 2 and record every non-MCP step (there should be none).
