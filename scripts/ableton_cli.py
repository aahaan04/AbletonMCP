"""Talk to the AbletonMCP Remote Script directly over its TCP socket.

Bypasses Claude Desktop and the MCP server so Remote Script features can be
tested on their own. Stdlib only, so plain `python` works; --via-server needs
the project env (`uv run`).

Every selftest builds what it needs on throwaway tracks/returns/devices and
deletes them again, so the open Set is left as it was found.

Examples (from the repo root):
    python scripts/ableton_cli.py ping
    python scripts/ableton_cli.py get_track_info track_index=0
    python scripts/ableton_cli.py add_notes_to_clip '{"track_index":0,"clip_index":0,"notes":[...]}'
    python scripts/ableton_cli.py selftest-notes
    uv run python scripts/ableton_cli.py selftest-notes --via-server
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from contextlib import contextmanager
from pathlib import Path

HOST = os.environ.get("ABLETON_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABLETON_PORT", "9877"))
TEMP_TRACK = "ableton_cli temp"
DRUM_KIT = "808 Core Kit.adg"
SELFTEST_NOTES = [
    {"pitch": 60, "start_time": 0.0, "duration": 0.5, "velocity": 100, "mute": False},
    {"pitch": 64, "start_time": 1.0, "duration": 0.5, "velocity": 90, "mute": False},
    {"pitch": 67, "start_time": 2.0, "duration": 1.0, "velocity": 80, "mute": False},
]


class CommandError(Exception):
    """The Remote Script answered with status=error."""


class Client:
    def __init__(self, host: str = HOST, port: int = PORT, timeout: float = 15.0):
        self.sock = socket.create_connection((host, port), timeout=5.0)
        self.sock.settimeout(timeout)

    def close(self) -> None:
        self.sock.close()

    def send(self, command_type: str, params: dict | None = None) -> dict:
        payload = {"type": command_type, "params": params or {}}
        self.sock.sendall(json.dumps(payload).encode("utf-8"))
        # The protocol has no framing: one JSON object per reply, so read
        # until the buffer parses.
        buf = b""
        while True:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("Remote Script closed the connection")
            buf += chunk
            try:
                response = json.loads(buf.decode("utf-8"))
                break
            except (ValueError, UnicodeDecodeError):
                continue
        if response.get("status") == "error":
            raise CommandError(response.get("message", "unknown error"))
        return response.get("result", {})


def parse_params(args: list[str]) -> dict:
    """Accept either one JSON object or key=value pairs (values JSON-decoded
    when possible, so track_index=0 is an int and name=Bass a string)."""
    if not args:
        return {}
    if len(args) == 1 and args[0].lstrip().startswith("{"):
        return json.loads(args[0])
    params = {}
    for arg in args:
        key, sep, value = arg.partition("=")
        if not sep:
            raise SystemExit(f"Expected key=value or a JSON object, got {arg!r}")
        try:
            params[key] = json.loads(value)
        except ValueError:
            params[key] = value
    return params


def cmd_ping(client: Client) -> int:
    info = client.send("get_script_info")
    print(f"Remote Script v{info.get('script_version')} "
          f"(protocol {info.get('protocol_version')}, port {info.get('port')})")
    print(f"capabilities ({len(info.get('capabilities', []))}): "
          + ", ".join(info.get("capabilities", [])))
    session = client.send("get_session_info")
    print(json.dumps(session, indent=2))
    return 0


# --------------------------------------------------------------------------
# Two ways to drive a selftest step: raw socket, or the MCP server's tools
# --------------------------------------------------------------------------

class RawOps:
    """Selftest steps straight over the socket, by Remote Script command name."""

    label = "raw socket"

    def __init__(self, client: Client):
        self.c = client

    def run(self, command, **params):
        return self.c.send(command, params)

    def load(self, track_index, uri, track_type="track"):
        return self.run("load_browser_item", track_index=track_index, item_uri=uri,
                        track_type=track_type)


class ServerOps:
    """Selftest steps through the MCP server's tool functions, in-process.

    No handshake is run first: that is the state the server is in when it was
    started before Live, which is what made clear_notes_from_clip refuse with
    "missing capability" against an up-to-date Remote Script.
    """

    label = "MCP server tools"

    def __init__(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        os.environ.setdefault("DISABLE_TELEMETRY", "1")
        os.environ.setdefault("ABLETON_MCP_DISABLE_DATASET", "1")
        import MCP_Server.server as server  # noqa: E402

        self.server = server

    def run(self, command, **params):
        """The MCP tool of the same name. Tools that return prose are checked
        for an error prefix; JSON is decoded."""
        import asyncio
        import inspect

        out = getattr(self.server, command)(None, **params)
        if inspect.iscoroutine(out):
            out = asyncio.run(out)
        if out.startswith(("Error", "Ableton Remote Script missing", "Failed")):
            raise CommandError(out)
        try:
            return json.loads(out)
        except ValueError:
            return {"message": out}

    def load(self, track_index, uri, track_type="track"):
        return self.run("load_instrument_or_effect", track_index=track_index, uri=uri,
                        track_type=track_type)


def make_ops(client: Client, via_server: bool):
    return ServerOps() if via_server else RawOps(client)


class Checker:
    def __init__(self):
        self.failures = 0

    def check(self, label, ok, detail=""):
        print(f"  {label:<26} {'ok' if ok else 'FAIL'}{f' ({detail})' if detail else ''}")
        if not ok:
            self.failures += 1

    def info(self, label, detail):
        print(f"  {label:<26} -- ({detail})")

    def finish(self):
        print("PASS" if not self.failures else f"FAIL ({self.failures})")
        return 1 if self.failures else 0


def _close(a, b, tol):
    return a is not None and abs(a - b) <= tol


# --------------------------------------------------------------------------
# Throwaway objects and browser lookups
# --------------------------------------------------------------------------

def track_names(client: Client) -> list[str]:
    return [client.send("get_track_info", {"track_index": i})["name"]
            for i in range(client.send("get_session_info")["track_count"])]


@contextmanager
def temp_track(client: Client, name: str = TEMP_TRACK):
    """A MIDI track at the end of the set, deleted on exit by its name."""
    index = client.send("create_midi_track", {"index": -1})["index"]
    client.send("set_track_name", {"track_index": index, "name": name})
    try:
        yield index
    finally:
        try:
            client.send("set_track_name", {"track_index": index, "name": name})
            client.send("delete_track", {"track_index": index, "expected_name": name})
        except CommandError as e:
            print(f"  WARNING: could not delete temp track {index} ({e})")


def browser_uri(client: Client, path: str, name: str) -> str:
    items = client.send("get_browser_items_at_path", {"path": path})["items"]
    hit = next((i for i in items if i["name"] == name), None)
    if hit is None:
        raise CommandError(f"{name!r} not found under browser path {path!r}")
    return hit["uri"]


def snapshot(client: Client) -> dict:
    return client.send("get_session_snapshot", {"include_notes": False, "include_params": False})


# --------------------------------------------------------------------------
# selftest-notes: create clip -> add notes -> read -> clear -> read empty
# --------------------------------------------------------------------------

def cmd_selftest_notes(client: Client, via_server: bool) -> int:
    ops = make_ops(client, via_server)
    c = Checker()
    with temp_track(client) as track:
        print(f"selftest-notes via {ops.label}: temp track {track}")
        client.send("create_clip", {"track_index": track, "clip_index": 0, "length": 4.0})
        try:
            ops.run("add_notes_to_clip", track_index=track, clip_index=0, notes=SELFTEST_NOTES)
            before = ops.run("get_clip_notes", track_index=track, clip_index=0)
            c.check("add + read", before["note_count"] == len(SELFTEST_NOTES),
                    f"{before['note_count']} notes")
            cleared = ops.run("clear_notes_from_clip", track_index=track, clip_index=0)
            c.check("clear", True, cleared.get("cleared_count", cleared.get("message")))
            after = ops.run("get_clip_notes", track_index=track, clip_index=0)
            c.check("read empty", after["note_count"] == 0 and after["notes"] == [], "notes == []")
        except CommandError as e:
            c.check("note commands", False, str(e))
    return c.finish()


# --------------------------------------------------------------------------
# selftest-connection: large multi-byte replies, then recovery after an error
# --------------------------------------------------------------------------

def cmd_selftest_connection(client: Client, via_server: bool) -> int:
    ops = make_ops(client, via_server)
    c = Checker()
    with temp_track(client) as track:
        print(f"selftest-connection via {ops.label}: temp track {track}")
        try:
            # >8 KiB of 2- and 4-byte characters, at both byte parities, so a
            # recv() boundary lands inside a character on at least one of them.
            for label, name in (("2-byte, even", "é" * 5000),
                                ("2-byte, odd", "x" + "é" * 5000),
                                ("4-byte", "🥁" * 2500)):
                client.send("set_track_name", {"track_index": track, "name": name})
                expected = client.send("get_track_info", {"track_index": track})["name"]
                got = ops.run("get_track_info", track_index=track)["name"]
                size = len(expected.encode("utf-8"))
                c.check(f"multi-byte reply {label}", got == expected and size > 8192, f"{size} bytes")
            try:
                ops.run("get_track_info", track_index=9999)
                c.check("error reply", False, "track 9999 did not error")
            except CommandError as e:
                c.check("error reply", True, str(e))
            c.check("command after error", ops.run("get_track_info", track_index=track)["index"] == track,
                    "reconnected")
        except CommandError as e:
            c.check("connection commands", False, str(e))
    return c.finish()


# --------------------------------------------------------------------------
# selftest-mixer: volume, pan, mute, solo, send -> read back
# --------------------------------------------------------------------------

def cmd_selftest_mixer(client: Client, via_server: bool) -> int:
    ops = make_ops(client, via_server)
    c = Checker()
    returns = client.send("get_session_info")["return_track_count"]
    with temp_track(client) as track:
        print(f"selftest-mixer via {ops.label}: temp track {track}, {returns} return track(s)")
        try:
            ops.run("set_track_volume", track_index=track, db=-6.0)
            ops.run("set_track_panning", track_index=track, value=0.5)
            ops.run("set_track_mute", track_index=track, mute=True)
            ops.run("set_track_solo", track_index=track, solo=True)
            if returns:
                ops.run("set_send", track_index=track, send_index=0, db=-12.0)
            m = ops.run("get_mixer", track_index=track)
            c.check("volume -6 dB", _close(m["volume"]["db"], -6.0, 0.05), m["volume"]["display"])
            c.check("pan 0.5", _close(m["panning"]["value"], 0.5, 1e-3), m["panning"]["display"])
            c.check("mute on", m["mute"] is True)
            c.check("solo on", m["solo"] is True)
            if returns:
                s = m["sends"][0]
                c.check("send A -12 dB", _close(s["db"], -12.0, 0.05), f"{s['name']}: {s['display']}")
            else:
                c.info("send", "skipped, no return tracks in this set")
        except CommandError as e:
            c.check("mixer commands", False, str(e))
        finally:
            # Unsolo before the track goes, so the rest of the set isn't left silenced.
            client.send("set_track_solo", {"track_index": track, "solo": False})
    return c.finish()


# --------------------------------------------------------------------------
# selftest-drums: stock kit -> list pads -> change pad parameters -> read back
# --------------------------------------------------------------------------

def _pick_param(params, preferred):
    """A writable, continuous parameter: the first preferred name present,
    else the first non-'Device On' one."""
    usable = [p for p in params if p["name"].strip() != "Device On"
              and p.get("is_enabled", True) and p["max"] > p["min"]]
    for name in preferred:
        hit = next((p for p in usable if p["name"].strip() == name), None)
        if hit:
            return hit
    return usable[0] if usable else None


def _roundtrip_param(c, ops, label, address, preferred):
    """Set a nested parameter to a new value, read it back."""
    params = ops.run("get_nested_device_parameters", **address)["device"]["parameters"]
    p = _pick_param(params, preferred)
    if p is None:
        c.check(label, False, "no writable parameter")
        return
    name, old = p["name"].strip(), p["value"]
    new = p["min"] + (p["max"] - p["min"]) * (0.25 if old > (p["min"] + p["max"]) / 2 else 0.75)
    if p.get("is_quantized"):
        new = float(round(new))
    ops.run("set_nested_device_parameter", value=new, parameter_name=name, **address)
    back = next(q for q in ops.run("get_nested_device_parameters", **address)["device"]["parameters"]
                if q["name"].strip() == name)
    c.check(label, _close(back["value"], new, 1e-4), f"{name}: {old:g} -> {back['value']:g}")


def cmd_selftest_drums(client: Client, via_server: bool) -> int:
    ops = make_ops(client, via_server)
    c = Checker()
    with temp_track(client) as track:
        kit = browser_uri(client, "drums", DRUM_KIT)
        client.send("load_browser_item", {"track_index": track, "item_uri": kit})
        print(f"selftest-drums via {ops.label}: temp track {track} with {DRUM_KIT}")
        try:
            pads = ops.run("get_drum_pads", track_index=track)
            listed = pads["pads"]
            names_ok = all(p["note_name"] == f"{'C C# D D# E F F# G G# A A# B'.split()[p['note'] % 12]}"
                                           f"{p['note'] // 12 - 2}" for p in listed)
            c.check("get_drum_pads", pads["pad_count"] > 0 and names_ok,
                    f"{pads['pad_count']} pads, "
                    + ", ".join(f"{p['note']}={p['note_name']} {p['name']}" for p in listed[:3]) + ", ...")

            pad = next((p for p in listed if p["note"] == 36), listed[0])
            address = {"track_index": track, "device_index": pads["device_index"],
                       "pad_note": pad["note"], "nested_device_index": 0}
            _roundtrip_param(c, ops, "pad macro", address, ("Decay", "Tone"))
            if pad["chains"][0]["devices"][0]["is_rack"]:
                _roundtrip_param(c, ops, "pad sampler (path)", dict(address, path=[0, 0]),
                                 ("Transpose", "Tune", "Decay"))

            empty = next(n for n in range(36, 128) if n not in {p["note"] for p in listed})
            try:
                ops.run("get_nested_device_parameters", **dict(address, pad_note=empty))
                c.check("empty pad refused", False, f"pad {empty} did not error")
            except CommandError as e:
                c.check("empty pad refused", "is empty" in str(e), str(e)[-30:])
        except CommandError as e:
            c.check("drum commands", False, str(e))
    return c.finish()


# --------------------------------------------------------------------------
# selftest-deletes: throwaway track with kit + clips and a return, delete all
# --------------------------------------------------------------------------

DELETE_TRACK = "ableton_cli delete me"


def _refused(ops, command, **params):
    try:
        ops.run(command, **params)
        return False
    except CommandError as e:
        return "Refusing to delete" in str(e)


def cmd_selftest_deletes(client: Client, via_server: bool) -> int:
    ops = make_ops(client, via_server)
    track = client.send("create_midi_track", {"index": -1})["index"]
    client.send("set_track_name", {"track_index": track, "name": DELETE_TRACK})
    kit_name = DRUM_KIT[:-4]
    client.send("load_browser_item", {"track_index": track, "item_uri": browser_uri(client, "drums", DRUM_KIT)})
    client.send("create_clip", {"track_index": track, "clip_index": 0, "length": 4.0})
    client.send("duplicate_session_clip_to_arrangement",
                {"track_index": track, "clip_index": 0, "destination_time": 0.0})
    print(f"selftest-deletes via {ops.label}: track {track} ({DELETE_TRACK!r}) "
          f"with {kit_name}, a session clip and an arrangement clip")

    c = Checker()
    try:
        n = client.send("get_arrangement_clips", {"track_index": track})["clip_count"]
        ops.run("delete_clip", track_index=track, clip_index=0, view="arrangement")
        left = client.send("get_arrangement_clips", {"track_index": track})["clip_count"]
        c.check("arrangement clip", n == 1 and left == 0, f"{n} -> {left}")

        ops.run("delete_clip", track_index=track, clip_index=0)
        slot = client.send("get_track_info", {"track_index": track})["clip_slots"][0]
        c.check("session clip", not slot["has_clip"], "slot 0 empty")

        pad_rack = {"track_index": track, "device_index": 0, "pad_note": 36, "nested_device_index": 0}
        inner = lambda: client.send("get_nested_device_parameters", pad_rack)["device"]["chains"][0]["devices"]
        before = [d["name"] for d in inner()]
        ops.run("delete_nested_device", **dict(pad_rack, path=[0, 0]))
        after = [d["name"] for d in inner()]
        c.check("nested device", len(after) == len(before) - 1, f"{before} -> {after}")

        c.check("wrong name refused", _refused(ops, "delete_device", track_index=track,
                                               device_index=0, expected_name="Not The Kit"))
        ops.run("delete_device", track_index=track, device_index=0, expected_name=kit_name)
        devices = client.send("get_track_info", {"track_index": track})["devices"]
        c.check("device", devices == [], f"{len(devices)} devices left")

        returns = client.send("get_session_info")["return_track_count"]
        created = client.send("create_return_track")
        name, index = created["name"], created["return_track_index"]
        c.check("return wrong name refused",
                _refused(ops, "delete_return_track", return_index=index, expected_name="__nope__"))
        ops.run("delete_return_track", return_index=index, expected_name=name)
        left = client.send("get_session_info")["return_track_count"]
        c.check("return track", left == returns, f"created {name!r}, {returns + 1} -> {left} returns")
    except CommandError as e:
        c.check("delete commands", False, str(e))
    finally:
        count = client.send("get_session_info")["track_count"]
        ops.run("delete_track", track_index=track, expected_name=DELETE_TRACK)
        names = track_names(client)
        c.check("track", len(names) == count - 1 and DELETE_TRACK not in names,
                f"{count} -> {len(names)} tracks")
    return c.finish()


# --------------------------------------------------------------------------
# selftest-returns: new return + Reverb, send to it, Limiter on master
# --------------------------------------------------------------------------

def cmd_selftest_returns(client: Client, via_server: bool) -> int:
    ops = make_ops(client, via_server)
    c = Checker()
    returns = client.send("get_session_info")["return_track_count"]
    master_devices = len(snapshot(client)["master_track"]["devices"])
    reverb = browser_uri(client, "audio_effects", "Reverb")
    limiter = browser_uri(client, "audio_effects", "Limiter")
    index = name = None
    limiter_loaded = False
    with temp_track(client) as track:
        print(f"selftest-returns via {ops.label}: temp track {track}, {returns} returns, "
              f"{master_devices} master devices")
        try:
            ops.run("create_return_track")
            index = returns
            name = client.send("get_mixer", {"track_index": index, "track_type": "return"})["name"]
            c.check("create_return_track", client.send("get_session_info")["return_track_count"] == returns + 1,
                    f"{name!r} at index {index}")

            ops.load(index, reverb, "return")
            ret = snapshot(client)["return_tracks"][index]
            on_return = [d["name"] for d in ret["devices"]]
            c.check("Reverb on return", on_return == ["Reverb"], str(on_return))
            # Live renames an auto-named return after its first device ("C-Return" -> "C-Reverb").
            name = ret["name"]

            ops.run("set_send", track_index=track, send_index=index, db=-10.0)
            s = client.send("get_mixer", {"track_index": track})["sends"][index]
            c.check("send to new return", _close(s["db"], -10.0, 0.05) and s["name"] == name,
                    f"{s['name']}: {s['display']}")

            ops.load(0, limiter, "master")
            limiter_loaded = True
            on_master = [d["name"] for d in snapshot(client)["master_track"]["devices"]]
            c.check("Limiter on master", len(on_master) == master_devices + 1 and on_master[-1] == "Limiter",
                    str(on_master))
        except CommandError as e:
            c.check("return/master commands", False, str(e))
        finally:
            if limiter_loaded:
                ops.run("delete_device", track_index=0, device_index=master_devices, track_type="master",
                        expected_name="Limiter")
            if name is not None:
                ops.run("delete_return_track", return_index=index, expected_name=name)
    snap = snapshot(client)
    c.check("cleaned up", len(snap["return_tracks"]) == returns
            and len(snap["master_track"]["devices"]) == master_devices,
            f"{len(snap['return_tracks'])} returns, {len(snap['master_track']['devices'])} master devices")
    return c.finish()


# --------------------------------------------------------------------------
# selftest-automation: rising filter sweep + volume fade, read back, clear
# --------------------------------------------------------------------------

def cmd_selftest_automation(client: Client, via_server: bool) -> int:
    ops = make_ops(client, via_server)
    c = Checker()
    with temp_track(client) as track:
        ops.load(track, browser_uri(client, "audio_effects", "Auto Filter"))
        client.send("create_clip", {"track_index": track, "clip_index": 0, "length": 32.0})
        params = client.send("get_device_parameters", {"track_index": track, "device_index": 0})
        param = _pick_param(params["device"]["parameters"], ("Frequency",))
        cutoff = {"device_index": 0, "parameter_name": param["name"].strip()}
        lo, hi = param["min"], param["max"]
        print(f"selftest-automation via {ops.label}: temp track {track}, Auto Filter "
              f"{cutoff['parameter_name']} ({lo:g}..{hi:g}), 8-bar clip")
        try:
            ops.run("write_clip_automation", track_index=track, clip_index=0, target=cutoff,
                    points=[{"time": 0, "normalized": 0.1}, {"time": 32, "normalized": 0.9}])
            got = ops.run("get_clip_automation", track_index=track, clip_index=0, target=cutoff,
                          times=[0, 8, 16, 24, 31.9])
            vals = [s["value"] for s in got["samples"]]
            expect = [lo + (hi - lo) * (0.1 + 0.8 * t / 32) for t in (0, 8, 16, 24, 31.9)]
            tol = (hi - lo) * 0.02
            c.check("cutoff sweep rises", got["has_envelope"] and vals == sorted(vals)
                    and all(_close(v, e, tol) for v, e in zip(vals, expect)),
                    ", ".join(s["display"] for s in got["samples"]))

            ops.run("write_clip_automation", track_index=track, clip_index=0, target={"mixer": "volume"},
                    points=[{"time": 0, "value": 0.85}, {"time": 16, "value": 0.0}])
            vol = ops.run("get_clip_automation", track_index=track, clip_index=0,
                          target={"mixer": "volume"}, times=[0, 8, 15.9, 24])
            vv = [s["value"] for s in vol["samples"]]
            c.check("volume fade falls", vv[0] > vv[1] > vv[2] >= vv[3] and _close(vv[3], 0.0, 1e-3),
                    ", ".join(s["display"] for s in vol["samples"]))

            ops.run("clear_clip_automation", track_index=track, clip_index=0, target=cutoff)
            after = ops.run("get_clip_automation", track_index=track, clip_index=0, target=cutoff)
            still = ops.run("get_clip_automation", track_index=track, clip_index=0, target={"mixer": "volume"})
            c.check("clear one envelope", not after["has_envelope"] and still["has_envelope"],
                    "cutoff gone, volume kept")
            ops.run("clear_clip_automation", track_index=track, clip_index=0, all_parameters=True)
            gone = ops.run("get_clip_automation", track_index=track, clip_index=0, target={"mixer": "volume"})
            c.check("clear all envelopes", not gone["has_envelope"])

            # Arrangement: report what Live allows rather than assume.
            client.send("duplicate_session_clip_to_arrangement",
                        {"track_index": track, "clip_index": 0, "destination_time": 0.0})
            try:
                ops.run("write_clip_automation", track_index=track, clip_index=0, target=cutoff,
                        view="arrangement", points=[{"time": 0, "normalized": 0.2}, {"time": 8, "normalized": 0.8}])
                arr = ops.run("get_clip_automation", track_index=track, clip_index=0, target=cutoff,
                              view="arrangement", times=[0, 4, 7.9])
                c.info("arrangement clip", "Live ALLOWED it: "
                       + ", ".join(s["display"] for s in arr.get("samples", [])))
            except CommandError as e:
                c.info("arrangement clip", f"Live refused: {str(e)[-90:]}")
        except CommandError as e:
            c.check("automation commands", False, str(e))
    return c.finish()


# --------------------------------------------------------------------------
# selftest-meters: kick loop playing -> meters up; stopped -> meters down
# --------------------------------------------------------------------------

def _peak_meters(ops, track, seconds=1.0, every=0.1):
    """Highest track and master output_meter_level seen over a short window."""
    t_peak = m_peak = 0.0
    end = time.time() + seconds
    while time.time() < end:
        t = ops.run("get_track_info", track_index=track).get("output_meter_level")
        m = ops.run("get_session_info")["master_track"].get("output_meter_level")
        if t is None or m is None:
            raise CommandError("output_meter_level missing from track/session info")
        t_peak, m_peak = max(t_peak, t), max(m_peak, m)
        time.sleep(every)
    return t_peak, m_peak


def _wait_for_silence(ops, track, timeout=5.0, threshold=0.01):
    """Seconds until both meters fall below threshold (Live holds the last
    peak for up to ~1 s after stopping), or None on timeout."""
    start = time.time()
    while time.time() - start < timeout:
        t = ops.run("get_track_info", track_index=track).get("output_meter_level") or 0.0
        m = ops.run("get_session_info")["master_track"].get("output_meter_level") or 0.0
        if t < threshold and m < threshold:
            return time.time() - start
        time.sleep(0.1)
    return None


def cmd_selftest_meters(client: Client, via_server: bool) -> int:
    ops = make_ops(client, via_server)
    c = Checker()
    with temp_track(client) as track:
        client.send("load_browser_item", {"track_index": track, "item_uri": browser_uri(client, "drums", DRUM_KIT)})
        client.send("create_clip", {"track_index": track, "clip_index": 0, "length": 4.0})
        kicks = [{"pitch": 36, "start_time": float(b), "duration": 0.25, "velocity": 127, "mute": False}
                 for b in range(4)]
        client.send("add_notes_to_clip", {"track_index": track, "clip_index": 0, "notes": kicks})
        print(f"selftest-meters via {ops.label}: temp track {track}, kick on every beat")
        quiet_t = quiet_m = 0.0
        try:
            quiet_t, quiet_m = _peak_meters(ops, track, 0.5)
            ops.run("fire_clip", track_index=track, clip_index=0)
            time.sleep(2.5)  # global launch quantization is 1 bar (2 s at 120 BPM)
            play_t, play_m = _peak_meters(ops, track, 1.5)
            c.check("track meter while playing", play_t > 0.05, f"peak {play_t:.3f}")
            c.check("master meter while playing", play_m > 0.05, f"peak {play_m:.3f}")
        except CommandError as e:
            c.check("meter commands", False, str(e))
        finally:
            client.send("stop_clip", {"track_index": track, "clip_index": 0})
            client.send("stop_playback")
        try:
            silent_after = _wait_for_silence(ops, track)
            playing = ops.run("get_session_info")["is_playing"]
            c.check("transport stopped", playing is False)
            c.check("meters fall to ~0", silent_after is not None,
                    "timed out after 5 s" if silent_after is None else f"< 0.01 after {silent_after:.1f} s")
            stop_t, stop_m = _peak_meters(ops, track, 0.5)
            c.check("meters stay at ~0", stop_t < 0.01 and stop_m < 0.01,
                    f"track {stop_t:.4f}, master {stop_m:.4f} (before play {quiet_t:.4f}, {quiet_m:.4f})")
        except CommandError as e:
            c.check("meter commands", False, str(e))
    return c.finish()


SELFTESTS = {
    "selftest-notes": cmd_selftest_notes,
    "selftest-connection": cmd_selftest_connection,
    "selftest-mixer": cmd_selftest_mixer,
    "selftest-drums": cmd_selftest_drums,
    "selftest-deletes": cmd_selftest_deletes,
    "selftest-returns": cmd_selftest_returns,
    "selftest-automation": cmd_selftest_automation,
    "selftest-meters": cmd_selftest_meters,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send commands straight to the AbletonMCP Remote Script socket.",
    )
    parser.add_argument("command", help="ping | " + " | ".join(SELFTESTS) + " | any Remote Script command type")
    parser.add_argument("params", nargs="*", help="JSON object or key=value pairs")
    parser.add_argument("--via-server", action="store_true",
                        help="selftests: drive the steps through MCP_Server tool functions")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)

    try:
        client = Client(args.host, args.port)
    except OSError as e:
        print(f"Could not connect to {args.host}:{args.port} ({e}). "
              "Is Live running with AbletonMCP selected as a Control Surface?")
        return 2

    try:
        if args.command == "ping":
            return cmd_ping(client)
        if args.command in SELFTESTS:
            return SELFTESTS[args.command](client, args.via_server)
        result = client.send(args.command, parse_params(args.params))
        print(json.dumps(result, indent=2))
        return 0
    except CommandError as e:
        print(f"Error from Remote Script: {e}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
