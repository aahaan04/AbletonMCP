"""Talk to the AbletonMCP Remote Script directly over its TCP socket.

Bypasses Claude Desktop and the MCP server so Remote Script features can be
tested on their own. Stdlib only, so plain `python` works; --via-server needs
the project env (`uv run`).

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
from pathlib import Path

HOST = os.environ.get("ABLETON_HOST", "127.0.0.1")
PORT = int(os.environ.get("ABLETON_PORT", "9877"))
SELFTEST_TRACK = "ableton_cli selftest"
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
# selftest-notes: create clip -> add notes -> read -> clear -> read empty
# --------------------------------------------------------------------------

class RawOps:
    """Selftest steps straight over the socket."""

    label = "raw socket"

    def __init__(self, client: Client):
        self.c = client

    def add_notes(self, t, s, notes):
        self.c.send("add_notes_to_clip", {"track_index": t, "clip_index": s, "notes": notes})

    def get_notes(self, t, s):
        return self.c.send("get_clip_notes", {"track_index": t, "clip_index": s})

    def clear_notes(self, t, s):
        return self.c.send("clear_notes_from_clip", {"track_index": t, "clip_index": s})

    def get_track_info(self, t):
        return self.c.send("get_track_info", {"track_index": t})

    def run(self, command, **params):
        """Any other command, by its Remote Script name."""
        return self.c.send(command, params)


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

    def _call(self, tool, *args):
        import asyncio
        import inspect

        result = tool(None, *args)
        if inspect.iscoroutine(result):
            result = asyncio.run(result)
        return result

    def add_notes(self, t, s, notes):
        out = self._call(self.server.add_notes_to_clip, t, s, notes)
        if out.startswith("Error"):
            raise CommandError(out)

    def get_notes(self, t, s):
        out = self._call(self.server.get_clip_notes, t, s)
        try:
            return json.loads(out)
        except ValueError:
            raise CommandError(out)

    def clear_notes(self, t, s):
        out = self._call(self.server.clear_notes_from_clip, t, s)
        if not out.startswith("Cleared"):
            raise CommandError(out)
        return {"message": out}

    def get_track_info(self, t):
        return self._json(self._call(self.server.get_track_info, t))

    def run(self, command, **params):
        """Any other command, through the MCP tool of the same name. Tools
        that return prose are checked for an error prefix; JSON is decoded."""
        import asyncio
        import inspect

        out = getattr(self.server, command)(None, **params)
        if inspect.iscoroutine(out):
            out = asyncio.run(out)
        if out.startswith(("Error", "Ableton Remote Script missing")):
            raise CommandError(out)
        try:
            return json.loads(out)
        except ValueError:
            return {"message": out}

    @staticmethod
    def _json(out):
        try:
            return json.loads(out)
        except ValueError:
            raise CommandError(out)


def find_or_create_track(client: Client, name: str) -> int:
    count = client.send("get_session_info")["track_count"]
    for i in range(count):
        if client.send("get_track_info", {"track_index": i})["name"] == name:
            return i
    index = client.send("create_midi_track", {"index": -1})["index"]
    client.send("set_track_name", {"track_index": index, "name": name})
    return index


def find_or_create_selftest_track(client: Client) -> int:
    return find_or_create_track(client, SELFTEST_TRACK)


def cmd_selftest_notes(client: Client, via_server: bool) -> int:
    ops = ServerOps() if via_server else RawOps(client)
    track = find_or_create_selftest_track(client)
    slots = client.send("get_track_info", {"track_index": track})["clip_slots"]
    slot = next((s["index"] for s in slots if not s["has_clip"]), None)
    if slot is None:
        print(f"FAIL: no empty clip slot on track {track} ({SELFTEST_TRACK!r})")
        return 1

    print(f"selftest-notes via {ops.label}: track {track} ({SELFTEST_TRACK!r}), slot {slot}")
    client.send("create_clip", {"track_index": track, "clip_index": slot, "length": 4.0})
    print("  create_clip            ok")
    try:
        ops.add_notes(track, slot, SELFTEST_NOTES)
        print(f"  add_notes_to_clip      ok ({len(SELFTEST_NOTES)} notes)")

        before = ops.get_notes(track, slot)
        if before["note_count"] != len(SELFTEST_NOTES):
            print(f"FAIL: expected {len(SELFTEST_NOTES)} notes after add, read {before['note_count']}")
            return 1
        print(f"  get_clip_notes         ok ({before['note_count']} notes)")

        cleared = ops.clear_notes(track, slot)
        print(f"  clear_notes_from_clip  ok ({cleared.get('cleared_count', cleared.get('message'))})")

        after = ops.get_notes(track, slot)
        if after["note_count"] != 0 or after["notes"]:
            print(f"FAIL: expected an empty clip after clear, read {after['note_count']} notes")
            return 1
        print("  get_clip_notes         ok (0 notes, notes == [])")
    except CommandError as e:
        print(f"FAIL: {e}")
        return 1
    finally:
        client.send("delete_clip", {"track_index": track, "clip_index": slot})
        print("  delete_clip            ok (cleaned up)")

    print("PASS")
    return 0


# --------------------------------------------------------------------------
# selftest-connection: large multi-byte replies, then recovery after an error
# --------------------------------------------------------------------------

def cmd_selftest_connection(client: Client, via_server: bool) -> int:
    ops = ServerOps() if via_server else RawOps(client)
    track = find_or_create_selftest_track(client)
    print(f"selftest-connection via {ops.label}: track {track} ({SELFTEST_TRACK!r})")
    failures = 0
    try:
        # >8 KiB of 2- and 4-byte characters, at both byte parities, so a
        # recv() boundary lands inside a character on at least one of them.
        for label, name in (("2-byte, even", "é" * 5000),
                            ("2-byte, odd", "x" + "é" * 5000),
                            ("4-byte", "🥁" * 2500)):
            client.send("set_track_name", {"track_index": track, "name": name})
            expected = client.send("get_track_info", {"track_index": track})["name"]
            got = ops.get_track_info(track)["name"]
            size = len(expected.encode("utf-8"))
            if got == expected and size > 8192:
                print(f"  multi-byte reply       ok ({label}, name {size} bytes)")
            else:
                print(f"  multi-byte reply       FAIL ({label}: got {len(got)} chars, "
                      f"expected {len(expected)}, {size} bytes)")
                failures += 1

        try:
            ops.get_track_info(9999)
            print("  error reply            FAIL (track 9999 did not error)")
            failures += 1
        except CommandError as e:
            print(f"  error reply            ok ({e})")
        if ops.get_track_info(track)["index"] == track:
            print("  command after error    ok (reconnected)")
    except CommandError as e:
        print(f"FAIL: {e}")
        failures += 1
    finally:
        client.send("set_track_name", {"track_index": track, "name": SELFTEST_TRACK})

    print("PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# selftest-mixer: volume, pan, mute, solo, send -> read back -> defaults
# --------------------------------------------------------------------------

class Checker:
    def __init__(self):
        self.failures = 0

    def check(self, label, ok, detail=""):
        print(f"  {label:<22} {'ok' if ok else 'FAIL'}{f' ({detail})' if detail else ''}")
        if not ok:
            self.failures += 1

    def finish(self):
        print("PASS" if not self.failures else f"FAIL ({self.failures})")
        return 1 if self.failures else 0


def _close(a, b, tol):
    return a is not None and abs(a - b) <= tol


def cmd_selftest_mixer(client: Client, via_server: bool) -> int:
    ops = ServerOps() if via_server else RawOps(client)
    track = find_or_create_selftest_track(client)
    returns = client.send("get_session_info")["return_track_count"]
    print(f"selftest-mixer via {ops.label}: track {track} ({SELFTEST_TRACK!r}), {returns} return track(s)")
    c = Checker()
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
            print("  send                   skipped (no return tracks in this set)")
    except CommandError as e:
        c.check("mixer commands", False, str(e))
    finally:
        ops.run("set_track_volume", track_index=track, db=0.0)
        ops.run("set_track_panning", track_index=track, value=0.0)
        ops.run("set_track_mute", track_index=track, mute=False)
        ops.run("set_track_solo", track_index=track, solo=False)
        if returns:
            ops.run("set_send", track_index=track, send_index=0, value=0.0)
    m = ops.run("get_mixer", track_index=track)
    restored = (_close(m["volume"]["db"], 0.0, 0.05) and _close(m["panning"]["value"], 0.0, 1e-3)
                and m["mute"] is False and m["solo"] is False
                and (not returns or m["sends"][0]["value"] == 0.0))
    c.check("defaults restored", restored,
            f"vol {m['volume']['display']}, pan {m['panning']['display']}, "
            f"mute {m['mute']}, solo {m['solo']}"
            + (f", send A {m['sends'][0]['display']}" if returns else ""))
    return c.finish()


# --------------------------------------------------------------------------
# selftest-drums: stock kit -> list pads -> change a pad parameter -> read back
# --------------------------------------------------------------------------

DRUMS_TRACK = "ableton_cli drums"
DRUM_KIT = "808 Core Kit.adg"


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
    """Set a nested parameter to a new value, read it back, restore it."""
    params = ops.run("get_nested_device_parameters", **address)["device"]["parameters"]
    p = _pick_param(params, preferred)
    if p is None:
        c.check(label, False, "no writable parameter")
        return
    name, old = p["name"].strip(), p["value"]
    new = p["min"] + (p["max"] - p["min"]) * (0.25 if old > (p["min"] + p["max"]) / 2 else 0.75)
    if p.get("is_quantized"):
        new = float(round(new))
    try:
        ops.run("set_nested_device_parameter", value=new, parameter_name=name, **address)
        back = next(q for q in ops.run("get_nested_device_parameters", **address)["device"]["parameters"]
                    if q["name"].strip() == name)
        shown = f" ({back['value_string']})" if back.get("value_string") else ""
        c.check(label, _close(back["value"], new, 1e-4), f"{name}: {old:g} -> {back['value']:g}{shown}")
    finally:
        ops.run("set_nested_device_parameter", value=old, parameter_name=name, **address)
    restored = next(q for q in ops.run("get_nested_device_parameters", **address)["device"]["parameters"]
                    if q["name"].strip() == name)
    c.check(f"{label} restored", _close(restored["value"], old, 1e-4), f"{name} = {restored['value']:g}")


def cmd_selftest_drums(client: Client, via_server: bool) -> int:
    ops = ServerOps() if via_server else RawOps(client)
    track = find_or_create_track(client, DRUMS_TRACK)
    if not client.send("get_track_info", {"track_index": track})["devices"]:
        items = client.send("get_browser_items_at_path", {"path": "drums"})["items"]
        kit = next((i for i in items if i["name"] == DRUM_KIT), None) \
            or next(i for i in items if i["name"].endswith(".adg"))
        client.send("load_browser_item", {"track_index": track, "item_uri": kit["uri"]})
        print(f"  loaded kit             {kit['name']}")
    print(f"selftest-drums via {ops.label}: track {track} ({DRUMS_TRACK!r})")
    c = Checker()
    try:
        pads = ops.run("get_drum_pads", track_index=track)
        listed = pads["pads"]
        names_ok = all(p["note_name"] == f"{'C C# D D# E F F# G G# A A# B'.split()[p['note'] % 12]}"
                                       f"{p['note'] // 12 - 2}" for p in listed)
        c.check("get_drum_pads", pads["pad_count"] > 0 and names_ok,
                f"{pads['rack_name']}: {pads['pad_count']} pads, "
                + ", ".join(f"{p['note']}={p['note_name']} {p['name']}" for p in listed[:4]) + ", ...")

        pad = next((p for p in listed if p["note"] == 36), listed[0])
        address = {"track_index": track, "device_index": pads["device_index"],
                   "pad_note": pad["note"], "nested_device_index": 0}
        print(f"  pad                    {pad['note']} {pad['note_name']} {pad['name']!r}: "
              + ", ".join(d["name"] for d in pad["chains"][0]["devices"]))
        _roundtrip_param(c, ops, "pad macro", address, ("Decay", "Tone"))
        if pad["chains"][0]["devices"][0]["is_rack"]:
            _roundtrip_param(c, ops, "pad sampler (path)", dict(address, path=[0, 0]),
                             ("Transpose", "Tune", "Decay"))

        empty = next(n for n in range(36, 128) if n not in {p["note"] for p in listed})
        try:
            ops.run("get_nested_device_parameters", **dict(address, pad_note=empty))
            c.check("empty pad refused", False, f"pad {empty} did not error")
        except CommandError as e:
            c.check("empty pad refused", "is empty" in str(e), str(e)[-40:])
    except CommandError as e:
        c.check("drum commands", False, str(e))
    return c.finish()


# --------------------------------------------------------------------------
# selftest-deletes: throwaway track with kit + clips, delete inside-out
# --------------------------------------------------------------------------

DELETE_TRACK = "ableton_cli delete me"


def _refused(ops, command, **params):
    try:
        ops.run(command, **params)
        return False
    except CommandError as e:
        return "Refusing to delete" in str(e)


def cmd_selftest_deletes(client: Client, via_server: bool) -> int:
    ops = ServerOps() if via_server else RawOps(client)
    track = client.send("create_midi_track", {"index": -1})["index"]
    client.send("set_track_name", {"track_index": track, "name": DELETE_TRACK})
    items = client.send("get_browser_items_at_path", {"path": "drums"})["items"]
    kit = next((i for i in items if i["name"] == DRUM_KIT), None) \
        or next(i for i in items if i["name"].endswith(".adg"))
    client.send("load_browser_item", {"track_index": track, "item_uri": kit["uri"]})
    client.send("create_clip", {"track_index": track, "clip_index": 0, "length": 4.0})
    client.send("duplicate_session_clip_to_arrangement",
                {"track_index": track, "clip_index": 0, "destination_time": 0.0})
    print(f"selftest-deletes via {ops.label}: track {track} ({DELETE_TRACK!r}) "
          f"with {kit['name']}, a session clip and an arrangement clip")

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
        ops.run("delete_device", track_index=track, device_index=0, expected_name=kit["name"][:-4])
        devices = client.send("get_track_info", {"track_index": track})["devices"]
        c.check("device", devices == [], f"{len(devices)} devices left")

        returns = client.send("get_session_info")["return_track_count"]
        refused = _refused(ops, "delete_return_track", return_index=0, expected_name="__not_a_return__")
        c.check("return wrong name", refused
                and client.send("get_session_info")["return_track_count"] == returns,
                f"{returns} returns kept")
    except CommandError as e:
        c.check("delete commands", False, str(e))
    finally:
        count = client.send("get_session_info")["track_count"]
        ops.run("delete_track", track_index=track, expected_name=DELETE_TRACK)
        names = [client.send("get_track_info", {"track_index": i})["name"]
                 for i in range(client.send("get_session_info")["track_count"])]
        c.check("track", len(names) == count - 1 and DELETE_TRACK not in names,
                f"{count} -> {len(names)} tracks")
    return c.finish()


SELFTESTS = {
    "selftest-notes": cmd_selftest_notes,
    "selftest-connection": cmd_selftest_connection,
    "selftest-mixer": cmd_selftest_mixer,
    "selftest-drums": cmd_selftest_drums,
    "selftest-deletes": cmd_selftest_deletes,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send commands straight to the AbletonMCP Remote Script socket.",
    )
    parser.add_argument("command", help="ping | " + " | ".join(SELFTESTS) + " | any Remote Script command type")
    parser.add_argument("params", nargs="*", help="JSON object or key=value pairs")
    parser.add_argument("--via-server", action="store_true",
                        help="selftest-notes: drive the note steps through MCP_Server tool functions")
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
