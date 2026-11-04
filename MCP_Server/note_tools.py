"""Compact note writing: pure helpers the MCP tools expand into Live notes.

Writing notes as one JSON object per note costs the model ~70 characters a
note; a full track is hundreds of notes. These helpers let a tool take
compact arrays, a one-bar pattern to repeat, or drum step strings, and turn
them into the plain note dicts the Remote Script's add_notes_to_clip expects.
Nothing here talks to Live.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FLATS = {"DB": "C#", "EB": "D#", "GB": "F#", "AB": "G#", "BB": "A#"}
MAX_NOTES = 20000


def normalize_note(note: Any, index: int = 0) -> dict:
    """One note from a dict, or from [pitch, start, duration, velocity(, mute)]."""
    if isinstance(note, dict):
        pitch = note.get("pitch")
        start = note.get("start_time", note.get("start"))
        duration = note.get("duration", 0.25)
        velocity = note.get("velocity", 100)
        mute = bool(note.get("mute", False))
    elif isinstance(note, (list, tuple)) and 4 <= len(note) <= 5:
        pitch, start, duration, velocity = note[:4]
        mute = bool(note[4]) if len(note) == 5 else False
    else:
        raise ValueError(f"note {index}: expected [pitch, start, duration, velocity] or an object, got {note!r}")
    try:
        pitch, start, duration, velocity = int(pitch), float(start), float(duration), float(velocity)
    except (TypeError, ValueError):
        raise ValueError(f"note {index}: pitch/start/duration/velocity must be numbers, got {note!r}")
    if not 0 <= pitch <= 127:
        raise ValueError(f"note {index}: pitch {pitch} outside 0..127")
    if start < 0:
        raise ValueError(f"note {index}: start {start} is before beat 0")
    if duration <= 0:
        raise ValueError(f"note {index}: duration must be > 0, got {duration}")
    if not 1 <= velocity <= 127:
        raise ValueError(f"note {index}: velocity {velocity} outside 1..127")
    return {"pitch": pitch, "start_time": start, "duration": duration,
            "velocity": velocity, "mute": mute}


def normalize_notes(notes: Iterable[Any]) -> list[dict]:
    return [normalize_note(n, i) for i, n in enumerate(notes)]


def tile_notes(notes: list[dict], every: float, until: float) -> list[dict]:
    """Repeat a pattern every `every` beats up to (not including) `until`.
    Notes of the pattern must start inside [0, every)."""
    if every <= 0:
        raise ValueError("repeat_every_beats must be > 0")
    late = [n for n in notes if n["start_time"] >= every - 1e-9]
    if late:
        raise ValueError(f"{len(late)} note(s) start at or after repeat_every_beats={every:g}; "
                         f"the pattern must fit in one repeat")
    out, k = [], 0
    while k * every < until - 1e-9:
        for n in notes:
            start = n["start_time"] + k * every
            if start < until - 1e-9:
                out.append(dict(n, start_time=start))
        k += 1
        if len(out) > MAX_NOTES:
            raise ValueError(f"more than {MAX_NOTES} notes after repeating; check the lengths")
    return out


def note_name(note: int) -> str:
    """Live's octave numbering: 36 = C1, 60 = C3."""
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 2}"


def parse_note_name(text: str) -> int | None:
    m = re.fullmatch(r"\s*([A-Ga-g])([#b]?)(-?\d+)\s*", text)
    if not m:
        return None
    name = m.group(1).upper() + m.group(2).upper()
    name = _FLATS.get(name, name)
    if name not in NOTE_NAMES:
        return None
    value = NOTE_NAMES.index(name) + (int(m.group(3)) + 2) * 12
    return value if 0 <= value <= 127 else None


def resolve_pad(key: str, pads: list[dict]) -> int:
    """A pad note from a name ("Kick", case-insensitive; a unique substring is
    fine), a MIDI number ("36") or a note name ("C1"). Pads are the entries
    of get_drum_pads."""
    text = str(key).strip()
    lowered = text.lower()
    exact = [p for p in pads if p["name"].strip().lower() == lowered]
    if len(exact) == 1:
        return exact[0]["note"]
    partial = [p for p in pads if lowered in p["name"].strip().lower()]
    if len(exact) > 1 or len(partial) > 1:
        names = ", ".join(f"{p['name']} ({p['note']})" for p in (exact or partial))
        raise ValueError(f"'{key}' matches several pads: {names}; use the exact name or the note number")
    if len(partial) == 1:
        return partial[0]["note"]
    number = int(text) if text.isdigit() else parse_note_name(text)
    if number is not None and any(p["note"] == number for p in pads):
        return number
    available = ", ".join(f"{p['name']} ({p['note']}/{p['note_name']})" for p in pads)
    raise ValueError(f"No pad '{key}' in this kit. Pads: {available}")


def parse_steps(spec: Any, base: int, accent: int, ghost: int) -> tuple[list[tuple[int, int]], int]:
    """((step index, velocity) for each hit, number of steps).

    spec is a string, one char per step:
      x hit at `base` velocity, X accent, o ghost, 1-9 velocity 14..127,
      . - _ rest; spaces and | are ignored (use them to mark beats/bars).
    or {"steps": "x.x.", "velocities": [110, 0, 90, 0]} for exact per-step
    velocities (0 = rest; a list entry overrides the char).
    """
    velocities = None
    if isinstance(spec, dict):
        velocities = spec.get("velocities")
        spec = spec.get("steps", "")
    if not isinstance(spec, str):
        raise ValueError(f"step pattern must be a string, got {spec!r}")
    steps = [ch for ch in spec if ch not in " |"]
    hits = []
    for i, ch in enumerate(steps):
        if ch in ".-_":
            vel = 0
        elif ch == "x":
            vel = base
        elif ch == "X":
            vel = accent
        elif ch == "o":
            vel = ghost
        elif ch.isdigit() and ch != "0":
            vel = round(int(ch) * 127 / 9)
        else:
            raise ValueError(f"step {i}: unknown character {ch!r} (use x X o 1-9 . -)")
        if velocities is not None and i < len(velocities):
            vel = int(velocities[i])
        if vel:
            if not 1 <= vel <= 127:
                raise ValueError(f"step {i}: velocity {vel} outside 1..127")
            hits.append((i, vel))
    return hits, len(steps)


def drum_pattern_notes(pattern: dict, pads: list[dict], step_beats: float, until: float,
                       base: int = 100, accent: int = 127, ghost: int = 60,
                       gate: float | None = None) -> tuple[list[dict], list[dict]]:
    """Notes for every pad row, each row repeated to fill `until` beats.
    Returns (notes, per-row summary)."""
    if step_beats <= 0:
        raise ValueError("step_beats must be > 0")
    duration = gate if gate is not None else step_beats
    notes, summary = [], []
    for key, spec in pattern.items():
        pitch = resolve_pad(key, pads)
        hits, n_steps = parse_steps(spec, base, accent, ghost)
        if n_steps == 0:
            raise ValueError(f"'{key}': empty step pattern")
        row = [{"pitch": pitch, "start_time": i * step_beats, "duration": duration,
                "velocity": float(v), "mute": False} for i, v in hits]
        tiled = tile_notes(row, n_steps * step_beats, until) if row else []
        notes += tiled
        summary.append({"row": key, "pitch": pitch, "note_name": note_name(pitch),
                        "steps": n_steps, "hits_per_repeat": len(row), "notes": len(tiled)})
    return notes, summary


def compact(notes: list[dict]) -> list[list]:
    """[pitch, start, duration, velocity] (+ mute when true), sorted by time."""
    rows = []
    for n in sorted(notes, key=lambda n: (n["start_time"], n["pitch"])):
        row = [n["pitch"], round(n["start_time"], 4), round(n["duration"], 4), round(n["velocity"])]
        if n.get("mute"):
            row.append(True)
        rows.append(row)
    return rows
