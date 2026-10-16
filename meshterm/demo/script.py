# SPDX-License-Identifier: Apache-2.0
"""What the people of the demo world say: the channels, the direct messages, and the room.

The conversations are in French and in English, as on the real mesh of Montréal. Each line
has a time on a virtual clock (refer to :func:`at`). The virtual clock sets "now" at
:data:`NOW_LOCAL`, a late morning on a weekday. The history writes each line at its age
before the real time of the run. Thus the ages on the screens ("12m ago") are the same at
each run. When the clock of the run is also set to that morning (the screenshots do
this), the times of day are correct too: the plan for lunch is made before lunch.

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
            "Nouveau répéteur à Longueuil, sur le toit de mon immeuble. Qui m'entend?",
        ),
        Line(
            at(6, "20:15"), "Poutine Patrol", "Reçu fort et clair depuis le Vieux-Longueuil, +9 dB"
        ),
        Line(
            at(6, "20:21"),
            "Ana Brossard",
            "Heard it from Brossard too, two hops through Mont-Royal",
        ),
        Line(at(6, "20:24"), "Félix", "Merci! Je le laisse en test cette semaine"),
        Line(at(5, "21:40"), "Kim-NDG", "Anyone else getting duplicates through Oratoire tonight?"),
        Line(
            at(5, "21:46"),
            "Jean-Seb",
            "C'est normal, Oratoire et Mont-Royal s'entendent très bien. Rien de cassé.",
        ),
        Line(at(5, "21:48"), "Kim-NDG", "ok good, I thought my T-Deck was haunted 👻"),
        Line(
            at(4, "07:55"),
            "Sam Verdun",
            "Verdun-Quai est hors ligne, batterie à plat. Je monte sur le toit demain matin",
        ),
        Line(at(3, "08:31"), "Sam Verdun", "Verdun-Quai est de retour, batterie neuve 🔋"),
        Line(at(3, "08:40"), ME, "Merci Sam! Je le reçois via Mont-Royal à +3 dB"),
        Line(
            at(2, "21:05"),
            "Dave-Lachine",
            "Lachine path is solid tonight: Lachine → Mont-Royal → Stade, worst link -1.5 dB",
        ),
        Line(
            at(2, "21:19"),
            "Bixi Bandit",
            "Riding up Berri with a node in my bag, pinging every 2 min. Zero drops so far 🚲",
        ),
        Line(at(1, "12:10"), "Chloé L.", "Les couleurs sont superbes sur le mont Royal 🍁"),
        Line(
            at(1, "12:25"),
            "Jean-Seb",
            "Et le répéteur du chalet entend tout le monde, même à travers les feuilles 😄",
        ),
        Line(at(0, "07:48"), "Noor", "Premier test depuis Saint-Laurent, quelqu'un me reçoit?"),
        Line(at(0, "07:52"), "Marie-Ève", "@[Noor] reçu via Mont-Royal, +6 dB. Bienvenue!"),
        Line(
            at(0, "10:05"),
            "Mathilde",
            "Bonjour la gang! Première fois sur MeshCore, je suis à Saint-Henri 👋",
        ),
        Line(at(0, "10:08"), "Marie-Ève", "Bienvenue @[Mathilde]! Tu passes par quel répéteur?"),
        Line(at(0, "10:12"), "Mathilde", "Mont-Royal je pense, ou Oratoire. Comment je vérifie?"),
        Line(
            at(0, "10:15"),
            ME,
            "Ton advert m'arrive en un hop via Mont-Royal, SNR +8. Trace path te montre tout",
        ),
        Line(
            at(0, "10:40"), "Jean-Seb", "Rappel: rencontre mesh samedi 14h au marché Jean-Talon ☕"
        ),
        Line(
            at(0, "11:01"),
            "Noor",
            "@[Ruelle Verte] t'as encore la carte de couverture? On veut la voir samedi",
        ),
        Line(
            at(0, "11:04"),
            ME,
            "Oui! https://www.openstreetmap.org/#map=11/45.5200/-73.6400 je l'apporte imprimée",
        ),
        Line(at(0, "11:30"), "Poutine Patrol", "J'apporte des poutines pour tout le monde 🍟"),
        Line(
            at(0, "11:39"), "Félix", "Longueuil ↔ Stade à +6 dB depuis la nouvelle antenne, enfin!"
        ),
    ),
    "#montreal": (
        Line(
            at(6, "18:40"),
            "Raphaël",
            "Pont Viau fermé ce soir pour des travaux, passez par Papineau",
        ),
        Line(
            at(4, "16:10"),
            "Olivier.T",
            "Anyone know if the Jean-Talon market is open late on Thursday?",
        ),
        Line(at(4, "16:21"), "Marie-Ève", "Jusqu'à 18h je crois, 20h le vendredi"),
        Line(at(3, "18:05"), "Olivier.T", "Bixi stations are all full downtown, good luck tonight"),
        Line(
            at(3, "20:05"),
            "Jean-Seb",
            "Panne de courant à Ahuntsic. Les nœuds sur batterie tiennent le coup 💪",
        ),
        Line(
            at(3, "20:13"), "Félix", "Même chose à Hochelaga, mais Stade-Olympique n'a rien senti"
        ),
        Line(
            at(1, "09:15"),
            "Chloé L.",
            "Neige annoncée la semaine prochaine. Rentrez vos antennes de balcon ⛄",
        ),
        Line(
            at(0, "07:40"),
            ME,
            "Quelqu'un a vu la grue sur Saint-Denis? Elle bloque mon lien vers Stade 😑",
        ),
        Line(at(0, "07:48"), "Félix", "Haha, la grue a un meilleur SNR que nous autres"),
        Line(
            at(0, "08:10"),
            "Raphaël",
            "La ligne orange roule au ralenti entre Henri-Bourassa et Montmorency",
        ),
        Line(at(0, "11:20"), "Kim-NDG", "Smoked meat run at noon, who's in?"),
        Line(at(0, "11:24"), "Poutine Patrol", "Toujours."),
        Line(at(0, "11:27"), "Sam Verdun", "Moi! On se rejoint au métro?"),
    ),
    "#velo": (
        Line(
            at(5, "10:20"),
            "Bixi Bandit",
            "Tour du canal de Lachine dimanche 9h, départ au marché Atwater",
        ),
        Line(
            at(5, "10:31"), "Dave-Lachine", "Count me in. I'll bring the T-Deck for a coverage test"
        ),
        Line(at(5, "10:52"), "Sam Verdun", "Je serai là avec le vélo cargo 🚲"),
        Line(
            at(3, "13:30"),
            "Bixi Bandit",
            "Résultat du tour: 11 répéteurs entendus entre Atwater et Lachine",
        ),
        Line(
            at(3, "13:42"), "Dave-Lachine", "And the Lachine repeater heard all of us. Great test!"
        ),
        Line(
            at(1, "16:20"),
            "Sam Verdun",
            "La piste du REV Saint-Denis est encore en travaux près de Jean-Talon",
        ),
        Line(
            at(0, "09:30"), "Bixi Bandit", "Quelqu'un pour une montée du mont Royal demain matin?"
        ),
    ),
    "Famille": (
        Line(at(4, "19:02"), "Léa", "Souper chez grand-maman dimanche, 17h"),
        Line(at(4, "19:10"), ME, "Parfait, j'apporte le dessert"),
        Line(at(4, "19:12"), "Léa", "N'oublie pas la tourtière de tante Jo!"),
        Line(at(1, "17:41"), "Léa", "T'es où? Le souper est prêt 😄"),
        Line(at(1, "17:44"), ME, "J'arrive, je suis au coin de la rue"),
        Line(at(0, "08:15"), "Léa", "Il fait 2 degrés ce matin, prends ta tuque!"),
        Line(at(0, "08:18"), ME, "Trop tard, je suis déjà dehors 🥶"),
    ),
}

#: The direct conversations, by the name of the other node, oldest line first.
DIRECT_LINES: dict[str, tuple[Line, ...]] = {
    "Jean-Seb": (
        Line(
            at(7, "09:12"),
            "Jean-Seb",
            "Peux-tu redémarrer Stade-Olympique? Il ne répond plus aux logins",
        ),
        Line(at(7, "09:31"), ME, "C'est fait, il est revenu"),
    ),
    "Bixi Bandit": (
        Line(at(2, "17:02"), "Bixi Bandit", "Le lien vers Mont-St-Hilaire tient la route?"),
        Line(at(2, "17:06"), ME, "Oui, -5 dB depuis Mont-Royal. Fragile, mais ça passe"),
    ),
    "Dave-Lachine": (
        Line(
            at(2, "20:20"),
            ME,
            "Can you trace me from Lachine tonight? Testing the path through Oratoire",
        ),
        Line(at(2, "20:51"), "Dave-Lachine", "Done: 3 hops, worst link -2.5 dB at Oratoire"),
        Line(at(2, "20:55"), ME, "Merci Dave!", acked=False),
    ),
    "Marie-Ève": (
        Line(at(1, "18:20"), "Marie-Ève", "Tu viens à la rencontre samedi?"),
        Line(at(1, "18:31"), ME, "Oui! J'apporte deux nœuds de rechange pour les nouveaux"),
        Line(at(1, "18:40"), "Marie-Ève", "Super. Je réserve une table au café du marché"),
        Line(at(1, "18:44"), ME, "Parfait, à samedi 👍"),
        Line(
            at(0, "10:50"), "Marie-Ève", "Mathilde est nouvelle, tu peux l'aider avec son Heltec?"
        ),
        Line(at(0, "10:53"), ME, "Bien sûr, je lui écris"),
    ),
    "Mathilde": (
        Line(
            at(0, "10:55"),
            ME,
            "Salut! Marie-Ève m'a dit que tu commences. Besoin d'aide pour le Heltec?",
        ),
        Line(at(0, "11:10"), "Mathilde", "Oui merci! Je ne sais pas quelle fréquence choisir"),
        Line(at(0, "11:12"), ME, "910.525 MHz, BW 62.5, SF7. C'est le réglage de Montréal"),
        Line(at(0, "11:20"), "Mathilde", "Ça marche! Je vois trois répéteurs maintenant 🎉"),
    ),
}

#: The board of each room, oldest post first. The room name as the sender is a notice of
#: the room admin. ``5e1d9c07`` is a member that no contact of our node names.
ROOM_LINES: dict[str, tuple[Line, ...]] = {
    "Le Babillard": (
        Line(
            at(2, "19:30"),
            "Jean-Seb",
            "Rencontre mesh samedi 14h au café du marché Jean-Talon. Apportez vos nœuds!",
        ),
        Line(at(2, "19:50"), "Marie-Ève", "J'y serai, avec des antennes à donner"),
        Line(
            at(1, "12:50"),
            "Noor",
            "Perdu: un Heltec V3 dans un boîtier jaune, parc La Fontaine. Récompense: une poutine",
        ),
        Line(at(1, "13:00"), "Poutine Patrol", "J'offre la poutine si tu le retrouves 😄"),
        Line(
            at(1, "22:50"),
            "Le Babillard",
            "Maintenance de Mont-Royal mardi de 22h à 23h. Prévoyez des détours.",
        ),
        Line(at(0, "07:50"), "5e1d9c07", "Quelqu'un a un connecteur SMA à prêter?"),
        Line(at(0, "08:40"), "Félix", "Moi, passe à Hochelaga quand tu veux"),
        Line(at(0, "08:50"), ME, "J'apporte le testeur d'antennes samedi"),
        Line(
            at(0, "10:30"), "Kim-NDG", "Slides from the last meetup are on the board, ask Jean-Seb"
        ),
        Line(at(0, "11:25"), "Jean-Seb", "Il reste six places pour samedi"),
    ),
}

#: The lines that arrive while the demo runs, as ``(seconds after the start, line)``. The
#: first one is a direct message, so the header shows one unread message soon after the
#: start. The age of each line is unused.
LIVE_DIRECT: tuple[tuple[float, Line], ...] = (
    (6.0, Line(0.0, "Bixi Bandit", "T'as vu le record? 34 km jusqu'à Mont-St-Hilaire 🔥")),
)
LIVE_CHANNEL: tuple[tuple[float, str, Line], ...] = (
    (
        45.0,
        "Public",
        Line(0.0, "Chloé L.", "Quelqu'un monte au mont Royal demain pour un test de portée?"),
    ),
    (160.0, "#montreal", Line(0.0, "Kim-NDG", "Le 80 est encore en retard sur du Parc 🙄")),
)

#: The traces that our node ran in the past days, as ``(age in minutes, target, path)``.
#: The target is a repeater name for a target trace, or ``None`` for a composed path. A
#: path names the relays, without our node at the two ends. The history runs each one in
#: the link model, tries again a few times when the trace does not come back (as a person
#: does), and stores each try.
TRACES: tuple[tuple[float, str | None, tuple[str, ...]], ...] = (
    (at(6, "21:30"), "Longueuil", ("Mont-Royal", "Longueuil", "Mont-Royal")),
    (at(5, "20:10"), "Pointe-Claire", ("Mont-Royal", "Pointe-Claire", "Mont-Royal")),
    (
        at(5, "20:20"),
        None,
        ("Mont-Royal", "Pointe-Claire", "Île-Bizard", "Pointe-Claire", "Mont-Royal"),
    ),
    (at(4, "21:00"), "Laval-Chomedey", ("Ahuntsic", "Laval-Chomedey", "Ahuntsic")),
    (at(3, "22:15"), None, ("Mont-Royal", "Mont-St-Hilaire", "Mont-Royal")),
    (at(2, "20:40"), None, ("Oratoire", "Lachine", "Oratoire")),
    (
        at(2, "21:50"),
        None,
        ("Mont-Royal", "Mont-St-Bruno", "Mont-St-Hilaire", "Mont-St-Bruno", "Mont-Royal"),
    ),
    (at(1, "20:30"), None, ("Mont-Royal", "Laval-Chomedey", "Ahuntsic", "Stade-Olympique")),
    (
        at(1, "21:10"),
        None,
        (
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
        ),
    ),
    (
        at(1, "21:40"),
        None,
        (
            "Oratoire",
            "Lachine",
            "Pointe-Claire",
            "Mont-Royal",
            "Brossard",
            "Mont-St-Bruno",
            "Mont-St-Hilaire",
            "Stade-Olympique",
        ),
    ),
    (at(0, "09:05"), "Brossard", ("Mont-Royal", "Brossard", "Mont-Royal")),
    (at(0, "10:20"), None, ("Mont-Royal", "Longueuil", "Stade-Olympique")),
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
