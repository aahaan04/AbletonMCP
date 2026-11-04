"""
Groove pool (get_groove_pool, load_groove, assign_groove, set_groove_amount,
set_groove_params) against fake Live objects.

Run from the repo root:
    uv run pytest -v
"""

import pytest

from tests.test_remote_script_bridge import make_instance, script_module  # noqa: F401  (fixture)


class Groove:
    def __init__(self, name):
        self.name = name
        self.timing_amount, self.random_amount = 100.0, 0.0
        self.velocity_amount, self.quantization_amount = 0.0, 0.0


class Item:
    def __init__(self, name, children=(), uri=None):
        self.name, self.children, self.uri = name, list(children), uri
        self.is_folder = bool(children)


class Browser:
    def __init__(self, song):
        self.song = song
        mpc = Item("MPC", [Item("Swing MPC 3000 8ths 57.agr", uri="u:57"),
                           Item("Swing MPC 3000 16ths 54.agr", uri="u:54")])
        core = Item("Core Library", [Item("Grooves", [Item("Swing", [mpc])])])
        self.packs = Item("Packs", [core])
        self.user_library = Item("User Library", [Item("Grooves", [Item("Mine.agr", uri="u:mine")])])

    def load_item(self, item):
        self.song.groove_pool.grooves.append(Groove(item.name[:-4]))


class Song:
    groove_amount = 1.0

    def __init__(self):
        self.groove_pool = type("Pool", (), {})()
        self.groove_pool.grooves = []
        clip = type("Clip", (), {"name": "Hats", "groove": None})()
        slot = type("Slot", (), {"has_clip": True, "clip": clip})()
        self.tracks = [type("Track", (), {"name": "Drums", "clip_slots": [slot]})()]


@pytest.fixture
def live(script_module):  # noqa: F811
    song = Song()
    browser = Browser(song)
    inst = make_instance(script_module, application=type("App", (), {"browser": browser})())
    inst._song = song
    inst._find_browser_item_by_uri = lambda b, uri: next(
        (i for i in browser.packs.children[0].children[0].children[0].children[0].children
         if i.uri == uri), None)
    return inst


def test_load_by_name_then_reuse(live):
    out = live._load_groove(name="Swing MPC 3000 8ths 57")
    assert (out["groove_index"], out["name"], out["loaded"]) == (0, "Swing MPC 3000 8ths 57", True)
    again = live._load_groove(name="Swing MPC 3000 8ths 57.agr")
    assert again["loaded"] is False and len(live._song.groove_pool.grooves) == 1


def test_load_from_user_library_and_by_uri(live):
    assert live._load_groove(name="Mine")["name"] == "Mine"
    assert live._load_groove(uri="u:54")["groove_index"] == 1


def test_load_refuses_unknown_or_ambiguous(live):
    with pytest.raises(ValueError, match="not found"):
        live._load_groove(name="Nope")
    with pytest.raises(ValueError, match="exactly one of uri or name"):
        live._load_groove(uri="u:57", name="x")


def test_assign_and_remove(live):
    live._load_groove(name="Swing MPC 3000 8ths 57")
    assert live._assign_groove(0, 0, groove_index=0)["groove"] == "Swing MPC 3000 8ths 57"
    assert live._assign_groove(0, 0, remove=True)["groove"] is None
    with pytest.raises(IndexError, match="out of range"):
        live._assign_groove(0, 0, groove_index=3)
    with pytest.raises(ValueError, match="either groove_index or remove"):
        live._assign_groove(0, 0)


def test_pool_amount_and_params(live):
    live._load_groove(name="Mine")
    assert live._set_groove_amount(0.5)["global_amount"] == 0.5
    with pytest.raises(ValueError, match="0..1.3"):
        live._set_groove_amount(2)
    assert live._set_groove_params(0, timing=60.0, velocity=20.0)["timing"] == 60.0
    pool = live._get_groove_pool()
    assert pool["global_amount"] == 0.5 and pool["grooves"][0]["velocity"] == 20.0
