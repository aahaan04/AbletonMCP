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


def find_or_create_selftest_track(client: Client) -> int:
    count = client.send("get_session_info")["track_count"]
    for i in range(count):
        if client.send("get_track_info", {"track_index": i})["name"] == SELFTEST_TRACK:
            return i
    index = client.send("create_midi_track", {"index": -1})["index"]
    client.send("set_track_name", {"track_index": index, "name": SELFTEST_TRACK})
    return index


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send commands straight to the AbletonMCP Remote Script socket.",
    )
    parser.add_argument("command", help="ping | selftest-notes | any Remote Script command type")
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
        if args.command == "selftest-notes":
            return cmd_selftest_notes(client, args.via_server)
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
