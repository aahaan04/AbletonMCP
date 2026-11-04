# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Optional, Union

from .telemetry import record_startup
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool
from .dataset import dataset_enabled, get_recorder
from .dataset.trajectory_decorator import trajectory_tool

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None

    # One socket, several threads: tool calls run on the MCP worker while the
    # passive poller drains events on its own thread. Serialise so their
    # sendall/recv pairs cannot interleave.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.disconnect()
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception as e:
                    logger.error(f"Error disconnecting from Ableton: {str(e)}")
                finally:
                    self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive one JSON response using the command's socket timeout."""
        data = bytearray()
        while True:
            chunk = sock.recv(buffer_size)
            if not chunk:
                raise ConnectionError("Connection closed before receiving a complete response")
            data.extend(chunk)

            try:
                decoded = data.decode("utf-8")
            except UnicodeDecodeError as e:
                # A TCP chunk may end between the bytes of a UTF-8 character.
                # Invalid bytes elsewhere are an error, not an incomplete frame.
                if e.reason == "unexpected end of data" and e.end == len(data):
                    continue
                raise

            try:
                json.loads(decoded)
            except json.JSONDecodeError:
                continue
            logger.info("Received complete response (%d bytes)", len(data))
            return bytes(data)

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response.

        Serialised on ``self._lock``: the request and its response are one
        indivisible exchange on a shared socket. Holding the lock across the
        whole round-trip (including reconnect) is what keeps a concurrent
        caller from reading someone else's reply.
        """
        with self._lock:
            return self._send_command_locked(command_type, params)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "delete_clip",
            "clear_notes_from_clip",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement",
            "fill_arrangement", "fill_arrangement_section", "set_arrangement_loop",
            "toggle_cue_at_playhead", "rename_locator",
            # Mixer commands
            "set_track_volume", "set_track_panning", "set_send",
            "set_track_mute", "set_track_solo",
            # Rack commands
            "set_nested_device_parameter",
            # Deletes (delete_clip is listed above)
            "delete_track", "delete_return_track", "delete_device", "delete_nested_device",
            "create_return_track", "load_browser_item",
            "write_clip_automation", "clear_clip_automation",
            "modify_notes", "delete_notes",
            "load_groove", "assign_groove", "set_groove_amount", "set_groove_params",
        ]

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {"create_audio_clip": 65.0}
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type
            if command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.disconnect()
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.disconnect()
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.disconnect()
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.disconnect()
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        script_info = None
        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
            from .script_handshake import handshake

            script_info = handshake(ableton.send_command)
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        if dataset_enabled():
            try:
                recorder = get_recorder()
                if recorder:
                    logger.info(
                        "Dataset recording enabled → Supabase session %s",
                        recorder.session_id,
                    )
                from .script_handshake import script_has_capability
                from .dataset.passive_poller import start_passive_poller

                # Runs even without Live-UI events: it also settles implicit
                # preferences for agent actions.
                start_passive_poller()
                if not script_has_capability("drain_passive_events"):
                    logger.warning(
                        "Passive Live listeners unavailable — Remote Script outdated "
                        "or not loaded. Restart Ableton after script update."
                    )
            except Exception as e:
                logger.warning(f"Failed to start dataset recorder: {e}")
        else:
            reason = "unknown"
            try:
                from .config import telemetry_config  # noqa: F401
                from .telemetry import get_telemetry_consent, is_telemetry_enabled

                if not is_telemetry_enabled():
                    reason = "telemetry disabled (config.enabled=False or DISABLE_TELEMETRY)"
                elif not get_telemetry_consent():
                    reason = "no dataset consent yet — the user has not opted in"
                else:
                    reason = "ABLETON_MCP_DISABLE_DATASET or unexpected gate failure"
            except Exception:
                reason = "MCP_Server/config.py missing or unreadable (gitignored; required for Supabase)"
            logger.info("Dataset recording off — %s", reason)

        yield {"script_info": script_info}
    finally:
        global _ableton_connection
        try:
            from .dataset.passive_poller import stop_passive_poller

            stop_passive_poller()
        except Exception as e:
            logger.debug(f"Failed to stop passive poller: {e}")
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        try:
            recorder = get_recorder()
            if recorder:
                # Bounded wait so queued rows land before the daemon worker
                # is killed at exit.
                recorder.end(timeout=float(
                    os.environ.get("ABLETON_MCP_DATASET_FLUSH_SEC", "5")
                ))
        except Exception as e:
            logger.debug(f"Failed to end dataset session: {e}")
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
def _server_instructions() -> str:
    from .dataset.consent import CONSENT_INFO

    return (
        "Controls Ableton Live through its Remote Script. For notes, prefer the "
        "compact tools (create_clip with notes, write_drum_pattern, compact note "
        "arrays with repeat) over long per-note JSON. " + CONSENT_INFO
    )


mcp = FastMCP(
    "AbletonMCP",
    instructions=_server_instructions(),
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
            # Passive events stop arriving while disconnected, so any cached
            # state hash may describe a Live session that has since moved on.
            try:
                recorder = get_recorder()
                if recorder:
                    recorder.invalidate_cached_state()
            except Exception:
                pass
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
def set_dataset_consent(ctx: Context, consent: bool, user_said: str = "") -> str:
    """Record the user's answer to the dataset consent question.

    Call this ONLY after the user has answered in their own words. Never infer
    the answer, never call it on their behalf, and never call it because
    contributing seems helpful — consent the user did not give is not consent.
    If they have not been asked yet, ask first and wait for their reply.

    Parameters:
    - consent: True if the user agreed to contribute, False if they declined
    - user_said: The user's reply, quoted as closely as possible
    """
    # Intentionally undecorated: recording this call would mean collecting data
    # from someone who may have just declined.
    try:
        from .dataset.consent import record_consent

        state = record_consent(consent, quote=user_said)
    except Exception as e:
        logger.error(f"Could not record dataset consent: {str(e)}")
        return f"Error recording consent: {str(e)}"

    if not consent:
        return (
            "Recorded: dataset contribution declined. Nothing is uploaded, and "
            "you will not be asked again."
        )

    try:
        from .telemetry import refresh_consent_from_dataset

        refresh_consent_from_dataset()
        recorder = get_recorder()
        # Under opt-in the poller is almost never started at boot — consent did
        # not exist yet — so a grant has to start it here or passive Live events
        # would be missed for the rest of the session. Idempotent and
        # self-gating, so this is safe even if startup did start it.
        from .dataset.passive_poller import start_passive_poller

        start_passive_poller()
    except Exception as e:
        logger.error(f"Consent saved but recording failed to start: {str(e)}")
        return (
            "Consent saved, but recording could not start in this session. "
            "It will begin after a restart."
        )

    if recorder is None:
        return (
            "Consent saved, but recording is unavailable (telemetry may be "
            f"disabled). State: {state}."
        )
    return (
        "Thank you — this session is now being contributed to the open dataset. "
        "Say so at any time to stop, or set ABLETON_MCP_DISABLE_DATASET=1."
    )


@mcp.tool()
@telemetry_tool("get_session_info")
@trajectory_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    master_track includes output_meter_left / output_meter_right /
    output_meter_level: Live's instantaneous output meters, 0..1 (1 = clipping
    at 0 dBFS), or null where Live has no reading. To judge loudness use
    measure_levels, which samples over time.

    groove_amount is the global groove amount (0..1.3). A new Live 12 Set
    starts at 0, so assigned grooves are silent until it is raised.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_remote_script_info")
@trajectory_tool("get_remote_script_info", modifying=False)
def get_remote_script_info(ctx: Context, user_prompt: str = "") -> str:
    """
    Report Ableton Remote Script version and capabilities (handshake).

    Use this to verify the Live-side bridge matches this MCP server package.
    """
    try:
        from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION
        from .script_handshake import get_cached_script_info, handshake

        ableton = get_ableton_connection()
        info = handshake(ableton.send_command)
        cached = dict(get_cached_script_info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return json.dumps(cached, indent=2)
    except Exception as e:
        logger.error(f"Error getting remote script info: {str(e)}")
        return f"Error getting remote script info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_info")
@trajectory_tool("get_track_info")
def get_track_info(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific track in Ableton.

    Includes output_meter_left / output_meter_right / output_meter_level:
    the track's instantaneous output meters, 0..1, or null where Live has no
    reading (e.g. MIDI tracks without an instrument).

    Parameters:
    - track_index: The index of the track to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_clip_notes")
@trajectory_tool("get_clip_notes")
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    compact: bool = False,
    user_prompt: str = "",
) -> str:
    """
    Read all MIDI notes from a Session-view clip.

    Default: one object per note with note_id, pitch, start_time, duration,
    velocity, mute (use the note_id values with modify_notes/delete_notes).
    compact=true returns [pitch, start, duration, velocity] rows (plus true
    when muted) sorted by time, about a fifth of the size: use it to check
    or compare clips.

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot index
    - compact: Return compact rows instead of note objects
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("get_clip_notes", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command(
            "get_clip_notes",
            {"track_index": track_index, "clip_index": clip_index},
        )
        if compact:
            from .note_tools import compact as to_rows

            result = {"clip_name": result.get("clip_name"), "length": result.get("length"),
                      "note_count": result.get("note_count"),
                      "format": "[pitch, start, duration, velocity(, muted)]",
                      "notes": to_rows(result.get("notes", []))}
            return json.dumps(result, separators=(",", ":"))
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"


@mcp.tool()
@telemetry_tool("get_device_parameters")
@trajectory_tool("get_device_parameters")
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    track_type: str = "track",
    include_nested: bool = False,
    name_filter: Optional[str] = None,
    user_prompt: str = "",
) -> str:
    """
    Read a top-level device's parameters (name, value, min, max, display
    string) on a track, return track or the master.

    By default only the device's own parameters are returned. For a rack
    (e.g. a Drum Rack) the chains are summarised as a count; nested devices
    can run to hundreds of KB, so read them one at a time with
    get_drum_pads / get_nested_device_parameters, or pass include_nested=true.

    Parameters:
    - track_index: Track (or return) index; ignored for the master
    - device_index: Index into that track's device chain
    - track_type: "track" (default), "return" or "master"
    - include_nested: Also return every chain and nested device (large for racks)
    - name_filter: Optional; only parameters whose name contains every word of this text
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        params = {"track_index": track_index, "device_index": device_index,
                  "include_chains": include_nested}
        if track_type != "track":
            from .script_handshake import require_capability

            missing = require_capability("device_params_anywhere", ableton.send_command)
            if missing:
                return missing
            params["track_type"] = track_type
        result = ableton.send_command("get_device_parameters", params)
        device = result.get("device", {})
        if not include_nested and "chains" in device:
            device["chain_count"] = len(device.pop("chains"))
            device["chains_omitted"] = "pass include_nested=true or use get_nested_device_parameters"
        if name_filter:
            device["parameters"] = [p for p in device.get("parameters", [])
                                    if _name_matches(p.get("name", ""), name_filter)]
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_device_parameter")
@trajectory_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameter_index: Optional[int] = None,
    value: Optional[float] = None,
    parameter_name: Optional[str] = None,
    track_type: str = "track",
    user_prompt: str = "",
) -> str:
    """
    Set one parameter of a top-level device on a track, return track or the
    master (e.g. the master Limiter's Ceiling, a return Reverb's Decay Time).
    For devices inside racks use set_nested_device_parameter.

    Give exactly one of parameter_name (case-insensitive exact match) or
    parameter_index; read get_device_parameters first for names, indices and
    min..max ranges. value is in the parameter's own units, which are often
    not what Live displays: many parameters (e.g. Reverb's Decay Time) run
    0..1 internally while showing seconds or Hz. Check min/max and the
    value_string, and read back after setting. Out-of-range values are refused.

    Parameters:
    - track_index: Track (or return) index; ignored for the master
    - device_index: Index into that track's device chain
    - parameter_index: Index into device.parameters
    - value: New parameter value (Live parameter units)
    - parameter_name: Parameter name instead of parameter_index, e.g. "Ceiling"
    - track_type: "track" (default), "return" or "master"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        if value is None:
            return "Error setting device parameter: value is required"
        if (parameter_index is None) == (parameter_name is None):
            return "Error setting device parameter: give exactly one of parameter_index or parameter_name"
        ableton = get_ableton_connection()
        params = {"track_index": track_index, "device_index": device_index, "value": value}
        if track_type != "track" or parameter_name is not None:
            # Older Remote Scripts ignore these and would set track N's parameter.
            from .script_handshake import require_capability

            missing = require_capability("device_params_anywhere", ableton.send_command)
            if missing:
                return missing
            params.update(track_type=track_type, parameter_name=parameter_name)
        if parameter_index is not None:
            params["parameter_index"] = parameter_index
        result = ableton.send_command("set_device_parameter", params)
        shown = f" ({result['display']})" if result.get("display") else ""
        return (
            f"Set {result.get('name', 'parameter')} "
            f"{result.get('old_value')} → {result.get('value')}{shown}"
        )
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool()
@telemetry_tool("get_session_snapshot")
@trajectory_tool("get_session_snapshot")
def get_session_snapshot(
    ctx: Context,
    include_notes: bool = True,
    include_params: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Capture a full project state snapshot for trajectory recording.

    Includes session metadata, every track (mixer, devices, session clips,
    arrangement clips), optional MIDI notes, and optional device parameters.
    Used to version musical state S_t → S_{t+1}.

    Parameters:
    - include_notes: Include MIDI note arrays in clips (default True)
    - include_params: Include device parameter values (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("get_session_snapshot", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command(
            "get_session_snapshot",
            {
                "include_notes": include_notes,
                "include_params": include_params,
            },
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session snapshot: {str(e)}")
        return f"Error getting session snapshot: {str(e)}"


@mcp.tool()
@telemetry_tool("create_midi_track")
@trajectory_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@telemetry_tool("create_audio_track")
@trajectory_tool("create_audio_track")
def create_audio_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new audio track in the Ableton session.

    Use this for recorded or imported audio (samples, stems, vocals). For MIDI
    instruments use create_midi_track instead.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_track", {"index": index})
        return f"Created new audio track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_track_name")
@trajectory_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

# ── Level measurement ──────────────────────────────────────────────────────────

import time as _time

_now = _time.monotonic
_sleep = _time.sleep
_METER_INTERVAL_SEC = 0.1
_MAX_MEASURE_SEC = 120.0


def _read_meters(ableton, track_indexes: Optional[List[int]], fast: bool) -> Dict[str, Any]:
    """One reading: song time plus every wanted track's output_meter_level."""
    if fast:
        m = ableton.send_command("get_meters", {})
        tracks = {t["index"]: t for t in m["tracks"]
                  if track_indexes is None or t["index"] in track_indexes}
        return {"time": m["song_time"], "playing": m["is_playing"], "tracks": tracks,
                "returns": {r["index"]: r for r in m.get("returns", [])}, "master": m["master"]}
    s = ableton.send_command("get_session_info")
    indexes = track_indexes if track_indexes is not None else range(s["track_count"])
    tracks = {}
    for i in indexes:
        info = ableton.send_command("get_track_info", {"track_index": i})
        tracks[i] = {"index": i, "name": info["name"], "level": info.get("output_meter_level")}
    return {"time": s["current_song_time"], "playing": s["is_playing"], "tracks": tracks,
            "returns": {}, "master": {"level": s["master_track"].get("output_meter_level")}}


class _LevelStats:
    def __init__(self):
        self.peak, self.total, self.count = 0.0, 0.0, 0

    def add(self, level):
        if level is None:
            return
        level = float(level)
        self.peak = max(self.peak, level)
        self.total += level
        self.count += 1

    def report(self):
        if not self.count:
            return {"peak": None, "mean": None}
        return {"peak": round(self.peak, 3), "mean": round(self.total / self.count, 3),
                "clipping": self.peak >= 0.999}


@mcp.tool()
@telemetry_tool("measure_levels")
def measure_levels(
    ctx: Context,
    seconds: float = 8.0,
    start_bar: Optional[float] = None,
    settle_seconds: float = 3.0,
    track_indexes: Optional[List[int]] = None,
    by_section: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Play the Set and measure output levels over a time window: peak and mean
    of Live's meters (0..1, where 1.0 = 0 dBFS, i.e. clipping) per track,
    return track and the master. Use this to check a mix instead of single
    get_track_info / get_session_info readings, which catch one instant.

    With start_bar the playhead jumps there and playback starts (and is
    stopped again afterwards); without it, an already-running playback is
    measured, or playback starts from the current position. The first
    settle_seconds are discarded: Live keeps showing earlier peaks for up to
    ~3 s, which made a silent track read loud right after a jump.

    by_section=true also splits the results by locator section (e.g. Intro,
    Drop) using the playhead position of each sample, so one long run can
    compare sections. Keep seconds short (8-30); the call blocks while it plays.

    Parameters:
    - seconds: Measuring time after settling (max 120)
    - start_bar: Bar to play from (1-based); omit to use the current position
    - settle_seconds: Seconds to discard first (default 3)
    - track_indexes: Tracks to include (default: all)
    - by_section: Also report per locator section
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        if not 0 < seconds <= _MAX_MEASURE_SEC:
            return f"Error measuring levels: seconds must be in (0, {_MAX_MEASURE_SEC:g}]"
        ableton = get_ableton_connection()
        fast = require_capability("get_meters", ableton.send_command) is None
        session = ableton.send_command("get_session_info")
        was_playing, start_time = session["is_playing"], session["current_song_time"]
        bpb = 4.0 * session["signature_numerator"] / session["signature_denominator"]
        started = False
        if start_bar is not None:
            if was_playing:
                ableton.send_command("stop_playback")
            ableton.send_command("set_current_song_time", {"time": (start_bar - 1) * bpb})
            _sleep(0.15)
        if start_bar is not None or not was_playing:
            ableton.send_command("start_playback")
            started = True

        sections = []
        if by_section and require_capability("get_locators", ableton.send_command) is None:
            locs = sorted(ableton.send_command("get_locators")["locators"], key=lambda l: l["time"])
            sections = [(l["time"], l["name"]) for l in locs]

        totals = {"tracks": {}, "returns": {}, "master": _LevelStats()}
        per_section: Dict[str, Dict[str, Any]] = {}
        names = {}
        samples = 0
        try:
            settle_until = _now() + max(0.0, settle_seconds)
            end = settle_until + seconds
            while _now() < end:
                reading = _read_meters(ableton, track_indexes, fast)
                if _now() >= settle_until:
                    samples += 1
                    section = None
                    for t0, name in sections:
                        if reading["time"] >= t0 - 1e-6:
                            section = name
                    buckets = [totals]
                    if section is not None:
                        buckets.append(per_section.setdefault(
                            section, {"tracks": {}, "returns": {}, "master": _LevelStats()}))
                    for b in buckets:
                        for kind in ("tracks", "returns"):
                            for i, t in reading[kind].items():
                                names[(kind, i)] = t.get("name")
                                b[kind].setdefault(i, _LevelStats()).add(t.get("level"))
                        b["master"].add(reading["master"].get("level"))
                _sleep(_METER_INTERVAL_SEC)
        finally:
            if started:
                ableton.send_command("stop_playback")
                if start_bar is not None:
                    ableton.send_command("set_current_song_time", {"time": start_time})

        def render(b):
            out = {"master": b["master"].report(),
                   "tracks": [dict(index=i, name=names.get(("tracks", i)), **s.report())
                              for i, s in sorted(b["tracks"].items())]}
            if b["returns"]:
                out["returns"] = [dict(index=i, name=names.get(("returns", i)), **s.report())
                                  for i, s in sorted(b["returns"].items())]
            return out

        result = {"samples": samples, "seconds": seconds, "settle_seconds": settle_seconds,
                  "source": "get_meters" if fast else "track info polling", **render(totals)}
        if by_section:
            result["sections"] = {name: render(b) for name, b in per_section.items()}
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error measuring levels: {str(e)}")
        return f"Error measuring levels: {str(e)}"


# ── Note writing helpers (compact notes, repeats, drum steps) ──────────────────

NoteInput = Union[List[float], Dict[str, Union[int, float, bool]]]

_ARRANGEMENT_COPY_NOTE = (
    "Arrangement copies of this clip do not follow Session edits; if it is "
    "already placed in the song, re-place it with fill_arrangement or "
    "fill_arrangement_section (replace=true)."
)


def _session_clip_info(ableton, track_index: int, clip_index: int) -> Optional[Dict[str, Any]]:
    """The clip dict from get_track_info for one slot, or None if it is empty."""
    slots = ableton.send_command("get_track_info", {"track_index": track_index})["clip_slots"]
    if clip_index < 0 or clip_index >= len(slots):
        raise IndexError(f"Clip slot {clip_index} out of range (track has {len(slots)})")
    return slots[clip_index]["clip"] if slots[clip_index]["has_clip"] else None


def _write_notes(ableton, track_index: int, clip_index: int, notes: List[Any],
                 repeat_every_beats: Optional[float] = None,
                 repeat_until_beats: Optional[float] = None) -> Dict[str, Any]:
    """Normalise compact/dict notes, tile them if asked, and add them."""
    from .note_tools import normalize_notes, tile_notes

    clip = _session_clip_info(ableton, track_index, clip_index)
    if clip is None:
        raise ValueError(f"No clip in slot {clip_index} of track {track_index}")
    length = float(clip["length"])
    pattern = normalize_notes(notes)
    written = pattern
    if repeat_every_beats:
        written = tile_notes(pattern, float(repeat_every_beats),
                             float(repeat_until_beats) if repeat_until_beats else length)
    if written:
        ableton.send_command("add_notes_to_clip", {
            "track_index": track_index, "clip_index": clip_index, "notes": written})
    past_end = sum(1 for n in written if n["start_time"] >= length - 1e-9)
    return {"pattern": len(pattern), "written": len(written), "length": length,
            "past_end": past_end, "clip": clip}


def _notes_summary(info: Dict[str, Any]) -> str:
    text = f"{info['written']} notes"
    if info["written"] != info["pattern"]:
        text += f" ({info['pattern']}-note pattern repeated)"
    if info["past_end"]:
        text += (f". WARNING: {info['past_end']} note(s) start at or after the clip end "
                 f"(beat {info['length']:g}) and will not play")
    return text


@mcp.tool()
@rich_telemetry_tool("create_clip")
@trajectory_tool("create_clip")
def create_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    length: float = 4.0,
    name: Optional[str] = None,
    notes: Optional[List[NoteInput]] = None,
    repeat_every_beats: Optional[float] = None,
    user_prompt: str = ""
) -> str:
    """
    Create a MIDI clip in an empty Session slot, and optionally name it and
    fill it in the same call (preferred over create_clip + set_clip_name +
    add_notes_to_clip).

    notes use the compact form [pitch, start_beat, duration_beats, velocity]
    (optionally a 5th value true = muted), e.g. [[60, 0, 1, 100], [63, 1, 1, 90]].
    With repeat_every_beats, the notes are one pattern (all starting inside
    the first repeat) tiled across the whole clip: write one bar of a 4-bar
    clip with repeat_every_beats=4. For drums, write_drum_pattern is shorter.

    The result reports the clip's groove: Live can give new clips a groove
    from the Set's Groove Pool on its own.

    Parameters:
    - track_index: Track to create the clip in
    - clip_index: Empty clip slot
    - length: Clip length in beats (4 beats = 1 bar in 4/4)
    - name: Optional clip name
    - notes: Optional notes, compact arrays or {"pitch","start_time","duration","velocity"} objects
    - repeat_every_beats: Optional pattern length to repeat the notes across the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("create_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "length": length
        })
        parts = [f"Created clip at track {track_index}, slot {clip_index}, {length:g} beats"]
        if name:
            ableton.send_command("set_clip_name", {
                "track_index": track_index, "clip_index": clip_index, "name": name})
            parts[0] += f", named '{name}'"
        if notes:
            info = _write_notes(ableton, track_index, clip_index, notes, repeat_every_beats)
            parts.append("added " + _notes_summary(info))
        groove = (_session_clip_info(ableton, track_index, clip_index) or {}).get("groove")
        if groove:
            parts.append(f"groove '{groove}' is assigned (from the Set's Groove Pool)")
        return "; ".join(parts)
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
@trajectory_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
@trajectory_tool("add_notes_to_clip")
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[NoteInput],
    repeat_every_beats: Optional[float] = None,
    repeat_until_beats: Optional[float] = None,
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to an existing Session clip (adds; does not replace).

    Use the compact form for anything beyond a few notes:
    [pitch, start_beat, duration_beats, velocity] (optionally a 5th value
    true = muted), e.g. [[36, 0, 0.25, 110], [36, 1, 0.25, 100]]. The object
    form {"pitch","start_time","duration","velocity","mute"} also works.

    To fill a clip from one bar, write that bar and set repeat_every_beats
    (e.g. 4): the notes are tiled up to repeat_until_beats, default the clip
    length. For drums prefer write_drum_pattern; for a new clip prefer
    create_clip(name=..., notes=...).

    On a drum track, call get_drum_pads first and use the pad notes it
    returns as pitches; never guess which note is which drum. Arrangement
    copies of this clip do not follow Session edits; re-place them with
    fill_arrangement(_section) replace=true.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: Compact arrays or note objects
    - repeat_every_beats: Optional pattern length; notes must start inside the first repeat
    - repeat_until_beats: Where repeating stops (default: clip length)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        info = _write_notes(ableton, track_index, clip_index, notes,
                            repeat_every_beats, repeat_until_beats)
        return f"Added {_notes_summary(info)} to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("write_drum_pattern")
@trajectory_tool("write_drum_pattern")
def write_drum_pattern(
    ctx: Context,
    track_index: int,
    clip_index: int,
    pattern: Dict[str, Union[str, Dict[str, Any]]],
    step_beats: float = 0.25,
    length: Optional[float] = None,
    name: Optional[str] = None,
    replace: bool = True,
    velocity: int = 100,
    accent_velocity: int = 127,
    ghost_velocity: int = 60,
    gate_beats: Optional[float] = None,
    device_index: Optional[int] = None,
    user_prompt: str = ""
) -> str:
    """
    Write a drum part from step strings, one row per Drum Rack pad. This is
    the shortest way to write drums; pads are looked up by name through
    get_drum_pads, so pitches are never guessed.

    pattern maps a pad to its steps, e.g. with 16th-note steps (step_beats=0.25):
      {"Kick": "x...x...x...x...",
       "Clap": "....x.......x...",
       "Hat":  "o.x.o.x.o.x.o.X."}
    Keys: a pad name (case-insensitive; a unique part like "Hat" is fine),
    a MIDI note ("36") or a note name ("C1").
    Steps: x = hit at `velocity`, X = accent, o = ghost, 1-9 = velocity
    14..127, . - _ = rest; spaces and | are ignored (use "x... x... | ...").
    For exact velocities use {"steps": "x.x.", "velocities": [120, 0, 80, 0]}.

    Each row repeats to fill the clip, so one bar of steps fills a 4-bar
    clip; rows may differ in length (polyrhythms). If the slot is empty a
    clip is created with `length` beats (default: the longest row). By
    default the clip's existing notes are replaced; pass replace=false to
    add on top.

    Parameters:
    - track_index: Track with a Drum Rack
    - clip_index: Session slot (empty or with a clip)
    - pattern: Pad -> step string (or {"steps", "velocities"})
    - step_beats: Beats per step (0.25 = 16ths, 0.5 = 8ths, 1/3 = 8th triplets)
    - length: Clip length in beats when a clip is created
    - name: Optional clip name
    - replace: Clear the clip's notes first (default true)
    - velocity / accent_velocity / ghost_velocity: Velocities for x / X / o
    - gate_beats: Note length (default: one step)
    - device_index: Drum Rack position if the track has more than one
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .note_tools import drum_pattern_notes, parse_steps

        ableton = get_ableton_connection()
        pads = ableton.send_command("get_drum_pads", {
            "track_index": track_index, "device_index": device_index})["pads"]
        clip = _session_clip_info(ableton, track_index, clip_index)
        created = clip is None
        if created:
            longest = max(parse_steps(spec, velocity, accent_velocity, ghost_velocity)[1]
                          for spec in pattern.values()) * step_beats
            clip_length = float(length) if length else longest
            ableton.send_command("create_clip", {
                "track_index": track_index, "clip_index": clip_index, "length": clip_length})
        else:
            clip_length = float(clip["length"])
            if replace:
                ableton.send_command("clear_notes_from_clip", {
                    "track_index": track_index, "clip_index": clip_index})
        notes, rows = drum_pattern_notes(pattern, pads, step_beats, clip_length,
                                         velocity, accent_velocity, ghost_velocity, gate_beats)
        if notes:
            ableton.send_command("add_notes_to_clip", {
                "track_index": track_index, "clip_index": clip_index, "notes": notes})
        if name:
            ableton.send_command("set_clip_name", {
                "track_index": track_index, "clip_index": clip_index, "name": name})
        lines = [f"- {r['row']} -> {r['pitch']} ({r['note_name']}): {r['hits_per_repeat']} hits per "
                 f"{r['steps']} steps, {r['notes']} notes" for r in rows]
        head = (f"{'Created' if created else ('Replaced notes in' if replace else 'Added to')} "
                f"clip at track {track_index}, slot {clip_index} ({clip_length:g} beats"
                + (f", '{name}'" if name else "") + f"): {len(notes)} notes")
        groove = (_session_clip_info(ableton, track_index, clip_index) or {}).get("groove")
        tail = f"\nGroove assigned: '{groove}'" if groove else ""
        return head + "\n" + "\n".join(lines) + tail
    except Exception as e:
        logger.error(f"Error writing drum pattern: {str(e)}")
        return f"Error writing drum pattern: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("clear_notes_from_clip")
def clear_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = ""
) -> str:
    """
    Remove all MIDI notes from a Session clip.

    Writes are additive (add_notes_to_clip only appends), so to truly *modify*
    a clip you clear it first, then add the new notes. Use this with
    get_clip_notes and add_notes_to_clip for a real read -> modify -> write
    loop: read the notes, edit the list, clear_notes_from_clip, then
    add_notes_to_clip the edited notes.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("clear_notes_from_clip", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("clear_notes_from_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return "Cleared {n} note(s) from clip '{name}' (track {t}, slot {c})".format(
            n=result.get("cleared_count", "?"),
            name=result.get("clip_name", "clip"),
            t=track_index,
            c=clip_index,
        )
    except Exception as e:
        logger.error(f"Error clearing notes from clip: {str(e)}")
        return f"Error clearing notes from clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
@trajectory_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_arrangement_clip_name")
@trajectory_tool("set_arrangement_clip_name")
def set_arrangement_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip placed in the Arrangement timeline.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, in the
      same order returned by get_arrangement_clips (i.e. ordered by start_time)
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting arrangement clip name: {str(e)}")
        return f"Error setting arrangement clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
@trajectory_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
@trajectory_tool("load_instrument_or_effect")
def load_instrument_or_effect(
    ctx: Context,
    track_index: int,
    uri: str,
    track_type: str = "track",
    user_prompt: str = ""
) -> str:
    """
    Load an instrument or effect onto a track, a return track or the master
    using its browser URI. Effects go at the end of the device chain.

    For a reverb/delay bus: create_return_track, load the effect on it with
    track_type="return", then set_send on the source tracks.

    Parameters:
    - track_index: The index of the track (or return track; ignored for master)
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - track_type: "track" (default), "return", or "master". Returns and the
      master take effects only.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        if track_type != "track":
            from .script_handshake import require_capability

            missing = require_capability("load_on_return_or_master", ableton.send_command)
            if missing:
                return missing
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri,
            "track_type": track_type,
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
@trajectory_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
@trajectory_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_clip")
def delete_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    view: str = "session",
    expected_name: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Delete one clip: a Session clip (freeing its slot) or an Arrangement clip.

    Use this before create_clip when you want to overwrite an existing clip
    (create_clip itself refuses to write into an occupied slot).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: view="session": the clip slot index. view="arrangement": the
      clip's position in get_arrangement_clips for that track.
    - view: "session" (default) or "arrangement"
    - expected_name: Optional; refuse unless the clip has exactly this name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability(
            "delete_arrangement_clip" if view == "arrangement" else "delete_clip",
            ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "view": view,
            "expected_name": expected_name,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"


@mcp.tool()
@telemetry_tool("start_playback")
@trajectory_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
@trajectory_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("get_browser_tree")
@trajectory_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("get_browser_items_at_path")
@trajectory_tool("get_browser_items_at_path")
def get_browser_items_at_path(
    ctx: Context,
    path: str,
    name_filter: Optional[str] = None,
    limit: int = 100,
    user_prompt: str = ""
) -> str:
    """
    Get browser items at a specific path in Ableton's browser. Big folders
    (e.g. "sounds/Bass") hold hundreds of presets, so results are capped at
    `limit`; narrow them with name_filter, or use search_browser to find an
    item by name anywhere below a category.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - name_filter: Optional; keep items whose name contains every word of this text (case-insensitive)
    - limit: Maximum items returned (default 100)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })

        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")

        items = result.get("items", [])
        if name_filter:
            items = [i for i in items if _name_matches(i.get("name", ""), name_filter)]
        result["total_items"] = len(items)
        if len(items) > limit:
            result["truncated"] = f"showing {limit} of {len(items)}; narrow with name_filter"
        result["items"] = items[:limit]
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

def _name_matches(name: str, query: str) -> bool:
    lowered = name.lower()
    return all(word in lowered for word in query.lower().split())


_SEARCH_ROOTS = ("instruments", "sounds", "drums", "audio_effects", "midi_effects", "user_library")
_SEARCH_FOLDER_BUDGET = 150


@mcp.tool()
@telemetry_tool("search_browser")
def search_browser(
    ctx: Context,
    query: str,
    category: str = "all",
    limit: int = 25,
    loadable_only: bool = True,
    user_prompt: str = ""
) -> str:
    """
    Find browser items (instruments, presets, drum kits, effects, samples,
    grooves) by name, without listing whole folders. Every word of the query
    must appear in the name, case-insensitively: "808 kit", "bass sub",
    "reverb hall". Returns name, path and uri; load a result with
    load_instrument_or_effect(uri=...).

    Parameters:
    - query: Words to look for in item names
    - category: "all" (instruments, sounds, drums, audio_effects, midi_effects,
      user_library) or one of those, or "packs", or a path such as
      "sounds/Bass" to search below it
    - limit: Maximum results (default 25, max 200)
    - loadable_only: Skip folders (default true)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        limit = max(1, min(int(limit), 200))
        roots = list(_SEARCH_ROOTS) if category == "all" else [category]
        ableton = get_ableton_connection()
        if require_capability("search_browser", ableton.send_command) is None:
            result = ableton.send_command("search_browser", {
                "query": query, "roots": roots, "limit": limit, "loadable_only": loadable_only})
            return json.dumps(result, indent=2)

        # Fallback for older Remote Scripts: breadth-first over folder listings.
        hits, queue, visited, budget_hit = [], list(roots), 0, False
        while queue and len(hits) < limit:
            if visited >= _SEARCH_FOLDER_BUDGET:
                budget_hit = True
                break
            path = queue.pop(0)
            visited += 1
            try:
                listing = ableton.send_command("get_browser_items_at_path", {"path": path})
            except Exception:
                continue
            for item in listing.get("items", []):
                child = f"{path}/{item['name']}"
                if _name_matches(item["name"], query) and (item.get("is_loadable") or not loadable_only):
                    hits.append({"name": item["name"], "path": child, "uri": item.get("uri"),
                                 "is_loadable": item.get("is_loadable"), "is_folder": item.get("is_folder")})
                    if len(hits) >= limit:
                        break
                if item.get("is_folder"):
                    queue.append(child)
        result = {"query": query, "results": hits, "count": len(hits), "folders_searched": visited}
        if budget_hit:
            result["note"] = (f"stopped after {_SEARCH_FOLDER_BUDGET} folders; "
                              "search a narrower category or path for more")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error searching browser: {str(e)}")
        return f"Error searching browser: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
@trajectory_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Afterwards call get_drum_pads to learn which note triggers which drum
    before writing any drum notes.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
@trajectory_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
@trajectory_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position. The reported
    position is read back after Live has applied the move.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("set_current_song_time", {"time": time})
        # Live applies the move after the command's callback returns, so the
        # value the command itself reports is the old position; read it again.
        position = None
        for _ in range(10):
            _sleep(0.1)
            session = ableton.send_command("get_session_info")
            position = session["current_song_time"]
            if session.get("is_playing") or abs(position - time) < 1e-3:
                break
        if abs(position - time) < 1e-3:
            return f"Playhead moved to beat {position:g}"
        return (f"Playhead is at beat {position:g} (asked for {time:g}"
                + ("; playback is running so it keeps moving)" if session.get("is_playing") else ")"))
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_clips")
@trajectory_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
@trajectory_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


# ── Arrangement building ──────────────────────────────────────────────────────
# Bars here are Live's ruler bars: bar 1 starts at beat 0, and bar ranges are
# inclusive (bars 17-32 = 16 bars). In 4/4 a bar is 4 beats, so bar N starts
# at beat (N-1)*4. Clip lengths and note/automation times stay in beats.

@mcp.tool()
@rich_telemetry_tool("fill_arrangement")
@trajectory_tool("fill_arrangement")
def fill_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    start_bar: float,
    end_bar: float,
    replace: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Place a Session clip into the Arrangement on its own track, repeated
    back-to-back to fill an inclusive bar range. Example: a 4-bar loop
    with start_bar=17, end_bar=32 is placed 4 times, at bars 17, 21, 25, 29.
    Clip automation envelopes travel with the copies. The copies are
    snapshots: after editing the Session clip, call again with replace=true
    to re-place them.

    The range must hold a whole number of repeats of the clip (a 4-bar clip
    cannot fill 6 bars). Existing arrangement clips in the range are refused
    unless replace=true, which deletes clips lying entirely inside the range;
    clips that stick out of it are never cut, the call is refused instead.

    Afterwards call get_arrangement_layout to verify positions, gaps and
    overlaps.

    Parameters:
    - track_index: Track holding the Session clip (the copies go on the same track)
    - clip_index: Session slot of the clip
    - start_bar: First bar, 1-based like Live's ruler
    - end_bar: Last bar, inclusive
    - replace: Remove arrangement clips lying entirely inside the range first
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("fill_arrangement", {
            "track_index": track_index, "clip_index": clip_index,
            "start_bar": start_bar, "end_bar": end_bar, "replace": replace})
        if isinstance(result, str):
            return result
        return (f"Placed '{result['clip_name']}' on '{result['track_name']}' {result['copies']}x "
                f"at bars {', '.join(f'{b:g}' for b in result['start_bars'])}"
                + (f" (replaced {result['replaced']} clip(s))" if result.get("replaced") else ""))
    except Exception as e:
        logger.error(f"Error filling arrangement: {str(e)}")
        return f"Error filling arrangement: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("fill_arrangement_section")
@trajectory_tool("fill_arrangement_section")
def fill_arrangement_section(
    ctx: Context,
    start_bar: float,
    end_bar: float,
    placements: List[Dict[str, int]],
    replace: bool = False,
    user_prompt: str = ""
) -> str:
    """
    Fill one song section in a single call: every (track, Session clip)
    pair is placed back-to-back across the same inclusive bar range, as
    fill_arrangement does for one track. All placements are checked first;
    if any is invalid (wrong length, clip in the way, empty slot) nothing is
    placed. Each track may appear once per call. Placed clips are copies:
    after editing a Session clip, re-run the section with replace=true.

    Typical song build: one call per section, e.g. intro bars 1-8, main
    bars 9-24, outro bars 25-32, then set_locator for each section name and
    get_arrangement_layout to verify.

    Parameters:
    - start_bar: First bar of the section, 1-based
    - end_bar: Last bar of the section, inclusive
    - placements: [{"track_index": 0, "clip_index": 2}, {"track_index": 1, "clip_index": 2}, ...]
    - replace: Remove arrangement clips lying entirely inside the range first
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("fill_arrangement_section", {
            "start_bar": start_bar, "end_bar": end_bar,
            "placements": placements, "replace": replace})
        if isinstance(result, str):
            return result
        lines = [f"- '{p['clip_name']}' on '{p['track_name']}': {p['copies']}x" for p in result["placed"]]
        return f"Filled bars {start_bar:g}-{end_bar:g}:\n" + "\n".join(lines)
    except Exception as e:
        logger.error(f"Error filling arrangement section: {str(e)}")
        return f"Error filling arrangement section: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_loop")
@trajectory_tool("set_arrangement_loop")
def set_arrangement_loop(
    ctx: Context,
    start_bar: float,
    end_bar: float,
    enabled: bool = True,
    user_prompt: str = ""
) -> str:
    """
    Set the Arrangement loop brace to an inclusive bar range (bars 9-16 loops
    8 bars) and switch looping on or off.

    Parameters:
    - start_bar: First bar, 1-based
    - end_bar: Last bar, inclusive
    - enabled: Loop switch on (default) or off
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("set_arrangement_loop", {
            "start_bar": start_bar, "end_bar": end_bar, "enabled": enabled})
        if isinstance(result, str):
            return result
        return (f"Loop {'on' if result['loop'] else 'off'}: bars {result['start_bar']:g}-"
                f"{result['end_bar']:g}")
    except Exception as e:
        logger.error(f"Error setting arrangement loop: {str(e)}")
        return f"Error setting arrangement loop: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_layout")
@trajectory_tool("get_arrangement_layout")
def get_arrangement_layout(
    ctx: Context,
    track_indexes: Optional[List[int]] = None,
    user_prompt: str = ""
) -> str:
    """
    Read the Arrangement as bars: each track's clips (start_bar, end_bar
    inclusive, plus beat times), the gaps and overlaps between them, the
    loop brace, and the locators. Call this after placing clips to verify
    the song form.

    Parameters:
    - track_indexes: Tracks to include (default: all)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("get_arrangement_layout", {"track_indexes": track_indexes})
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error reading arrangement layout: {str(e)}")
        return f"Error reading arrangement layout: {str(e)}"


# Locators: Live's only locator call toggles one at the playhead, and a
# playhead move lands only after the Remote Script callback that made it. So
# each step is its own command, with a short pause for Live to apply it.
_LOCATOR_SETTLE_SEC = 0.15


def _locator_at(ableton, beat: float) -> Optional[Dict[str, Any]]:
    return next((c for c in ableton.send_command("get_locators")["locators"]
                 if abs(c["time"] - beat) < 0.01), None)


def _move_playhead(ableton, beat: float) -> None:
    import time as _time

    ableton.send_command("set_current_song_time", {"time": beat})
    for _ in range(10):
        _time.sleep(_LOCATOR_SETTLE_SEC)
        if abs(ableton.send_command("get_session_info")["current_song_time"] - beat) < 0.01:
            return
    raise Exception(f"Playhead did not reach beat {beat}")


def _toggle_locator(ableton, beat: float) -> None:
    """Toggle the cue at beat, refusing if the playhead isn't exactly there
    (a toggle elsewhere would add or delete the wrong locator)."""
    import time as _time

    if ableton.send_command("get_session_info")["is_playing"]:
        raise Exception("Stop playback first: locators are placed at the playhead")
    restore = ableton.send_command("get_session_info")["current_song_time"]
    _move_playhead(ableton, beat)
    try:
        playhead = ableton.send_command("toggle_cue_at_playhead")["playhead"]
        if abs(playhead - beat) > 0.01:
            raise Exception(f"Locator toggled at beat {playhead}, not {beat}")
        _time.sleep(_LOCATOR_SETTLE_SEC)
    finally:
        ableton.send_command("set_current_song_time", {"time": restore})


@mcp.tool()
@rich_telemetry_tool("set_locator")
@trajectory_tool("set_locator")
def set_locator(
    ctx: Context,
    name: str,
    bar: float,
    user_prompt: str = ""
) -> str:
    """
    Add a named locator (section marker such as "Intro", "Drop") at the
    start of a bar, or rename the one already there. Playback must be
    stopped. The playhead is moved temporarily and restored.

    Parameters:
    - name: Locator name
    - bar: Bar number, 1-based (bar 17 = beat 64 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        for cap in ("get_locators", "toggle_cue_at_playhead", "rename_locator"):
            missing = require_capability(cap, ableton.send_command)
            if missing:
                return missing
        beat = (bar - 1) * ableton.send_command("get_locators")["beats_per_bar"]
        existed = _locator_at(ableton, beat) is not None
        if not existed:
            _toggle_locator(ableton, beat)
            if _locator_at(ableton, beat) is None:
                return f"Error setting locator: Live did not create a locator at bar {bar:g}"
        result = ableton.send_command("rename_locator", {"time": beat, "name": name})
        return f"{'Renamed' if existed else 'Added'} locator '{result['name']}' at bar {result['bar']:g}"
    except Exception as e:
        logger.error(f"Error setting locator: {str(e)}")
        return f"Error setting locator: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("delete_locator")
@trajectory_tool("delete_locator")
def delete_locator(
    ctx: Context,
    bar: float,
    expected_name: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Delete the locator at the start of a bar. Playback must be stopped.

    Parameters:
    - bar: Bar number, 1-based
    - expected_name: Optional; refuse unless the locator has exactly this name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        for cap in ("get_locators", "toggle_cue_at_playhead"):
            missing = require_capability(cap, ableton.send_command)
            if missing:
                return missing
        beat = (bar - 1) * ableton.send_command("get_locators")["beats_per_bar"]
        cue = _locator_at(ableton, beat)
        if cue is None:
            return f"Error deleting locator: no locator at bar {bar:g}"
        if expected_name is not None and cue["name"] != expected_name:
            return (f"Error deleting locator: Refusing to delete: locator at bar {bar:g} is named "
                    f"'{cue['name']}', not '{expected_name}'")
        _toggle_locator(ableton, beat)
        if _locator_at(ableton, beat) is not None:
            return f"Error deleting locator: locator at bar {bar:g} is still there"
        return f"Deleted locator '{cue['name']}' at bar {bar:g}"
    except Exception as e:
        logger.error(f"Error deleting locator: {str(e)}")
        return f"Error deleting locator: {str(e)}"


# ── In-place note editing ─────────────────────────────────────────────────────

@mcp.tool()
@rich_telemetry_tool("modify_notes")
@trajectory_tool("modify_notes")
def modify_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    selection: Dict[str, Any],
    changes: Dict[str, Any],
    user_prompt: str = ""
) -> str:
    """
    Edit existing notes in a MIDI clip in place, without clearing it. Note
    IDs are kept. Read the notes first with get_clip_notes.
    Arrangement copies of the clip do not follow these edits; re-place them
    with fill_arrangement(_section) replace=true.

    selection picks notes in exactly one of two ways:
    - {"note_ids": [3, 7]}: the note_id values returned by get_clip_notes
    - a range: any of {"pitch_min": 36, "pitch_max": 38} (inclusive) and
      {"time_min": 0, "time_max": 4} (beats from the clip start; a note is
      selected when its start time is >= time_min and < time_max)

    changes applies to every selected note. Absolute or relative, not both
    for the same property:
    - "pitch": 60 or "transpose": 7 (semitones, may be negative)
    - "velocity": 90 or "velocity_delta": -20 (result must stay 1..127)
    - "start_time": 2.0 or "shift": 0.25 (beats)
    - "duration": 0.5 (beats)
    - "mute": true/false
    If any selected note would end up invalid (pitch outside 0..127,
    velocity outside 1..127, start before beat 0, duration <= 0), nothing is
    changed.

    Parameters:
    - track_index: Track holding the clip
    - clip_index: Session slot index
    - selection: Which notes, see above
    - changes: What to change, see above
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("modify_notes", {
            "track_index": track_index, "clip_index": clip_index,
            "selection": selection, "changes": changes})
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error modifying notes: {str(e)}")
        return f"Error modifying notes: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("delete_notes")
@trajectory_tool("delete_notes")
def delete_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    selection: Dict[str, Any],
    user_prompt: str = ""
) -> str:
    """
    Delete specific notes from a MIDI clip. To empty the whole clip use
    clear_notes_from_clip instead; an empty selection is refused.
    Arrangement copies of the clip do not follow these edits; re-place them
    with fill_arrangement(_section) replace=true.

    selection picks notes in exactly one of two ways:
    - {"note_ids": [3, 7]}: the note_id values returned by get_clip_notes
    - a range: any of {"pitch_min": 42, "pitch_max": 42} (inclusive) and
      {"time_min": 0, "time_max": 4} (beats from the clip start; a note is
      selected when its start time is >= time_min and < time_max)

    Parameters:
    - track_index: Track holding the clip
    - clip_index: Session slot index
    - selection: Which notes, see above
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("delete_notes", {
            "track_index": track_index, "clip_index": clip_index, "selection": selection})
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting notes: {str(e)}")
        return f"Error deleting notes: {str(e)}"


# ── Groove pool ───────────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("get_groove_pool")
@trajectory_tool("get_groove_pool")
def get_groove_pool(ctx: Context, user_prompt: str = "") -> str:
    """
    List the grooves in the Set's Groove Pool (index, name, timing, random,
    velocity, quantize amounts) and the global groove amount. Which groove a
    clip uses is shown per clip in get_track_info ("groove").

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("get_groove_pool", {})
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error reading groove pool: {str(e)}")
        return f"Error reading groove pool: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_groove")
@trajectory_tool("load_groove")
def load_groove(
    ctx: Context,
    uri: Optional[str] = None,
    name: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Add a groove from Live's library to the Groove Pool and return its
    index for assign_groove. A groove already in the pool is reused.

    Give exactly one of:
    - uri: from get_browser_items_at_path, e.g. browse
      "packs/Core Library/Grooves/Swing/MPC"
    - name: a groove file name such as "Swing MPC 3000 8ths 57" (searched
      under Core Library > Grooves and User Library > Grooves)

    Parameters:
    - uri: Browser URI of a .agr groove
    - name: Groove name, with or without .agr
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("load_groove", {"uri": uri, "name": name})
        if isinstance(result, str):
            return result
        return (f"Groove '{result['name']}' is pool index {result['groove_index']}"
                + (" (loaded)" if result["loaded"] else " (already in pool)"))
    except Exception as e:
        logger.error(f"Error loading groove: {str(e)}")
        return f"Error loading groove: {str(e)}"


def _clip_list(clip_index: Optional[int], clip_indexes: Optional[List[int]]) -> List[int]:
    if (clip_index is None) == (not clip_indexes):
        raise ValueError("give exactly one of clip_index or clip_indexes")
    return [clip_index] if clip_index is not None else list(clip_indexes)


def _groove_amount_warning(ableton) -> str:
    amount = ableton.send_command("get_groove_pool")["global_amount"]
    if amount <= 0:
        return ("\nWARNING: the global groove amount is 0%, so no groove is heard; raise it "
                "with set_groove_amount (1.0 = 100%). Clips you don't want swung should get "
                "clear_clip_groove first, because new clips often carry the Set's default groove.")
    return ""


@mcp.tool()
@rich_telemetry_tool("assign_groove")
@trajectory_tool("assign_groove")
def assign_groove(
    ctx: Context,
    track_index: int,
    groove_index: int,
    clip_index: Optional[int] = None,
    clip_indexes: Optional[List[int]] = None,
    user_prompt: str = ""
) -> str:
    """
    Assign a Groove Pool groove to one Session clip, or several on the same
    track (clip_indexes). The groove's timing, velocity and randomness apply
    on playback, scaled by the global groove amount (shown in get_groove_pool
    and get_session_info; it is 0% in a new Live 12 Set, which silences every
    groove). Check per clip with get_track_info ("groove").

    To remove swing from a clip use clear_clip_groove. Don't use
    set_groove_amount(0) for that: it disables every groove in the Set.

    Parameters:
    - track_index: Track holding the clip(s)
    - groove_index: Index from get_groove_pool / load_groove
    - clip_index: One Session slot (give this or clip_indexes)
    - clip_indexes: Several Session slots on the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        slots = _clip_list(clip_index, clip_indexes)
        lines = []
        for slot in slots:
            result = _gated_call("assign_groove", {
                "track_index": track_index, "clip_index": slot, "groove_index": groove_index})
            if isinstance(result, str):
                return result
            lines.append(f"Clip '{result['clip_name']}' (slot {slot}) groove: {result['groove']}")
        return "\n".join(lines) + _groove_amount_warning(get_ableton_connection())
    except Exception as e:
        logger.error(f"Error assigning groove: {str(e)}")
        return f"Error assigning groove: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("clear_clip_groove")
@trajectory_tool("clear_clip_groove")
def clear_clip_groove(
    ctx: Context,
    track_index: int,
    clip_index: Optional[int] = None,
    clip_indexes: Optional[List[int]] = None,
    user_prompt: str = ""
) -> str:
    """
    Make Session clip(s) play straight, with no groove.

    Live's API cannot set a clip's groove to None, so this assigns a neutral
    groove instead: "AbletonMCP No Groove" in the Set's Groove Pool (Live's
    "Quantize 16" with timing, random, velocity and quantize all 0, keeping
    that name if Live refuses the rename), loaded the first time it's
    needed. Don't change that groove's amounts.

    A new Live 12 Set gives every new clip its default groove ("Swing 16ths
    66"); clear it on clips that should stay straight before raising the
    global groove amount.

    Parameters:
    - track_index: Track holding the clip(s)
    - clip_index: One Session slot (give this or clip_indexes)
    - clip_indexes: Several Session slots on the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        slots = _clip_list(clip_index, clip_indexes)
        lines = []
        for slot in slots:
            result = _gated_call("clear_clip_groove", {"track_index": track_index, "clip_index": slot})
            if isinstance(result, str):
                return result
            lines.append(f"Clip '{result['clip_name']}' (slot {slot}) now plays straight "
                         f"(groove '{result['groove']}')")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error clearing clip groove: {str(e)}")
        return f"Error clearing clip groove: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_groove_amount")
@trajectory_tool("set_groove_amount")
def set_groove_amount(ctx: Context, amount: float, user_prompt: str = "") -> str:
    """
    Set the global groove amount, which scales every groove in the Set:
    0.0 = off, 1.0 = 100%, up to 1.3 = 130%. A new Live 12 Set starts at 0%,
    so assigned grooves are silent until this is raised. To keep a single
    clip straight use clear_clip_groove, not 0% here (that silences every
    groove in the Set).

    Parameters:
    - amount: 0.0 to 1.3
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("set_groove_amount", {"amount": amount})
        return result if isinstance(result, str) else f"Global groove amount: {result['global_amount']:g}"
    except Exception as e:
        logger.error(f"Error setting groove amount: {str(e)}")
        return f"Error setting groove amount: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_groove_params")
@trajectory_tool("set_groove_params")
def set_groove_params(
    ctx: Context,
    groove_index: int,
    timing: Optional[float] = None,
    random: Optional[float] = None,
    velocity: Optional[float] = None,
    quantize: Optional[float] = None,
    user_prompt: str = ""
) -> str:
    """
    Adjust one groove in the pool. Values use the same units get_groove_pool
    reports; omitted ones are left as they are.

    Parameters:
    - groove_index: Index from get_groove_pool
    - timing: How strongly the groove moves note timing
    - random: Random timing amount
    - velocity: How strongly the groove changes velocity
    - quantize: How much notes are quantized before the groove applies
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("set_groove_params", {
            "groove_index": groove_index, "timing": timing, "random": random,
            "velocity": velocity, "quantize": quantize})
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error setting groove params: {str(e)}")
        return f"Error setting groove params: {str(e)}"


# ── Dataset / preference tools (Supabase trajectory recording) ─────────────────

@mcp.tool()
@telemetry_tool("submit_intent")
@trajectory_tool("submit_intent", modifying=False)
def submit_intent(
    ctx: Context,
    text: str,
    level: int = 5,
    user_prompt: str = "",
) -> str:
    """
    Record the human's creative intent for subsequent actions in this session.

    Call this before a sequence of edits so trajectory steps are conditioned on intent.
    Levels: 1=atomic, 2=musical op, 3=section, 4=song, 5=creative goal.

    Requires telemetry consent. Data is stored in Supabase (same project as telemetry).

    Parameters:
    - text: Natural-language intent (e.g. "make the chorus feel bigger")
    - level: Hierarchical intent level 1–5 (default 5)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.set_intent(text=text, level=level)
        return (
            f"Recorded intent {event.intent_id} (level {level}): {text!r}. "
            f"Session: {recorder.session_id}"
        )
    except Exception as e:
        logger.error(f"Error submitting intent: {str(e)}")
        return f"Error submitting intent: {str(e)}"


@mcp.tool()
@telemetry_tool("rate_last_action")
@trajectory_tool("rate_last_action", modifying=False)
def rate_last_action(
    ctx: Context,
    rating: str,
    tags: str = "",
    note: str = "",
    user_prompt: str = "",
) -> str:
    """
    Rate the most recent recorded action (or the track after an edit).

    Ratings: better | same | worse | keep | reject | thumbs_up | thumbs_down
    Optional tags (comma-separated): groove, harmony, melody, sound, arrangement,
    energy, mix, emotion

    Requires telemetry consent.

    Parameters:
    - rating: Preference label
    - tags: Comma-separated aspect tags
    - note: Optional free-text reason
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        event = recorder.record_preference(
            rating=rating.strip().lower(),
            tags=tag_list,
            note=note or None,
        )
        target = event.target_action_id or "none"
        return f"Recorded preference {rating!r} for action {target}"
    except Exception as e:
        logger.error(f"Error rating action: {str(e)}")
        return f"Error rating action: {str(e)}"


@mcp.tool()
@telemetry_tool("prefer_candidate")
@trajectory_tool("prefer_candidate", modifying=False)
def prefer_candidate(
    ctx: Context,
    candidate_a: str,
    candidate_b: str,
    winner: str,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Record a pairwise preference between two candidate actions or auditions.

    winner should be 'a', 'b', or an explicit candidate id matching A or B.

    Requires telemetry consent.

    Parameters:
    - candidate_a: Id / URI / label for option A
    - candidate_b: Id / URI / label for option B
    - winner: 'a', 'b', or the winning id
    - reason: Optional reason ("C has the right attack")
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        w = winner.strip().lower()
        if w == "a":
            w = candidate_a
        elif w == "b":
            w = candidate_b
        event = recorder.record_preference(
            rating="pairwise",
            winner=w,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            note=reason or None,
        )
        return f"Recorded pairwise preference: winner={w!r} (event {event.event_id})"
    except Exception as e:
        logger.error(f"Error recording pairwise preference: {str(e)}")
        return f"Error recording pairwise preference: {str(e)}"


@mcp.tool()
@telemetry_tool("reject_last_action")
@trajectory_tool("reject_last_action", modifying=False)
def reject_last_action(
    ctx: Context,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Mark the last action as rejected (exploration / preference boundary).

    Use when the human undoes or discards an agent edit. Requires telemetry consent.

    Parameters:
    - reason: Optional why it was rejected
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_preference(
            rating="reject",
            note=reason or None,
        )
        return f"Recorded rejection for action {event.target_action_id or 'none'}"
    except Exception as e:
        logger.error(f"Error rejecting action: {str(e)}")
        return f"Error rejecting action: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("record_audition")
@trajectory_tool("record_audition", modifying=False)
def record_audition(
    ctx: Context,
    uri: str,
    kept: bool = False,
    search_query: str = "",
    dwell_ms: float = 0.0,
    user_prompt: str = "",
) -> str:
    """
    Log a browser/preset/sample audition (keep or reject) for preference learning.

    Call once per candidate auditioned. Does not load the device — use
    load_instrument_or_effect when kept=True and you want to commit.

    Requires telemetry consent.

    Parameters:
    - uri: Browser item URI (or stable preset/sample id)
    - kept: Whether this candidate was kept
    - search_query: Optional search text that led here (e.g. "analog bass")
    - dwell_ms: Optional time spent auditioning
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_audition(
            uri=uri,
            kept=kept,
            search_query=search_query or None,
            dwell_ms=dwell_ms if dwell_ms > 0 else None,
        )
        status = "kept" if kept else "rejected"
        return f"Recorded audition ({status}) uri={uri!r} event={event.event_id}"
    except Exception as e:
        logger.error(f"Error recording audition: {str(e)}")
        return f"Error recording audition: {str(e)}"


# ── Mixer tools ───────────────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("get_mixer")
@trajectory_tool("get_mixer")
def get_mixer(ctx: Context, track_index: int = 0, track_type: str = "track",
              user_prompt: str = "") -> str:
    """
    Get the mixer state (volume, panning and sends) for a track.

    Each value is reported three ways: the raw 0..1 parameter value, the dB it
    corresponds to, and the string Live itself displays.

    Parameters:
    - track_index: The index of the track (ignored when track_type is "master")
    - track_type: "track" (default), "return", or "master"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("get_mixer", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("get_mixer", {
            "track_index": track_index,
            "track_type": track_type
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting mixer: {str(e)}")
        return f"Error getting mixer: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_track_volume")
@trajectory_tool("set_track_volume")
def set_track_volume(
    ctx: Context,
    track_index: int,
    db: Optional[float] = None,
    value: Optional[float] = None,
    track_type: str = "track",
    user_prompt: str = ""
) -> str:
    """
    Set a track's volume fader.

    Prefer `db` — Live's fader taper is non-linear, so the raw 0..1 value is not
    proportional to level (0.85 is 0 dB, 1.0 is +6 dB). Passing `db` resolves the
    exact fader position for that level using Live's own readout.

    Parameters:
    - track_index: The index of the track (ignored when track_type is "master")
    - db: Target level in decibels, e.g. -4.0. Clamped to the fader range.
    - value: Raw fader position 0.0-1.0, as an alternative to db
    - track_type: "track" (default), "return", or "master"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        if db is None and value is None:
            return "Error: provide either db or value"
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("set_track_volume", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("set_track_volume", {
            "track_index": track_index,
            "db": db,
            "value": value,
            "track_type": track_type
        })
        return f"Set volume of '{result.get('name')}' to {result.get('display')}"
    except Exception as e:
        logger.error(f"Error setting track volume: {str(e)}")
        return f"Error setting track volume: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_track_panning")
@trajectory_tool("set_track_panning")
def set_track_panning(
    ctx: Context,
    track_index: int,
    value: float,
    track_type: str = "track",
    user_prompt: str = ""
) -> str:
    """
    Set a track's pan position.

    Parameters:
    - track_index: The index of the track (ignored when track_type is "master")
    - value: -1.0 is hard left, 0.0 is centre, 1.0 is hard right
    - track_type: "track" (default), "return", or "master"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("set_track_panning", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("set_track_panning", {
            "track_index": track_index,
            "value": value,
            "track_type": track_type
        })
        return f"Set panning of '{result.get('name')}' to {result.get('display')}"
    except Exception as e:
        logger.error(f"Error setting track panning: {str(e)}")
        return f"Error setting track panning: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_send")
@trajectory_tool("set_send")
def set_send(
    ctx: Context,
    track_index: int,
    send_index: int,
    db: Optional[float] = None,
    value: Optional[float] = None,
    track_type: str = "track",
    user_prompt: str = ""
) -> str:
    """
    Set one of a track's send levels.

    As with volume, prefer `db` over the raw value. The master track has no sends.

    Parameters:
    - track_index: The index of the track
    - send_index: Which send to set (0 is the first return track, A)
    - db: Target send level in decibels, e.g. -12.0
    - value: Raw send position 0.0-1.0, as an alternative to db
    - track_type: "track" (default) or "return"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        if db is None and value is None:
            return "Error: provide either db or value"
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("set_send", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("set_send", {
            "track_index": track_index,
            "send_index": send_index,
            "db": db,
            "value": value,
            "track_type": track_type
        })
        return f"Set send {result.get('send')} on '{result.get('name')}' to {result.get('display')}"
    except Exception as e:
        logger.error(f"Error setting send: {str(e)}")
        return f"Error setting send: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_track_mute")
@trajectory_tool("set_track_mute")
def set_track_mute(
    ctx: Context,
    track_index: int,
    mute: bool,
    track_type: str = "track",
    user_prompt: str = ""
) -> str:
    """
    Mute or unmute a track. The master track has no mute.

    Parameters:
    - track_index: The index of the track
    - mute: True to mute, False to unmute
    - track_type: "track" (default) or "return"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("set_track_mute", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("set_track_mute", {
            "track_index": track_index,
            "mute": mute,
            "track_type": track_type
        })
        return f"{'Muted' if result.get('mute') else 'Unmuted'} '{result.get('name')}'"
    except Exception as e:
        logger.error(f"Error setting track mute: {str(e)}")
        return f"Error setting track mute: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_track_solo")
@trajectory_tool("set_track_solo")
def set_track_solo(
    ctx: Context,
    track_index: int,
    solo: bool,
    track_type: str = "track",
    user_prompt: str = ""
) -> str:
    """
    Solo or unsolo a track. The master track has no solo. With Live's
    exclusive-solo preference on, soloing one track unsolos the others.

    Parameters:
    - track_index: The index of the track
    - solo: True to solo, False to unsolo
    - track_type: "track" (default) or "return"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("set_track_solo", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("set_track_solo", {
            "track_index": track_index,
            "solo": solo,
            "track_type": track_type
        })
        return f"{'Soloed' if result.get('solo') else 'Unsoloed'} '{result.get('name')}'"
    except Exception as e:
        logger.error(f"Error setting track solo: {str(e)}")
        return f"Error setting track solo: {str(e)}"

# ── Rack tools: drum pads and devices inside racks ─────────────────────────────

@mcp.tool()
@telemetry_tool("get_drum_pads")
@trajectory_tool("get_drum_pads")
def get_drum_pads(
    ctx: Context,
    track_index: int,
    device_index: Optional[int] = None,
    user_prompt: str = ""
) -> str:
    """
    List the filled pads of a Drum Rack: MIDI note, note name (36 = C1), pad
    name, and the devices in each pad's chain.

    ALWAYS call this before writing notes to a drum track, and take each
    drum's pitch from the "note" field here. Never assume General MIDI
    numbers (36 = kick, 38 = snare): kits differ, and a guessed note plays the
    wrong drum or nothing.

    Parameters:
    - track_index: The track holding the Drum Rack
    - device_index: The Drum Rack's index in the track's devices. Omit when the
      track has exactly one top-level Drum Rack.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("get_drum_pads", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("get_drum_pads", {
            "track_index": track_index,
            "device_index": device_index,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting drum pads: {str(e)}")
        return f"Error getting drum pads: {str(e)}"

@mcp.tool()
@telemetry_tool("get_nested_device_parameters")
@trajectory_tool("get_nested_device_parameters")
def get_nested_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_index: Optional[int] = None,
    pad_note: Optional[int] = None,
    nested_device_index: int = 0,
    path: Optional[List[int]] = None,
    user_prompt: str = ""
) -> str:
    """
    Read the parameters of a device inside a rack (Drum Rack, Instrument Rack,
    Audio Effect Rack, MIDI Effect Rack).

    Address the device with exactly one of:
    - pad_note: a Drum Rack pad's MIDI note (from get_drum_pads), or
    - chain_index: the chain's index in the rack (any rack type; get_drum_pads
      also reports each pad's chain_index)
    then nested_device_index for the device in that chain.

    Racks can nest (stock drum kits put an Instrument Rack on each pad, with
    the sampler inside it). The result lists a nested rack's chains and
    devices; to reach one, add path=[chain_index, device_index, ...] pairs,
    e.g. path=[0, 0] for the first device of the nested rack's first chain.

    Parameters:
    - track_index: The track holding the rack
    - device_index: The rack's index in the track's devices
    - chain_index: Chain in the rack (give this or pad_note)
    - pad_note: Drum Rack pad note, 0-127 (give this or chain_index)
    - nested_device_index: Device index within that chain (default 0)
    - path: Optional deeper [chain_index, device_index, ...] pairs
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("get_nested_device_parameters", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("get_nested_device_parameters", {
            "track_index": track_index,
            "device_index": device_index,
            "chain_index": chain_index,
            "pad_note": pad_note,
            "nested_device_index": nested_device_index,
            "path": path,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting nested device parameters: {str(e)}")
        return f"Error getting nested device parameters: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_nested_device_parameter")
@trajectory_tool("set_nested_device_parameter")
def set_nested_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    value: float,
    chain_index: Optional[int] = None,
    pad_note: Optional[int] = None,
    nested_device_index: int = 0,
    path: Optional[List[int]] = None,
    parameter_index: Optional[int] = None,
    parameter_name: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Set one parameter of a device inside a rack, e.g. a kick pad's Decay or
    Transpose. Addressing is the same as get_nested_device_parameters; read
    that first for parameter names, indices and min/max ranges.

    Give exactly one of parameter_name (case-insensitive exact match) or
    parameter_index. value is in the parameter's own units and must lie
    within its min..max; out-of-range values are refused, not clamped.

    Parameters:
    - track_index: The track holding the rack
    - device_index: The rack's index in the track's devices
    - value: New value within the parameter's min..max
    - chain_index: Chain in the rack (give this or pad_note)
    - pad_note: Drum Rack pad note, 0-127 (give this or chain_index)
    - nested_device_index: Device index within that chain (default 0)
    - path: Optional deeper [chain_index, device_index, ...] pairs
    - parameter_index: Parameter index (give this or parameter_name)
    - parameter_name: Parameter name, e.g. "Decay" (give this or parameter_index)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("set_nested_device_parameter", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("set_nested_device_parameter", {
            "track_index": track_index,
            "device_index": device_index,
            "value": value,
            "chain_index": chain_index,
            "pad_note": pad_note,
            "nested_device_index": nested_device_index,
            "path": path,
            "parameter_index": parameter_index,
            "parameter_name": parameter_name,
        })
        return (
            f"Set {result.get('location')} > {result.get('name')} "
            f"{result.get('old_value')} → {result.get('value')} ({result.get('display')})"
        )
    except Exception as e:
        logger.error(f"Error setting nested device parameter: {str(e)}")
        return f"Error setting nested device parameter: {str(e)}"

@mcp.tool()
@telemetry_tool("create_return_track")
@trajectory_tool("create_return_track")
def create_return_track(ctx: Context, user_prompt: str = "") -> str:
    """
    Create a return track at the end of the session.

    Returns the index of the newly created return track. Every track gains a
    send to it at that same index (set it with set_send). Load effects onto it
    with load_instrument_or_effect(track_type="return").

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        ableton = get_ableton_connection()
        missing = require_capability("create_return_track", ableton.send_command)
        if missing:
            return missing
        result = ableton.send_command("create_return_track", {})
        return f"Created return track '{result.get('name')}' at index {result.get('return_track_index')} (total returns: {result.get('return_track_count')})"
    except Exception as e:
        logger.error(f"Error creating return track: {str(e)}")
        return f"Error creating return track: {str(e)}"

# ── Clip automation tools ─────────────────────────────────────────────────────

def _gated_call(command: str, params: Dict[str, Any]) -> Dict[str, Any] | str:
    from .script_handshake import require_capability

    ableton = get_ableton_connection()
    missing = require_capability(command, ableton.send_command)
    if missing:
        return missing
    return ableton.send_command(command, params)


@mcp.tool()
@telemetry_tool("get_clip_automation")
@trajectory_tool("get_clip_automation")
def get_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int,
    target: Dict[str, Any],
    times: Optional[List[float]] = None,
    view: str = "session",
    user_prompt: str = ""
) -> str:
    """
    Read a clip's automation envelope for one parameter, sampled at given
    times (beats from the clip start; default: 9 points spread over the clip).
    Returns has_envelope=false when the parameter is not automated in this clip.
    Session clips only in practice: Live does not expose Arrangement clips'
    envelopes, so view="arrangement" always reports has_envelope=false even
    though envelopes copied there by fill_arrangement do play.
    
    target picks the parameter, on the same track as the clip. Either:
    - a mixer control: {"mixer": "volume"} | {"mixer": "panning"} |
      {"mixer": "send", "send_index": 0}
    - a device parameter: {"device_index": 1, "parameter_name": "Frequency"}
      (or "parameter_index"). For a device inside a rack add the nested
      address used by get_nested_device_parameters: "chain_index" or
      "pad_note", plus "nested_device_index" and optional "path".
    Read get_device_parameters / get_nested_device_parameters first for the
    parameter's exact name and its min..max range.

    Parameters:
    - track_index: Track holding the clip
    - clip_index: Session slot index (view="session")
    - target: Parameter to read, see above
    - times: Optional list of beat positions to sample
    - view: "session" (default) or "arrangement"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("get_clip_automation", {
            "track_index": track_index, "clip_index": clip_index,
            "target": target, "times": times, "view": view})
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error reading clip automation: {str(e)}")
        return f"Error reading clip automation: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("write_clip_automation")
@trajectory_tool("write_clip_automation")
def write_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int,
    target: Dict[str, Any],
    points: List[Dict[str, float]],
    curve: str = "linear",
    resolution: float = 0.0625,
    clear_existing: bool = True,
    view: str = "session",
    user_prompt: str = ""
) -> str:
    """
    Draw an automation envelope on a Session clip from breakpoints, e.g. a
    filter sweep or a volume fade. The envelope plays whenever the clip plays,
    including after duplicate_to_arrangement.

    Time: each point's "time" is in BEATS from the clip's start (4 beats = 1
    bar in 4/4; an 8-bar sweep runs 0 to 32). Times must lie within the
    clip's length.

    Value: give each point exactly one of
    - "value": the parameter's own units, within its min..max (as shown by
      get_device_parameters; mixer volume is 0..1 with 0.85 = 0 dB), or
    - "normalized": 0..1 across the parameter's range (0 = min, 1 = max).
    Out-of-range values are refused, not clamped.

    Before the first point the first value holds; after the last point the
    last value holds to the clip end.
    
    target picks the parameter, on the same track as the clip. Either:
    - a mixer control: {"mixer": "volume"} | {"mixer": "panning"} |
      {"mixer": "send", "send_index": 0}
    - a device parameter: {"device_index": 1, "parameter_name": "Frequency"}
      (or "parameter_index"). For a device inside a rack add the nested
      address used by get_nested_device_parameters: "chain_index" or
      "pad_note", plus "nested_device_index" and optional "path".
    Read get_device_parameters / get_nested_device_parameters first for the
    parameter's exact name and its min..max range.

    Example, 8-bar rising cutoff on Auto Filter at device 0:
      target={"device_index": 0, "parameter_name": "Frequency"},
      points=[{"time": 0, "normalized": 0.1}, {"time": 32, "normalized": 0.9}]

    Parameters:
    - track_index: Track holding the clip
    - clip_index: Session slot index
    - target: Parameter to automate, see above
    - points: [{"time": beats, "value": v} or {"time": beats, "normalized": 0..1}, ...]
    - curve: "linear" (ramps between points, default) or "step" (hold each value until the next point)
    - resolution: Ramp step length in beats (default 1/16 beat); larger = fewer steps
    - clear_existing: Replace any existing envelope for this parameter (default true)
    - view: "session" (default). "arrangement" is attempted but Live may refuse it.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("write_clip_automation", {
            "track_index": track_index, "clip_index": clip_index, "target": target,
            "points": points, "curve": curve, "resolution": resolution,
            "clear_existing": clear_existing, "view": view})
        if isinstance(result, str):
            return result
        return (
            f"Automated {result.get('location')} on clip '{result.get('clip_name')}': "
            f"{result.get('points')} points over beats {result.get('start')}–{result.get('end')}, "
            f"{result.get('first_display')} → {result.get('last_display')} ({result.get('steps')} steps)"
        )
    except Exception as e:
        logger.error(f"Error writing clip automation: {str(e)}")
        return f"Error writing clip automation: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("clear_clip_automation")
@trajectory_tool("clear_clip_automation")
def clear_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int,
    target: Optional[Dict[str, Any]] = None,
    all_parameters: bool = False,
    view: str = "session",
    user_prompt: str = ""
) -> str:
    """
    Remove automation from one clip: one parameter's envelope (target), or
    every envelope in the clip (all_parameters=true). Give exactly one.
    
    target picks the parameter, on the same track as the clip. Either:
    - a mixer control: {"mixer": "volume"} | {"mixer": "panning"} |
      {"mixer": "send", "send_index": 0}
    - a device parameter: {"device_index": 1, "parameter_name": "Frequency"}
      (or "parameter_index"). For a device inside a rack add the nested
      address used by get_nested_device_parameters: "chain_index" or
      "pad_note", plus "nested_device_index" and optional "path".
    Read get_device_parameters / get_nested_device_parameters first for the
    parameter's exact name and its min..max range.

    Parameters:
    - track_index: Track holding the clip
    - clip_index: Session slot index
    - target: Parameter whose envelope to remove, see above
    - all_parameters: true to remove every envelope in this clip
    - view: "session" (default) or "arrangement"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _gated_call("clear_clip_automation", {
            "track_index": track_index, "clip_index": clip_index, "target": target,
            "all_parameters": all_parameters, "view": view})
        if isinstance(result, str):
            return result
        return f"Cleared automation ({result.get('cleared')}) on clip '{result.get('clip_name')}'"
    except Exception as e:
        logger.error(f"Error clearing clip automation: {str(e)}")
        return f"Error clearing clip automation: {str(e)}"

# ── Delete tools ──────────────────────────────────────────────────────────────
# One target per call, by explicit index. Indexes after the deleted item shift
# down, so re-read (get_session_info / get_track_info) before the next delete,
# or pass expected_name so a shifted index is refused instead of deleting the
# wrong thing.

def _run_delete(command: str, params: Dict[str, Any], what: str) -> str:
    from .script_handshake import require_capability

    ableton = get_ableton_connection()
    missing = require_capability(command, ableton.send_command)
    if missing:
        return missing
    result = ableton.send_command(command, params)
    where = f" from {result['location']}" if result.get("location") else (
        f" from '{result['track_name']}'" if result.get("track_name") else "")
    return f"Deleted {what} '{result.get('name')}'{where}"

@mcp.tool()
@telemetry_tool("delete_track")
def delete_track(
    ctx: Context,
    track_index: int,
    expected_name: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Delete one track (regular MIDI/audio/group track) and everything on it.
    Live cannot delete its last remaining track. Live renumbers auto-named
    tracks ("3-Audio" becomes "5-Audio") when tracks are inserted, so
    re-read names right before using expected_name.

    Parameters:
    - track_index: The index of the track to delete
    - expected_name: Optional; refuse unless the track has exactly this name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        return _run_delete("delete_track", {"track_index": track_index,
                                            "expected_name": expected_name}, "track")
    except Exception as e:
        logger.error(f"Error deleting track: {str(e)}")
        return f"Error deleting track: {str(e)}"

@mcp.tool()
@telemetry_tool("delete_return_track")
def delete_return_track(
    ctx: Context,
    return_index: int,
    expected_name: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Delete one return track. Every track's send to it is removed too.

    Parameters:
    - return_index: The return track's index (0 = A)
    - expected_name: Optional; refuse unless the return track has exactly this name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        return _run_delete("delete_return_track", {"return_index": return_index,
                                                   "expected_name": expected_name}, "return track")
    except Exception as e:
        logger.error(f"Error deleting return track: {str(e)}")
        return f"Error deleting return track: {str(e)}"

@mcp.tool()
@telemetry_tool("delete_device")
def delete_device(
    ctx: Context,
    track_index: int,
    device_index: int,
    track_type: str = "track",
    expected_name: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Delete one top-level device from a track's device chain. To delete a
    device inside a rack, use delete_nested_device.

    Parameters:
    - track_index: The track's index (ignored when track_type is "master")
    - device_index: The device's index in the track's devices
    - track_type: "track" (default), "return", or "master"
    - expected_name: Optional; refuse unless the device has exactly this name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        return _run_delete("delete_device", {"track_index": track_index,
                                             "device_index": device_index,
                                             "track_type": track_type,
                                             "expected_name": expected_name}, "device")
    except Exception as e:
        logger.error(f"Error deleting device: {str(e)}")
        return f"Error deleting device: {str(e)}"

@mcp.tool()
@telemetry_tool("delete_nested_device")
def delete_nested_device(
    ctx: Context,
    track_index: int,
    device_index: int,
    chain_index: Optional[int] = None,
    pad_note: Optional[int] = None,
    nested_device_index: int = 0,
    path: Optional[List[int]] = None,
    expected_name: Optional[str] = None,
    user_prompt: str = ""
) -> str:
    """
    Delete one device inside a rack chain. Addressing is the same as
    get_nested_device_parameters: exactly one of chain_index or pad_note,
    then nested_device_index, plus optional deeper path pairs.

    Parameters:
    - track_index: The track holding the rack
    - device_index: The rack's index in the track's devices
    - chain_index: Chain in the rack (give this or pad_note)
    - pad_note: Drum Rack pad note, 0-127 (give this or chain_index)
    - nested_device_index: Device index within that chain (default 0)
    - path: Optional deeper [chain_index, device_index, ...] pairs
    - expected_name: Optional; refuse unless the device has exactly this name
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        return _run_delete("delete_nested_device", {
            "track_index": track_index,
            "device_index": device_index,
            "chain_index": chain_index,
            "pad_note": pad_note,
            "nested_device_index": nested_device_index,
            "path": path,
            "expected_name": expected_name,
        }, "device")
    except Exception as e:
        logger.error(f"Error deleting nested device: {str(e)}")
        return f"Error deleting nested device: {str(e)}"

# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()