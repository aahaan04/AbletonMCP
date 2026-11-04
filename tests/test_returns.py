"""
Return tracks and loading devices onto returns / the master: Remote Script
handlers against fake Live objects, and the server tools with the connection
mocked.

Run from the repo root:
    uv run pytest -v
"""

import pytest

import MCP_Server.server as server
from tests.test_clip_notes import call, fake_conn  # noqa: F401  (fixture)
from tests.test_clip_notes import _silence_telemetry, _no_consent_prompt  # noqa: F401
from tests.test_clip_notes import _script_capabilities_available  # noqa: F401
from tests.test_racks import Device
from tests.test_remote_script_bridge import make_instance, script_module  # noqa: F401  (fixture)


class Track:
    def __init__(self, name):
        self.name, self.devices = name, []


class Item:
    def __init__(self, name, uri):
        self.name, self.uri = name, uri


class Browser:
    """load_item puts a device on whatever track is selected, as Live does."""

    def __init__(self, song):
        self.song = song

    def load_item(self, item):
        self.song.view.selected_track.devices.append(Device(item.name))


class App:
    def __init__(self, song):
        self.browser = Browser(song)


class Song:
    def __init__(self):
        self.tracks = [Track("Drums"), Track("Bass")]
        self.return_tracks = [Track("A-Reverb")]
        self.master_track = Track("Main")
        self.view = type("View", (), {"selected_track": None})()

    def create_return_track(self):
        self.return_tracks.append(Track("%s-Return" % "ABCDEFGH"[len(self.return_tracks)]))


@pytest.fixture
def live(script_module):  # noqa: F811
    song = Song()
    inst = make_instance(script_module, application=App(song))
    inst._song = song
    items = {"query:AudioFx#Reverb": Item("Reverb", "query:AudioFx#Reverb"),
             "query:AudioFx#Limiter": Item("Limiter", "query:AudioFx#Limiter")}
    inst._find_browser_item_by_uri = lambda browser, uri: items.get(uri)
    return inst


def test_create_return_track_reports_index_and_name(live):
    out = live._create_return_track()
    assert out == {"created": True, "return_track_index": 1,
                   "return_track_count": 2, "name": "B-Return"}


@pytest.mark.parametrize("track_type,index,target", [
    ("track", 1, lambda s: s.tracks[1]),
    ("return", 0, lambda s: s.return_tracks[0]),
    ("master", 99, lambda s: s.master_track),   # index ignored for master
])
def test_load_targets_track_return_or_master(live, track_type, index, target):
    out = live._load_browser_item(index, "query:AudioFx#Reverb", track_type)
    assert target(live._song).devices[-1].name == "Reverb"
    assert out["track_type"] == track_type and out["devices_after"] == ["Reverb"]


def test_load_refuses_bad_return_index_and_unknown_uri(live):
    with pytest.raises(IndexError, match="return index 3 out of range"):
        live._load_browser_item(3, "query:AudioFx#Reverb", "return")
    with pytest.raises(ValueError, match="not found"):
        live._load_browser_item(0, "query:Nope", "track")


# -- server tools -----------------------------------------------------------

def test_load_forwards_track_type(fake_conn):
    conn = fake_conn(response={"loaded": True, "devices_after": ["Limiter"]})
    out = call(server.load_instrument_or_effect, None, 0, "query:AudioFx#Limiter", track_type="master")
    assert conn.sent == [("load_browser_item", {"track_index": 0, "item_uri": "query:AudioFx#Limiter",
                                                "track_type": "master"})]
    assert "Limiter" in out


def test_load_on_return_or_master_gates_on_its_capability(monkeypatch, fake_conn):
    """An older script ignores track_type and would load onto regular track N."""
    import MCP_Server.script_handshake as sh
    seen = []
    monkeypatch.setattr(sh, "require_capability",
                        lambda name, send_command=None: seen.append(name) or "missing")
    conn = fake_conn(response={"loaded": True, "devices_after": []})
    assert call(server.load_instrument_or_effect, None, 0, "u", track_type="return") == "missing"
    call(server.load_instrument_or_effect, None, 0, "u")  # regular track: no gate
    assert seen == ["load_on_return_or_master"]
    assert [c[1]["track_type"] for c in conn.sent] == ["track"]


def test_create_return_track_tool(fake_conn):
    fake_conn(response={"return_track_index": 2, "return_track_count": 3, "name": "C-Return"})
    assert call(server.create_return_track, None) == \
        "Created return track 'C-Return' at index 2 (total returns: 3)"
