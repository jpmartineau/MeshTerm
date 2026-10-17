# SPDX-License-Identifier: Apache-2.0
r"""The tours of the videos for Instagram: one or more for each page of MeshTerm.

``scripts/reels.py`` records, paints, and encodes them. Each tour gets a director (refer to
``reels.Director``): it types the keys at the pace of a person, rolls the camera, and
writes the subtitles. A tour starts on its own page. It opens the page off camera, so that
the first video frame already shows the feature (a Reel has one or two seconds to keep a
viewer).

A subtitle is short: two lines of approximately 26 characters at most. ``*text*`` is in
the brand colour, and ``\n`` starts the second line. The subtitles and the captions are
for viewers who do not know MeshTerm. Thus they use plain words: "name tag" instead of
"chip", for example.

The written pages (About MeshTerm, About the author, Join Discord, and Support MeshTerm)
have no video. They are text, and a video of text is not interesting.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import screenshots
import video

if TYPE_CHECKING:
    from reels import Director


@dataclass(frozen=True)
class Reel:
    """One video of the series.

    Attributes:
        title: The name of the page, in the series line above the terminal.
        tour: The keys of the tour, and the subtitles.
        caption: The text of the post on Instagram.
        background: Whether the demo device plays the background traffic of its world. A
            page that shows the traffic live (the dashboard, the live feed) needs it. On the
            other pages, it stays off: then the screen does not change while the tour waits
            off camera, and the cut does not show.
    """

    title: str
    tour: Callable[[Director], None]
    caption: str
    background: bool = False


#: The end of each caption: where the video comes from, the website, and the hashtags.
_FOOTER = (
    "\n\nFilmed in MeshTerm's demo mesh over Montréal (meshterm --demo). MeshTerm is a free,"
    " open-source MeshCore client for Windows, macOS, and Linux.\n"
    "\n"
    "meshterm.net\n"
    "\n"
    "#MeshCore #LoRa #OffGrid #MeshNetwork #Terminal #TUI #OpenSource\n"
)


def _caption(*paragraphs: str) -> str:
    """The caption of a post: its paragraphs, then :data:`_FOOTER`."""
    return "\n\n".join(paragraphs) + _FOOTER


# --------------------------------------------------------------------------- Message


def chat(d: Director) -> None:
    """The chat: the conversations, a live message, the paths, a mention, and a QR code."""
    from meshterm.demo import script

    d.open_page("chat", "pick a conversation")
    d.start("Off-grid chat over *LoRa radio*,\nright in your terminal")
    d.hold(2.8)
    d.type("pub")
    d.hold(0.5)
    d.key("enter", pause=0.0)
    d.wait_for(screenshots.PATHS_MESSAGE[2])
    d.subtitle("Everyone gets\ntheir own *colour*")
    d.hold(2.6)

    offset, _, line = next(item for item in script.LIVE_CHANNEL if item[1] == "Public")
    with d.off_camera():
        d.wait_until(video._start() + offset - 0.8)
    d.wait_for(line.text, timeout=20)
    d.subtitle("New messages arrive *live*,\nover the air")
    d.hold(2.4)

    for _ in range(3):  # Chloé, Félix, then Gabriel
        d.key("up", pause=0.45)
    d.hold(0.5)
    # A key press can be lost while the transcript draws. The camera does not show the
    # tries: it shows the dialog of the right message as it opens.
    with d.off_camera():
        video._open_paths(d)
    d.subtitle("See *every path* a message\ntook to reach you")
    d.hold(2.2)
    d.key("down", pause=1.4)
    d.key("up", pause=1.4)
    d.key("down", pause=0.5)
    d.camera.mark("poster")
    d.hold(1.6)
    d.key("escape")
    d.key("escape")  # the first Esc closes the dialog, the second clears the selection

    d.subtitle("*Mentions* turn into\nname tags as you type")
    d.hold(0.6)
    # The mention is typed in full: the ] makes it a chip.
    d.type("Hey @[Dave-Lachine], are you coming Saturday?")
    d.hold(0.8)
    d.key("enter", pause=0.0)
    sent = time.monotonic()
    d.wait_for("0/130")  # the compose line is empty: the message went
    d.hold(0.8)
    # Dave answers after ``script.REPLY_DELAY_S``. The camera does not show most of the wait.
    with d.off_camera():
        d.wait_until(sent + script.REPLY_DELAY_S - 0.6)
    d.wait_for("be there at 2", timeout=30)
    d.subtitle("When someone *mentions you*,\nyou get a ★")
    d.hold(2.8)

    d.subtitle("Turn any link into a\n*QR code* for your phone")
    d.hold(0.4)
    d.walk_to("11:04")  # our message with the link to the map
    d.hold(0.7)
    d.key(text="u", ctrl=True, pause=0.0)
    d.hold(3.4)


def channels(d: Director) -> None:
    """The channels: their activity, a QR code to share one, and the scope of a channel."""
    d.open_page("channels", "Channels · 4/8 slots")
    d.start("All your channels,\nwith their *activity*")
    d.camera.mark("poster")
    d.hold(3.0)
    d.key("enter", pause=0.0)  # Public
    d.wait_for("Channel — Public")
    d.subtitle("Share a channel\nwith a *QR code*")
    d.hold(1.4)
    d.key("enter", pause=0.0)  # Show QR code
    d.wait_for("meshcore://channel/add")
    d.hold(3.4)
    d.key("escape")
    d.key("escape")
    d.wait_for("Channels · 4/8 slots")
    d.subtitle("Keep a channel's messages\nin *one region*")
    d.key("down", pause=0.6)  # #montreal, which has the scope yul
    d.key("enter", pause=0.0)
    d.wait_for("Channel — #montreal")
    d.hold(0.8)
    d.walk_to("Send scope", "down", pause=0.3)
    d.hold(0.5)
    d.key("enter", pause=0.0)
    d.capture.settle(quiet=0.8)
    d.hold(3.2)


def rooms(d: Director) -> None:
    """The rooms: the list, the board of a room, and a post that the room confirms."""
    d.open_page("rooms", "Rooms · 1 joined")
    d.start("Join a room server's\n*message board*")
    d.hold(2.4)
    d.key("enter", pause=0.0)
    d.wait_for("Room — MTL BBS")
    d.hold(1.2)
    d.key("enter", pause=0.0)  # Open the board
    d.wait_for("MTL BBS · member")
    d.subtitle("Read the posts, also\nthe ones you *missed*")
    d.camera.mark("poster")
    d.hold(3.0)
    d.subtitle("Then *post* to it")
    d.type("I can bring two spare antennas on Saturday")
    d.hold(0.6)
    before = d.count("✓")
    d.key("enter", pause=0.0)
    d.wait_until_shows(lambda: d.count("✓") > before, timeout=20)
    d.subtitle("The ✓ says the\nroom *got* your post")
    d.hold(3.0)


def courier(d: Director) -> None:
    """The courier: a message that waits until its contact is heard."""
    d.open_page("courier", "store-and-forward outbox")
    d.start("Write now.\n*Deliver later.*")
    d.hold(1.6)
    d.key("enter", pause=0.0)  # Queue a message…
    d.wait_for("Courier — recipient")
    d.hold(0.8)
    d.type("sylvie")
    d.hold(0.5)
    d.key("enter", pause=0.0)
    d.wait_for("Message for Sylvie-Gaspésie")
    d.type("Welcome to the mesh! Ping me when you are on the air")
    d.hold(0.6)
    d.key("enter", pause=0.0)
    d.wait_for("When should Sylvie-Gaspésie get it?")
    d.subtitle("It goes when they are\n*next heard*, or at a time")
    d.camera.mark("poster")
    d.hold(3.0)
    d.key("enter", pause=0.0)  # "When it's next heard"
    d.capture.settle(quiet=0.8)
    d.subtitle("Until then, it waits\nin the *outbox*")
    d.hold(3.2)


def contacts(d: Director) -> None:
    """The contacts: a list to sort and filter, then the page of one node."""
    d.open_page("contacts", "Contacts · 40 known")
    d.start("Every node you know,\nsorted *your way*")
    d.hold(2.0)
    d.key("right", ctrl=True, pause=1.3)
    d.key("right", ctrl=True, pause=1.3)
    d.type("mont-r")
    d.hold(1.0)
    d.key("enter", pause=0.0)
    d.wait_for("Node — Mont-Royal")
    d.subtitle("Each node has a page,\nwith its *map*")
    d.hold(3.2)
    d.key("tab", pause=0.0)
    d.wait_for("node → you, as heard")
    d.subtitle("and every *route*\nbetween you and it")
    d.camera.mark("poster")
    d.hold(3.4)


# --------------------------------------------------------------------------- Watch


def dashboard(d: Director) -> None:
    """The dashboard: the activity, the traffic, and the health of the radio."""
    d.open_page("dashboard", "Dashboard — mesh overview", settle=1.5)
    d.start("Your whole mesh,\n*live*, on one screen")
    d.camera.mark("poster")
    d.hold(3.4)
    d.subtitle("Every packet *counted*,\nby kind")
    d.hold(3.0)
    d.subtitle("and the *health*\nof your radio")
    d.hold(3.0)
    d.subtitle("Narrow it to\n*one region*")
    d.type("s")
    d.wait_for("· unscoped")
    d.hold(1.6)
    d.type("s")
    d.hold(2.4)


def livefeed(d: Director) -> None:
    """The live feed: packets that arrive, and one packet opened."""
    from meshterm.demo import script

    d.open_page("live f", "Live feed")
    offset, _, _ = next(item for item in script.LIVE_CHANNEL if item[1] == "Public")
    with d.off_camera():
        d.wait_until(video._start() + offset - 1.5)
    d.start("Every packet,\nthe moment it *arrives*")
    d.hold(4.5)
    d.subtitle("Open one to see\n*where it came from*")
    d.key("down", pause=0.6)
    d.key("enter", pause=0.0)
    d.wait_for("class     ")
    d.camera.mark("poster")
    d.hold(3.6)


def watchtower(d: Director) -> None:
    """The watchtower: the alerts, and a node to watch."""
    d.open_page("watchtower", "Watchtower — alerts")
    d.start("Know when a node\n*goes quiet*")
    d.camera.mark("poster")
    d.hold(2.8)
    d.subtitle("or when a *new node*\nshows up")
    d.hold(2.6)
    d.subtitle("Pick the nodes\nyou want to *watch*")
    d.key("end", pause=0.4)
    d.walk_to("Watch a node", "up", pause=0.3)
    d.key("enter", pause=0.0)
    d.wait_for("Silence and SNR rules will watch it")
    d.hold(1.0)
    d.type("ros")
    d.hold(0.6)
    d.key("enter", pause=0.0)
    d.wait_for("Rosemont   silence")
    d.hold(3.0)


def timemachine(d: Director) -> None:
    """The time machine: the history of the whole mesh, then of one node."""
    d.open_page("time m", "Time machine")
    d.start("Everything your radio\nhas ever *heard*")
    d.hold(1.8)
    d.key("enter", pause=0.0)  # The whole mesh
    d.wait_for("the whole mesh ·")
    d.subtitle("Day by day,\nand *hour by hour*")
    d.camera.mark("poster")
    d.hold(3.4)
    d.subtitle("New nodes, and\nthe *busiest* day")
    d.key("pagedown", pause=0.0)
    d.hold(2.8)
    d.key("escape")
    d.wait_for("── Nodes ──")
    d.type("villeray")
    d.hold(0.6)
    d.key("enter", pause=0.0)
    d.wait_for("Villeray ·")
    d.subtitle("and the story\nof *each node*")
    d.hold(3.4)


# --------------------------------------------------------------------------- Explore


def map_(d: Director) -> None:
    """The map: the nodes over the city, a search, and the streets around our node."""
    capture = d.capture
    d.open_page("map", "Map ·", settle=3.0, timeout=120)
    capture.settle(quiet=3.0, timeout=120)
    d.start("Every node on a\nreal *street map*")
    d.camera.mark("poster")
    d.hold(3.0)
    d.subtitle("Type a name\nto *find* a node")
    d.type("lach")
    capture.settle(quiet=0.8, timeout=30)
    d.hold(2.2)
    d.key("escape", pause=0.4)
    d.subtitle("Zoom in, down\nto the *streets*")
    d.key(text="l", ctrl=True, pause=0.0)  # our node
    capture.settle(quiet=0.8, timeout=30)
    for _ in range(5):
        d.key("pageup", pause=0.0)
        capture.settle(quiet=0.6, timeout=30)
        d.hold(0.7)
    d.hold(2.4)


def walk(d: Director) -> None:
    """The mesh walk: the links of our node, then of the repeaters that it hears."""
    d.open_page("mesh w", "Mesh walk")
    d.start("*Walk* the mesh,\none link at a time")
    d.camera.mark("poster")
    d.hold(3.0)
    d.key("enter", pause=0.0)  # the strongest link: Villeray
    d.wait_for("1 hop out")
    d.subtitle("See who each repeater\nhears, and *how well*")
    d.hold(3.0)
    d.key("enter", pause=0.0)
    d.capture.settle(quiet=0.8)
    d.hold(2.6)
    d.subtitle("and walk *home*\nin one key")
    d.key(text="l", ctrl=True, pause=0.0)
    d.wait_for("this device")
    d.hold(2.4)


def trace(d: Director) -> None:
    """A trace to Brossard, with the signal at each hop and the new records."""
    d.open_page("trace t", "pick a target")
    d.start("Trace a route to\nany *repeater*")
    d.hold(1.4)
    d.type("brossard")
    d.hold(0.4)
    d.key("enter", pause=0.0)
    d.wait_for("Trace — Brossard")
    d.hold(1.6)
    d.key("enter", pause=0.0)
    d.wait_for("Per-hop me", timeout=30)  # a records dialog can cover the end
    d.capture.settle(quiet=0.5)
    # The history stores walks of one byte for each hop. Thus this walk of two bytes is new,
    # and its records go to the trophy case.
    if d.shows("new record"):
        d.subtitle("New *records* go to\nyour trophy case")
        d.hold(3.0)
        d.key("enter", pause=0.0)  # Close
    d.subtitle("See the *signal*\nat every hop")
    d.hold(1.2)
    d.key("pagedown", pause=0.0)
    d.camera.mark("poster")
    d.hold(3.4)


def tracepath(d: Director) -> None:
    """Trace path: a route composed hop by hop, then traced."""
    d.open_page("trace p", "Trace path")
    d.start("Build a route\n*hop by hop*")
    d.hold(1.8)
    d.walk_to("Compose path", "up", pause=0.3)
    d.key("enter", pause=0.0)
    d.wait_for("Next hop from")
    d.hold(1.2)
    for _ in range(4):  # remove the route of the last walk
        d.key("backspace", pause=0.3)
    d.subtitle("MeshTerm suggests the\n*strongest* next hop")
    d.hold(1.0)
    for name, label in (("vill", "Villeray"), ("mont-r", "Mont-Royal"), ("rose", "Rosemont")):
        d.type(name)
        d.hold(0.4)
        d.key("enter", pause=0.0)
        d.wait_for(f"Next hop from {label}")
        d.hold(0.9)
    d.camera.mark("poster")
    d.walk_to("Use this path", "down", pause=0.2)
    d.key("enter", pause=0.0)
    d.wait_for("one transmission")
    d.subtitle("Then *trace* it")
    d.hold(0.8)
    d.walk_to("one transmission", "down", pause=0.3)
    d.key("enter", pause=0.0)
    d.capture.settle(quiet=1.5, timeout=30)
    if d.shows("new record"):
        d.subtitle("and set new *records*")
        d.hold(2.6)
        d.key("enter", pause=0.0)  # Close
        d.capture.settle(quiet=0.5)
    d.hold(3.0)


def records(d: Director) -> None:
    """The trophy case: the record lists, then one record, its route, and its area."""
    d.open_page("trophy", "Trophy case")
    d.start("Every trace is *scored*")
    d.hold(2.2)
    d.subtitle("Your best walks,\nin the *trophy case*")
    d.hold(2.2)
    d.key("enter", pause=0.0)
    d.wait_for("1st place")
    d.hold(2.4)
    d.key("tab", pause=0.0)
    d.capture.settle(quiet=0.8)
    d.subtitle("See the *route*\nit walked")
    d.camera.mark("poster")
    d.hold(3.0)
    d.key("tab", pause=0.0)
    d.capture.settle(quiet=0.8, timeout=60)
    d.subtitle("and the *area*\nit covered")
    d.hold(3.2)


# --------------------------------------------------------------------------- This node


def info(d: Director) -> None:
    """Device info: the status of the radio and its settings."""
    d.open_page("device i", "Device info")
    d.start("Your radio's *status*\nat a glance")
    d.camera.mark("poster")
    d.hold(3.4)
    d.subtitle("and every setting,\n*explained*")
    d.key("pagedown", pause=0.0)
    d.hold(2.6)
    d.key("pagedown", pause=0.0)
    d.hold(2.6)


def config(d: Director) -> None:
    """Device config: the settings, a location set on the map, and the staged changes."""
    capture = d.capture
    d.open_page("device c", "Device config — Alex Base")
    d.start("Change your radio's\n*settings*")
    d.hold(2.2)
    d.key("down", pause=0.6)  # Pick location on map…
    with d.off_camera():
        d.key("enter", pause=0.0)
        d.wait_for("Set location")
        capture.settle(quiet=2.5, timeout=120)
    d.subtitle("Set your location\n*on a map*")
    d.camera.mark("poster")
    d.hold(1.6)
    for _ in range(4):
        d.key("left", pause=0.0)
        capture.settle(quiet=0.4, timeout=30)
        d.hold(0.3)
    d.hold(1.0)
    d.key("enter", pause=0.0)
    d.wait_for("Device config — Alex Base")
    d.hold(1.0)
    d.subtitle("Nothing changes until\nyou *apply* it")
    d.key("end", pause=0.0)
    d.wait_for("staged change")
    d.hold(3.2)


def advert(d: Director) -> None:
    """Send advert: announce our node, or share it as a QR code."""
    d.open_page("send adv", "Announce this node to the mesh:")
    d.start("Announce your node\nto the *mesh*")
    d.hold(2.6)
    d.subtitle("or share it as a\n*QR code*")
    d.key("down", pause=0.5)
    d.key("down", pause=0.6)
    d.key("enter", pause=0.0)
    d.wait_for("meshcore://contact/add")
    d.camera.mark("poster")
    d.hold(4.0)


# --------------------------------------------------------------------------- Other nodes


def repeater_admin(d: Director) -> None:
    """Repeater admin: the settings of a repeater, read over the air, then its CLI."""
    from meshterm.core import preferences

    capture = d.capture
    d.open_page("repeater a", "node to manage")
    with d.off_camera():
        d.key("enter", pause=0.0)  # Stade-Olympique, a remembered admin
        d.wait_for("Repeater admin — Stade-Olympique")
        capture.settle(quiet=0.8)
    d.start("Run a repeater\n*over the air*")
    d.hold(2.2)
    d.key("end", pause=0.5)
    d.walk_to("Read settings", "up")
    d.hold(0.5)
    d.key("enter", pause=0.0)
    d.wait_for("Reading — Stade-Olympique")
    d.subtitle("Read all its\n*settings*")
    d.hold(2.2)
    # The read is one round trip for each setting, with the transmit cooldown between two
    # reads: more than three minutes in all. Off camera, a short cooldown makes the run
    # shorter, and the video does not show it.
    with d.off_camera():
        cooldown = preferences.current().trace_cooldown_s
        preferences.current().set("trace_cooldown_s", 0.2)
        try:
            d.gone("Reading — Stade-Olympique")
        finally:
            preferences.current().set("trace_cooldown_s", cooldown)
        capture.settle(quiet=0.5)
        if d.shows("No reply from"):
            d.key("enter")  # the list of the settings that did not answer
            capture.settle(quiet=0.5)
        d.key("home", pause=0.0)
        capture.settle(quiet=0.5)
    d.camera.mark("poster")
    d.hold(2.6)
    d.subtitle("and talk to its\n*command line*")
    d.key("end", pause=0.5)
    d.walk_to("Command line", "up")
    d.hold(0.4)
    d.key("enter", pause=0.0)
    capture.settle(quiet=0.8)
    d.hold(0.6)
    d.type("neighbors")
    d.hold(0.4)
    d.key("enter", pause=0.0)
    capture.settle(quiet=1.2, timeout=30)
    d.hold(3.4)


def tx_optimize(d: Director) -> None:
    """TX optimize: a login, a sweep of the TX power of Rosemont, and the best power set."""
    from meshterm.core import preferences
    from meshterm.demo import world

    capture = d.capture
    d.open_page("tx opt", "node to tune")
    d.start("Tune a repeater's\n*TX power*")
    d.hold(1.4)
    d.type("rose")
    d.hold(0.4)
    d.key("enter", pause=0.0)
    d.wait_for("measure at")
    d.hold(0.8)
    d.type("mont-r")
    d.hold(0.4)
    d.key("enter", pause=0.0)
    d.wait_for("TX optimize — Rosemont → Mont-Royal")
    d.hold(1.6)
    d.key("enter", pause=0.0)  # Sweep
    d.wait_for("Admin password for Rosemont")
    d.subtitle("Log in to it\n*over the air*")
    d.hold(0.8)
    d.type(world.ADMIN_PASSWORD)
    d.hold(0.4)
    d.key("enter", pause=0.0)
    d.subtitle("MeshTerm tries each power,\nand *measures* it")
    d.hold(3.0)
    # The sweep is up to 30 traces, with the transmit cooldown between two of them. Off
    # camera, a short cooldown makes the run shorter, and the video does not show it.
    with d.off_camera():
        cooldown = preferences.current().trace_cooldown_s
        preferences.current().set("trace_cooldown_s", 0.2)
        try:
            d.wait_for("Apply winner", timeout=300)
        finally:
            preferences.current().set("trace_cooldown_s", cooldown)
        capture.settle(quiet=0.5)
    d.subtitle("then finds the *best* one")
    d.camera.mark("poster")
    d.hold(3.0)
    d.key("enter", pause=0.0)  # Apply
    d.wait_for("set on Rosemont")
    d.subtitle("and sets it\nwith *one key*")
    d.hold(3.0)


# --------------------------------------------------------------------------- This app


def preferences(d: Director) -> None:
    """Preferences: how MeshTerm behaves, and a staged change."""
    d.open_page("prefer", "Preferences")
    d.start("Make MeshTerm\nwork *your way*")
    d.camera.mark("poster")
    d.hold(2.6)
    d.walk_to("Transmit cooldown", "down", pause=0.3)
    d.hold(0.5)
    d.key("enter", pause=0.0)
    d.wait_for("Allowed:")
    d.hold(0.8)
    for _ in range(4):
        d.key("backspace", pause=0.15)
    d.type("3")
    d.hold(0.6)
    d.key("enter", pause=0.0)
    d.subtitle("Each change waits\nuntil you *apply* it")
    d.hold(1.2)
    d.key("end", pause=0.0)
    d.wait_for("staged change")
    d.hold(3.0)


def diagnostics(d: Director) -> None:
    """Diagnostics: everything a bug report needs."""
    d.open_page("diagn", "MeshTerm diagnostics")
    d.start("Everything a *bug report*\nneeds, on one page")
    d.camera.mark("poster")
    d.hold(3.4)
    # Only a few lines down, to the facts of the radio: the next section shows the folders
    # of the machine that made the video.
    for _ in range(5):
        d.key("down", pause=0.3)
    d.hold(2.6)


REELS: dict[str, Reel] = {
    "chat": Reel(
        "Chat",
        chat,
        _caption(
            "Off-grid chat over LoRa radio, right in your terminal.",
            "Chat on channels, in direct messages, and in rooms. See every path a message"
            " took across the mesh. Mention a friend, and their name turns into a tag. Turn"
            " any link into a QR code for your phone.",
        ),
    ),
    "channels": Reel(
        "Channels",
        channels,
        _caption(
            "All your MeshCore channels, with their activity at a glance.",
            "Share a channel with a QR code, and keep a channel's messages in one region.",
        ),
    ),
    "rooms": Reel(
        "Rooms",
        rooms,
        _caption(
            "The message boards of the mesh, in your terminal.",
            "Join a room server, read every post, also the ones you missed while you were"
            " away, and post to it. A ✓ tells you that the room got your post.",
        ),
    ),
    "courier": Reel(
        "Courier",
        courier,
        _caption(
            "Write now. Deliver later.",
            "The courier keeps a message in its outbox until the contact is heard again, or"
            " until the time you pick. Then it sends it for you.",
        ),
    ),
    "contacts": Reel(
        "Contacts",
        contacts,
        _caption(
            "Every node you know, sorted your way.",
            "Each node has its own page, with a map, its signal, and every route between you"
            " and it.",
        ),
    ),
    "dashboard": Reel(
        "Dashboard",
        dashboard,
        _caption(
            "Your whole mesh, live, on one screen.",
            "Activity, every kind of packet, and the health of your radio. Narrow it to one"
            " region with one key.",
        ),
        background=True,
    ),
    "livefeed": Reel(
        "Live feed",
        livefeed,
        _caption(
            "Every packet your radio hears, the moment it arrives.",
            "Open one to see what it is, where it came from, and the repeaters that carried it.",
        ),
        background=True,
    ),
    "watchtower": Reel(
        "Watchtower",
        watchtower,
        _caption(
            "Know when a node goes quiet.",
            "The watchtower watches the nodes you pick. It tells you when one goes silent,"
            " when it comes back, and when a new node shows up on the mesh.",
        ),
    ),
    "timemachine": Reel(
        "Time machine",
        timemachine,
        _caption(
            "Everything your radio has ever heard.",
            "MeshTerm keeps a record of every packet. Explore your mesh day by day and hour"
            " by hour, and the story of each node.",
        ),
    ),
    "map": Reel(
        "Map",
        map_,
        _caption(
            "Every node of your mesh on a real street map, in your terminal.",
            "Type a name to find a node, and zoom in down to the streets. The map comes from"
            " OpenStreetMap.",
        ),
    ),
    "walk": Reel(
        "Mesh walk",
        walk,
        _caption(
            "Walk the mesh, one link at a time.",
            "Start from your own node, and see who each repeater hears, and how well.",
        ),
    ),
    "trace": Reel(
        "Trace target",
        trace,
        _caption(
            "Trace a route to any repeater.",
            "See the signal at every hop of the way there and back. Each new record goes to"
            " your trophy case.",
        ),
    ),
    "tracepath": Reel(
        "Trace path",
        tracepath,
        _caption(
            "Build a route hop by hop, then trace it.",
            "MeshTerm suggests the strongest next hop from what it has heard.",
        ),
    ),
    "records": Reel(
        "Trophy case",
        records,
        _caption(
            "Every trace is scored.",
            "Your longest, farthest, and widest walks across the mesh, with their route and"
            " the area that they covered.",
        ),
    ),
    "info": Reel(
        "Device info",
        info,
        _caption(
            "Your radio's status at a glance.",
            "Battery, airtime, packets, and every setting, with what it does.",
        ),
    ),
    "config": Reel(
        "Device config",
        config,
        _caption(
            "Change your radio's settings.",
            "Set your location on a map. Nothing changes on the radio until you apply it.",
        ),
    ),
    "advert": Reel(
        "Send advert",
        advert,
        _caption(
            "Announce your node to the mesh.",
            "Or share it as a QR code that a phone can scan.",
        ),
    ),
    "repeater-admin": Reel(
        "Repeater admin",
        repeater_admin,
        _caption(
            "Run a repeater over the air.",
            "Read and change its settings, and talk to its command line, from your terminal.",
        ),
    ),
    "tx-optimize": Reel(
        "TX optimize",
        tx_optimize,
        _caption(
            "Tune a repeater's TX power for the best signal.",
            "MeshTerm logs in to the repeater over the air, tries each power level, measures"
            " how well the target hears it, and sets the best one. Often, that is a lower"
            " power.",
        ),
    ),
    "preferences": Reel(
        "Preferences",
        preferences,
        _caption(
            "Make MeshTerm work your way.",
            "Each preference says what it does, and each change waits until you apply it.",
        ),
    ),
    "diagnostics": Reel(
        "Diagnostics",
        diagnostics,
        _caption(
            "Everything a bug report needs, on one page.",
            "The version, the computer, the terminal, and the radio.",
        ),
    ),
}
