# SPDX-License-Identifier: Apache-2.0
"""What the people of the demo world say: the channels, the direct messages, and the room.

The conversations are in English, and they are about ordinary things: the mesh, a
meetup, a bike ride, the weather, a family dinner. Each line has a time on a virtual clock
(refer to :func:`at`). The virtual clock sets "now" at :data:`NOW_LOCAL`, a late morning
on a weekday. The history writes each line at its age before the real time of the run.
Thus the ages on the screens ("12m ago") are the same at each run. When the clock of the
run is also set to that morning (the screenshots do this), the times of day are correct
too: the plan for lunch is made before lunch.

The script only holds text and times. :mod:`meshterm.demo.history` turns each line into
the stored message and into the packets that our node heard when the line went over the
air.
"""

from __future__ import annotations

from dataclasses import dataclass

from .world import SELF

#: The local time of day that "now" stands for on the virtual clock (hours): 11:42.
NOW_LOCAL = 11 + 42 / 60


def at(days_ago: int, clock: str) -> float:
    """The age (minutes before now) of a moment on the virtual clock.

    Args:
        days_ago: 0 for today, 1 for yesterday, and so on.
        clock: The local time of day, as ``"HH:MM"``.

    Returns:
        The minutes from that moment to now. A time later than :data:`NOW_LOCAL` on day
        0 is a bug in the script, because it is in the future.
    """
    hours, minutes = (int(part) for part in clock.split(":"))
    age = days_ago * 1440.0 + NOW_LOCAL * 60.0 - (hours * 60.0 + minutes)
    if age < 0:
        raise ValueError(f"{clock} today is after now on the virtual clock")
    return age


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a conversation.

    Attributes:
        age_min: How many minutes before now the line was sent.
        sender: The name of the node that sent it: a node of the world, :data:`ME` for our
            node, or, in a room, the room itself (a notice of its admin) or an 8-hex key
            prefix (an author that no contact names).
        text: The body.
        acked: For our own direct message or room post: whether the ack came back.
    """

    age_min: float
    sender: str
    text: str
    acked: bool = True


#: The sender name of our own lines.
ME = SELF.name

#: The channel conversations, by channel name, oldest line first.
CHANNEL_LINES: dict[str, tuple[Line, ...]] = {
    "Public": (
        Line(
            at(6, "20:12"),
            "Félix",
            "New repeater up in Longueuil, on the roof of my building. Can anyone hear it?",
        ),
        Line(at(6, "20:15"), "Steve K.", "Loud and clear from Old Longueuil, +9 dB"),
        Line(
            at(6, "20:21"),
            "Ana Brossard",
            "Heard it from Brossard too, two hops through Mont-Royal",
        ),
        Line(at(6, "20:24"), "Félix", "Thanks! I'll leave it in test mode this week"),
        Line(at(5, "21:40"), "Kim-NDG", "Anyone else getting duplicates through Oratoire tonight?"),
        Line(
            at(5, "21:46"),
            "Jean-Seb",
            "That's expected, Oratoire and Mont-Royal hear each other really well. Nothing broke.",
        ),
        Line(at(5, "21:48"), "Kim-NDG", "ok good, I thought my T-Deck was haunted 👻"),
        Line(
            at(4, "07:55"),
            "Sam Verdun",
            "Verdun-Quai is offline, flat battery. I'll go up on the roof tomorrow morning",
        ),
        Line(at(3, "08:31"), "Sam Verdun", "Verdun-Quai is back with a new battery 🔋"),
        Line(at(3, "08:40"), ME, "Thanks Sam! Getting it through Mont-Royal at +3 dB"),
        Line(
            at(2, "21:05"),
            "Dave-Lachine",
            "Lachine path is solid tonight: Lachine → Mont-Royal → Stade, worst link -1.5 dB",
        ),
        Line(
            at(2, "21:19"),
            "Nico R.",
            "Biking up Berri with a node in my bag, pinging every 2 min. Zero drops so far 🚲",
        ),
        Line(
            at(1, "12:10"),
            "Chloé L.",
            "The leaves are peaking on the mountain, great day for a walk",
        ),
        Line(
            at(1, "12:25"),
            "Jean-Seb",
            "And the repeater at the chalet hears everyone, even through the trees 😄",
        ),
        Line(at(0, "07:48"), "Noor", "First test from Côte-des-Neiges, can anyone hear me?"),
        Line(at(0, "07:52"), "Marie-Ève", "@[Noor] got you through Mont-Royal, +6 dB. Welcome!"),
        Line(
            at(0, "10:05"), "Mathilde", "Hi everyone! First time on MeshCore, I'm in Saint-Henri 👋"
        ),
        Line(
            at(0, "10:08"),
            "Marie-Ève",
            "Welcome @[Mathilde]! Which repeater are you going through?",
        ),
        Line(at(0, "10:12"), "Mathilde", "Mont-Royal I think, or Oratoire. How do I check?"),
        Line(
            at(0, "10:15"),
            ME,
            "Your advert reaches me through Mont-Royal, then Rosemont. Trace path shows it all",
        ),
        Line(
            at(0, "10:40"),
            "Jean-Seb",
            "Reminder: mesh meetup Saturday at 2 pm, Jean-Talon market ☕",
        ),
        Line(
            at(0, "11:01"),
            "Noor",
            "@[Alex Base] do you still have the coverage map? We'd love to see it Saturday",
        ),
        Line(
            at(0, "11:04"),
            ME,
            "Yes! https://www.openstreetmap.org/#map=11/45.5200/-73.6400 I'll bring a printout",
        ),
        Line(at(0, "11:30"), "Steve K.", "I'll bring a spare antenna for anyone who needs one"),
        Line(
            at(0, "11:33"),
            "Gabriel",
            "Hello from Beloeil! My messages get to Montréal through Mont-St-Hilaire",
        ),
        Line(at(0, "11:39"), "Félix", "Longueuil ↔ Stade at +6 dB since the new antenna, finally!"),
    ),
    "#montreal": (
        Line(
            at(6, "18:40"),
            "Raphaël",
            "Pont Viau is closed tonight for roadwork, take Papineau instead",
        ),
        Line(
            at(4, "16:10"),
            "Olivier.T",
            "Anyone know if the Jean-Talon market is open late on Thursday?",
        ),
        Line(at(4, "16:21"), "Marie-Ève", "Until 6 I think, and 8 on Fridays"),
        Line(at(3, "18:05"), "Olivier.T", "Bike stations are all full downtown, good luck tonight"),
        Line(
            at(3, "20:05"),
            "Jean-Seb",
            "Power outage in Ahuntsic. The nodes on batteries are holding up fine 💪",
        ),
        Line(at(3, "20:13"), "Félix", "Same in Hochelaga, but Stade-Olympique didn't even notice"),
        Line(
            at(1, "09:15"),
            "Chloé L.",
            "Snow in the forecast next week. Time to bring in the balcony antennas ⛄",
        ),
        Line(
            at(0, "07:40"),
            ME,
            "Anyone seen the crane on Saint-Denis? It's blocking my link to Stade 😑",
        ),
        Line(at(0, "07:48"), "Félix", "Ha, the crane has a better SNR than the rest of us"),
        Line(
            at(0, "08:10"),
            "Raphaël",
            "Orange line is running slow between Henri-Bourassa and Montmorency",
        ),
        Line(
            at(0, "11:20"), "Kim-NDG", "Anyone up for lunch near the Atwater market? Around 12:30"
        ),
        Line(at(0, "11:24"), "Steve K.", "Can't today, working from home"),
        Line(at(0, "11:27"), "Sam Verdun", "I'm in! Meet at the main entrance?"),
    ),
    "#cycling": (
        Line(
            at(5, "10:20"),
            "Nico R.",
            "Lachine Canal ride on Sunday at 9, leaving from the Atwater market",
        ),
        Line(
            at(5, "10:31"), "Dave-Lachine", "Count me in. I'll bring the T-Deck for a coverage test"
        ),
        Line(at(5, "10:52"), "Sam Verdun", "I'll be there with the cargo bike 🚲"),
        Line(
            at(3, "13:30"),
            "Nico R.",
            "Ride results: 11 repeaters heard between Atwater and Lachine",
        ),
        Line(
            at(3, "13:42"), "Dave-Lachine", "And the Lachine repeater heard all of us. Great test!"
        ),
        Line(
            at(1, "16:20"),
            "Sam Verdun",
            "The Saint-Denis bike lane is still under construction near Jean-Talon",
        ),
        Line(at(0, "09:30"), "Nico R.", "Anyone up for a ride up the mountain tomorrow morning?"),
    ),
    "Family": (
        Line(at(4, "19:02"), "Léa", "Dinner at Grandma's on Sunday, 5 pm"),
        Line(at(4, "19:10"), ME, "Perfect, I'll bring dessert"),
        Line(at(4, "19:12"), "Léa", "Don't forget the folding chairs!"),
        Line(at(1, "17:41"), "Léa", "Where are you? Dinner's ready 😄"),
        Line(at(1, "17:44"), ME, "Almost there, just around the corner"),
        Line(at(0, "08:15"), "Léa", "It's 2 degrees this morning, dress warm!"),
        Line(at(0, "08:18"), ME, "Too late, I'm already outside 🥶"),
    ),
}

#: The direct conversations, by the name of the other node, oldest line first.
DIRECT_LINES: dict[str, tuple[Line, ...]] = {
    "Jean-Seb": (
        Line(
            at(7, "09:12"),
            "Jean-Seb",
            "Can you reboot Stade-Olympique? It stopped answering logins",
        ),
        Line(at(7, "09:31"), ME, "Done, it's back"),
    ),
    "Nico R.": (
        Line(at(2, "17:02"), "Nico R.", "Is the link to Mont-St-Hilaire holding up?"),
        Line(at(2, "17:06"), ME, "Yes, -5 dB from Mont-Royal. Fragile, but it works"),
    ),
    "Dave-Lachine": (
        Line(
            at(2, "20:20"),
            ME,
            "Can you trace me from Lachine tonight? Testing the path through Oratoire",
        ),
        Line(at(2, "20:51"), "Dave-Lachine", "Done: 3 hops, worst link -2.5 dB at Oratoire"),
        Line(at(2, "20:55"), ME, "Thanks Dave!", acked=False),
    ),
    "Marie-Ève": (
        Line(at(1, "18:20"), "Marie-Ève", "Are you coming to the meetup on Saturday?"),
        Line(at(1, "18:31"), ME, "Yes! I'm bringing two spare nodes for the newcomers"),
        Line(at(1, "18:40"), "Marie-Ève", "Great. I'll book a table at the café in the market"),
        Line(at(1, "18:44"), ME, "Perfect, see you Saturday 👍"),
        Line(at(0, "10:50"), "Marie-Ève", "Mathilde is new, could you help her set up her Heltec?"),
        Line(at(0, "10:53"), ME, "Of course, I'll message her"),
    ),
    "Mathilde": (
        Line(
            at(0, "10:55"),
            ME,
            "Hi! Marie-Ève said you're just getting started. Need a hand with the Heltec?",
        ),
        Line(at(0, "11:10"), "Mathilde", "Yes please! I'm not sure which frequency to use"),
        Line(at(0, "11:12"), ME, "910.525 MHz, BW 62.5, SF7. That's what the Montréal mesh uses"),
        Line(at(0, "11:20"), "Mathilde", "That worked! I can see three repeaters now 🎉"),
    ),
}

#: The board of each room, oldest post first. The room name as the sender is a notice of
#: the room admin. ``5e1d9c07`` is a member that no contact of our node names.
ROOM_LINES: dict[str, tuple[Line, ...]] = {
    "MTL BBS": (
        Line(
            at(2, "19:30"),
            "Jean-Seb",
            "Mesh meetup Saturday at 2 pm, at the café in the Jean-Talon market. Bring your nodes!",
        ),
        Line(at(2, "19:50"), "Marie-Ève", "I'll be there, with a few antennas to give away"),
        Line(
            at(1, "12:50"),
            "Noor",
            "Lost: a Heltec V3 in a yellow case, somewhere in La Fontaine Park. Reward offered",
        ),
        Line(at(1, "13:00"), "Steve K.", "I'll keep an eye out, I walk through there every day"),
        Line(
            at(1, "22:50"),
            "MTL BBS",
            "Mont-Royal maintenance on Tuesday from 10 to 11 pm. Expect detours.",
        ),
        Line(at(0, "07:50"), "5e1d9c07", "Does anyone have an SMA adapter I could borrow?"),
        Line(at(0, "08:40"), "Félix", "I do, come by Hochelaga anytime"),
        Line(at(0, "08:50"), ME, "I'm bringing the antenna analyzer on Saturday"),
        Line(
            at(0, "10:30"), "Kim-NDG", "Slides from the last meetup are on the board, ask Jean-Seb"
        ),
        Line(at(0, "11:25"), "Jean-Seb", "Six spots left for Saturday"),
    ),
}

#: The lines that arrive while the demo runs, as ``(seconds after the start, line)``. The
#: first one is a direct message, so the header shows one unread message soon after the
#: start. The age of each line is unused.
LIVE_DIRECT: tuple[tuple[float, Line], ...] = (
    (6.0, Line(0.0, "Nico R.", "Did you see the record? 34 km to Mont-St-Hilaire 🔥")),
)
LIVE_CHANNEL: tuple[tuple[float, str, Line], ...] = (
    (
        45.0,
        "Public",
        Line(0.0, "Chloé L.", "Anyone heading up the mountain tomorrow for a range test?"),
    ),
    (160.0, "#montreal", Line(0.0, "Kim-NDG", "The 80 bus is late again on Parc 🙄")),
)

#: The answers of the people of the demo world to a channel message of our node that
#: mentions them, by node name. The node answers on the same channel, a few seconds later
#: (:data:`REPLY_DELAY_S`), and its answer mentions our node. A node that is not in this
#: table does not answer. The demo video asks if Dave comes to the meetup
#: (``scripts/video.py``).
REPLIES: dict[str, str] = {
    "Dave-Lachine": f"@[{ME}] Yes! I'll be there at 2, with the T-Deck",
}

#: The seconds from a message of our node to the answer of a node that it mentions (refer
#: to :data:`REPLIES`).
REPLY_DELAY_S = 4.0

#: The traces that our node ran in the past days, as ``(age in minutes, target, path)``.
#: The target is a repeater name for a target trace, or ``None`` for a composed path. A
#: path names the relays, without our node at the two ends. The history runs each one in
#: the link model, tries again a few times when the trace does not come back (as a person
#: does), and stores each try.
TRACES: tuple[tuple[float, str | None, tuple[str, ...]], ...] = (
    (
        at(6, "21:30"),
        "Longueuil",
        ("Rosemont", "Stade-Olympique", "Longueuil", "Stade-Olympique", "Rosemont"),
    ),
    (
        at(5, "20:10"),
        "Pointe-Claire",
        ("Villeray", "Mont-Royal", "Pointe-Claire", "Mont-Royal", "Villeray"),
    ),
    (
        at(5, "20:20"),
        None,
        (
            "Villeray",
            "Mont-Royal",
            "Pointe-Claire",
            "Île-Bizard",
            "Pointe-Claire",
            "Mont-Royal",
            "Villeray",
        ),
    ),
    (
        at(4, "21:00"),
        "Laval-Chomedey",
        ("Villeray", "Ahuntsic", "Laval-Chomedey", "Ahuntsic", "Villeray"),
    ),
    (at(3, "22:15"), None, ("Rosemont", "Mont-Royal", "Mont-St-Hilaire", "Mont-Royal", "Rosemont")),
    (at(2, "20:40"), None, ("Villeray", "Oratoire", "Lachine", "Oratoire", "Villeray")),
    (
        at(2, "21:50"),
        None,
        (
            "Rosemont",
            "Stade-Olympique",
            "Mont-St-Bruno",
            "Mont-St-Hilaire",
            "Mont-St-Bruno",
            "Stade-Olympique",
            "Rosemont",
        ),
    ),
    (
        at(1, "20:30"),
        None,
        ("Villeray", "Mont-Royal", "Laval-Chomedey", "Ahuntsic", "Stade-Olympique", "Rosemont"),
    ),
    (
        at(1, "21:10"),
        None,
        (
            "Rosemont",
            "Centre-Ville",
            "Verdun-Quai",
            "Brossard",
            "Mont-St-Bruno",
            "Mont-St-Hilaire",
            "Mont-St-Bruno",
            "Longueuil",
            "Stade-Olympique",
            "Ahuntsic",
            "Laval-Chomedey",
            "Mont-Royal",
            "Oratoire",
            "Lachine",
            "Pointe-Claire",
            "Mont-Royal",
            "Villeray",
        ),
    ),
    (
        at(1, "21:40"),
        None,
        (
            "Villeray",
            "Oratoire",
            "Lachine",
            "Pointe-Claire",
            "Mont-Royal",
            "Brossard",
            "Mont-St-Bruno",
            "Mont-St-Hilaire",
            "Stade-Olympique",
            "Rosemont",
        ),
    ),
    (at(0, "09:05"), "Brossard", ("Rosemont", "Mont-Royal", "Brossard", "Mont-Royal", "Rosemont")),
    (at(0, "10:20"), None, ("Rosemont", "Longueuil", "Stade-Olympique", "Villeray")),
)

#: The Watchtower: the nodes that our node watches, and the alerts that it raised and the
#: user did not acknowledge yet, as ``(age in minutes, kind, node name, message)``. The
#: messages are the words of :mod:`meshterm.services.watchtower`.
WATCHED: tuple[str, ...] = ("Mont-Royal", "Mont-St-Hilaire", "Verdun-Quai", "Longueuil")
ALERTS: tuple[tuple[float, str, str, str], ...] = (
    (at(4, "07:40"), "silence", "Verdun-Quai", "nothing heard for 6 h"),
    (
        at(3, "08:31"),
        "recovered",
        "Verdun-Quai",
        "back on the air — heard again after a silence alarm",
    ),
    (at(0, "09:55"), "new-node", "Mathilde", "first appearance — never heard on this mesh before"),
)
