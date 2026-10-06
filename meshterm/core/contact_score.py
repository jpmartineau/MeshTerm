# SPDX-License-Identifier: Apache-2.0
"""Rate contacts by their value for a slot in the contact table of the device.

The contact table of a companion has a limited size, and a mesh fills it with all the
adverts that come. Thus, after a device runs for a long time, the table holds mostly nodes
that were heard one time, in passing, from four hops away. Then there is no space to
discover new nodes. The bulk sweep of the Contacts screen (refer to
:func:`~meshterm.ui.sweep_screen.archive_contacts`) gives the table its free space back.
This module gives the judgement that the sweep uses: one score for each contact. Thus the
sweep can take the weakest contacts and keep the contacts that you do not want to lose.

**The score is additive, never multiplicative.** It is the sum of six terms. Each term is
normalized to ``0..1`` and weighted (:class:`ScoreWeights`). With a product, one zero can
remove all the value of a contact that is strong in all the other terms. For example, a
node that never advertised a position gets a score of zero, however much you talk to it.
A "quality" heuristic must not have exactly this failure mode.

**An unknown gets a neutral score, not zero.** This is the most important rule.
Approximately half of the contacts on a real mesh advertise no location. Many are never
overheard as a relayed packet, so they have no hop count. A channel poster whose name
matches no contact cannot be attributed at all. If "we do not know" gives ``0.0``, all of
these contacts go to the bottom together. Then the sweep archives by the quantity of
metadata that a node broadcasts, instead of by the value of the node.

Thus a term that cannot be calculated returns ``None``. :func:`_fill_unknowns` fills it
with the **population median** of the contacts that have a value for it. The contact then
gets exactly the position of an average peer on that axis, and the axes that are known
make the decision. This rule is also the reason why the two least reliable terms have the
two smallest weights.

**A newcomer gets time to show its value.** Each term above rewards evidence that collects
over time. Thus a node first heard this morning looks the same as a node that was quiet
for a year: both have one advert and no history. For this reason,
:attr:`ScoreWeights.grace` adds a bonus that decays linearly to nothing over
:attr:`ScoreWeights.grace_days`. The bonus is large enough to keep a newcomer out of the
sweep for two weeks. In that time, the newcomer becomes a real neighbour, or it does not.

A contact that MeshTerm never heard (added by hand, or copied from the device before this
history started) has no arrival time from which to decay. Thus it is protected completely,
instead of punished because MeshTerm does not know it (refer to
:data:`PROTECT_UNOBSERVED`).

**Some contacts are never candidates.** A score is a heuristic, and a heuristic must not
overrule an explicit choice. Thus :func:`protection_for` short-circuits five cases before
any arithmetic: a contact that you locked, a watched node, each contact that you sent a
direct message to, a repeater whose admin credentials are stored, and the unobserved
contact above. These contacts still get a score and a rank, because they are real
contacts and the percentile scale is the whole population. But the sweep never takes them.

**The score is never shown.** A raw ``47.3`` means nothing without the distribution that it
came from. Instead, this module gives a screen a rank. The list comes back in order, and
:attr:`~ScoredContact.percentile` puts one contact in the field. The percentile needs no
legend, and it does not become stale when the weights change (refer to
:func:`percentile_rank`). But a screen draws neither of them next to a contact. The archive
preview shows the :class:`ContactSignals` themselves, one lane for each type of
measurement. The reason: the user can compare the evidence with what the user knows. The
user cannot do that with the interpretation of this module.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from statistics import median

from .geo import haversine_km
from .models import Contact

#: Protection reason: the user locked this contact on its page. This is the most explicit
#: claim of all (a lock says only "never archive this one"), so the code checks it first.
PROTECT_LOCKED = "locked"

#: Protection reason: the node is starred in the Watchtower. An explicit pin is more
#: important than each heuristic in this module, because the user already said that this
#: node is important.
PROTECT_WATCHED = "watched"

#: Protection reason: you sent a direct message to this contact. A decision to talk to a
#: person is the strongest statement of intent that the app can observe, and a score must
#: not undo it. (Traffic that is only inbound does not protect a contact, because anyone
#: can send you a message.)
PROTECT_MESSAGED = "messaged"

#: Protection reason: a repeater or a room server whose admin password is stored. If the
#: sweep archives it, the login is lost. That loss is much larger than the gain of one
#: contact slot.
PROTECT_ADMIN = "admin"

#: Protection reason: MeshTerm never heard this contact. Thus each evidence term is empty,
#: for a reason that tells nothing about the node. The contact came from the table of the
#: device itself (or was added by hand) before this history started. A sweep of it archives
#: by how long MeshTerm has run, not by an action of the contact.
PROTECT_UNOBSERVED = "unobserved"

#: The order of the protections when a contact qualifies for more than one. The most
#: deliberate is first, so that the preview explains a row by its strongest claim.
PROTECTION_ORDER = (
    PROTECT_LOCKED,
    PROTECT_WATCHED,
    PROTECT_MESSAGED,
    PROTECT_ADMIN,
    PROTECT_UNOBSERVED,
)

#: The UI text of each protection, keyed by the constants above.
PROTECTION_LABELS = {
    PROTECT_LOCKED: "locked",
    PROTECT_WATCHED: "watched",
    PROTECT_MESSAGED: "you messaged",
    PROTECT_ADMIN: "admin login",
    PROTECT_UNOBSERVED: "never heard here",
}


@dataclass(frozen=True)
class ScoreWeights:
    """The weights and half-lives from which the score is built.

    The sum of the six term weights is 100. Thus a score is "out of 100" before the grace
    bonus is added, but nothing shows it (refer to the module docstring: the UI shows the
    percentile). Their relative sizes encode the ranking that the user asked for: direct
    correspondence much above all the others, then recency, then raw volume, then the
    three weaker signals. Of these three, the two least reliable (hops, distance) have the
    smallest weights.

    Attributes:
        dm: The weight of the direct-message correspondence. This is the heaviest term. A
            conversation is the only signal here that needed a decision by a person at
            both ends.
        recency: The weight of how recently the contact was heard.
        volume: The weight of how many times the contact was heard.
        channel: The weight of the posts on the channels that are configured on the
            device.
        hops: The weight of topological closeness (fewer relays are better).
        distance: The weight of geographic closeness. This is the least reliable term,
            because approximately half of a real mesh advertises no position at all.
        grace: The bonus points that a new contact starts with. They decay linearly to
            zero over :attr:`grace_days`. The bonus is large enough to put the contact
            above most of the field, because a newcomer has no evidence yet, and that is
            the purpose of the bonus.
        grace_days: The time that the newcomer bonus takes to go to zero.
        recency_half_life_days: The half-life of the recency term. Two weeks: long enough
            for a node that sends one advert each week, and short enough that a month of
            silence has an effect.
        dm_half_life_days: The half-life of the conversation recency. On purpose, it is
            much longer than :attr:`recency_half_life_days`. A person that you exchanged
            messages with in the spring is still a person that you correspond with. But a
            node that was last overheard in the spring is gone.
        volume_saturation: The packet count at which the volume term reaches ``1.0``.
        dm_saturation: The message count at which the conversation-volume half reaches
            ``1.0``.
        channel_saturation: The post count at which the channel term reaches ``1.0``.
        hops_midpoint: The hop count at which the hops term falls to ``0.5``.
    """

    dm: float = 30.0
    recency: float = 25.0
    volume: float = 15.0
    channel: float = 12.0
    hops: float = 10.0
    distance: float = 8.0

    grace: float = 25.0
    grace_days: float = 14.0

    recency_half_life_days: float = 14.0
    dm_half_life_days: float = 90.0
    volume_saturation: float = 100.0
    dm_saturation: float = 50.0
    channel_saturation: float = 30.0
    hops_midpoint: float = 2.0


#: The default weights. All the code uses them, unless a caller gives its own.
DEFAULT_WEIGHTS = ScoreWeights()


@dataclass(frozen=True)
class ContactSignals:
    """All the data that the score reads about one contact, already collected from the history.

    :meth:`~meshterm.persistence.repository.Repository.contact_signals` assembles it in a
    small number of grouped queries, not in one query for each contact. Thus the score of
    a table of several hundred contacts costs a constant number of scans. Each optional
    field means unknown. Before the scoring, the population median fills each such field.
    MeshTerm never uses zero for it.

    Attributes:
        node: The canonical id of the contact, 12 hex digits (the key of a node in the
            observations).
        heard_age_days: The age in days of the last time that the contact was heard, or
            ``None`` if it was never heard.
        packets: How many transmissions MeshTerm heard from this node. It counts the
            reception history of the app itself. Thus a new install correctly reads zero
            for all contacts. For this reason, the sweep shows the full ranked preview
            before it acts.
        dm_total: The direct messages exchanged with this contact, in the two directions.
        dm_outbound: How many of those messages we sent. A value that is not zero
            protects the contact completely.
        dm_age_days: The age in days of the most recent direct message in either
            direction, or ``None``.
        channel_posts: The messages that this contact posted on a configured channel. The
            attribution uses the ``Name: `` prefix of a channel message (the wire has no
            sender key).
        channel_attributed: Whether that attribution was possible at all. ``False`` when
            another contact has the same name, or when the name never appeared. Then the
            term is unknown, instead of "posted nothing".
        hops: The median relay count of the packets heard from this node as their
            originator, or ``None`` when no packet from it was ever overheard as a relayed
            packet.
        distance_km: The great-circle distance from our node, or ``None`` when one of the
            two ends advertises no position.
        known_days: The age in days of the first time that MeshTerm heard this node, or
            ``None`` if it was never heard. :data:`PROTECT_UNOBSERVED` uses this ``None``.
        watched: Whether the node is starred in the Watchtower.
        has_admin: Whether admin credentials are stored for it.
        locked: Whether the user locked the contact, so that the sweep cannot archive it
            (refer to :meth:`~meshterm.core.contact_store.ContactStore.set_locked`).
    """

    node: str
    heard_age_days: float | None = None
    packets: int = 0
    dm_total: int = 0
    dm_outbound: int = 0
    dm_age_days: float | None = None
    channel_posts: int = 0
    channel_attributed: bool = False
    hops: float | None = None
    distance_km: float | None = None
    known_days: float | None = None
    watched: bool = False
    has_admin: bool = False
    locked: bool = False


@dataclass(frozen=True)
class ScoredContact:
    """The position of one contact among the others: its score, its rank, and their evidence.

    Attributes:
        contact: The contact that this record describes.
        signals: The evidence from which the score was calculated.
        score: The weighted sum, plus the newcomer grace, if any. It is never shown. Refer
            to :attr:`percentile`, and to the module docstring for the reason.
        percentile: The percentile rank in the scored population, ``0`` to ``100``. This is
            the only form of the score that MeshTerm shows. It calibrates itself (a mesh of
            30 contacts and a mesh of 300 read the same way), it needs no legend, and it
            keeps its meaning if the weights change.
        protection: The reason why the sweep can never take this contact, or ``None`` if
            it can. One of the ``PROTECT_*`` constants.

    A screen shows the **signals**, not the score or its terms. The archive preview draws
    one lane for each type of measurement directly from :attr:`signals` (refer to
    :mod:`~meshterm.ui.sweep_screen`). Thus a user who examines a sweep reads the evidence
    in the same units as it was collected, not in a phrase that this module chose.
    """

    contact: Contact
    signals: ContactSignals
    score: float
    percentile: int
    protection: str | None = None

    @property
    def protected(self) -> bool:
        """Whether this contact is exempt from the sweep, with any score."""
        return self.protection is not None

    @property
    def protection_label(self) -> str:
        """The UI text of the protection, or the empty string when there is no protection."""
        return PROTECTION_LABELS.get(self.protection or "", "")


# -- the individual terms ---------------------------------------------------------------
#
# Each term returns a value in 0..1, or ``None`` for "cannot be computed from this
# contact's evidence". The caller changes ``None`` to the population median, not to zero.


def _decay(age_days: float | None, half_life: float) -> float | None:
    """Exponential decay from ``1.0`` at age zero. The value halves each ``half_life`` days.

    Args:
        age_days: The age of the event, or ``None`` if it never occurred.
        half_life: The number of days for the value to halve.

    Returns:
        The decayed weight, or ``None`` when ``age_days`` is ``None``.
    """
    if age_days is None:
        return None
    return 0.5 ** (max(0.0, age_days) / half_life)


def _saturating(count: float, ceiling: float) -> float:
    """A logarithmic ramp from ``0`` at zero to ``1`` at ``ceiling``, clipped above ``ceiling``.

    The ramp is logarithmic because the important difference is between 1 packet and 10,
    not between 90 and 100. A node heard ten times is certainly not one tenth as
    established as a node heard a hundred times, but a linear ramp says exactly that.

    Args:
        count: The observed count.
        ceiling: The count at which the term reaches ``1.0``.

    Returns:
        The ramped value, in ``0..1``.
    """
    if count <= 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(ceiling))


def term_recency(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """How recently the contact was heard, with a decay that has a two-week half-life.

    A contact that was never heard gets ``0.0``, not unknown. A missing location is a gap
    in what the node chose to broadcast. But "we have never received anything from this
    node" is real evidence about the node. (Some contacts were never heard by MeshTerm,
    because the history is younger than the contact. :data:`PROTECT_UNOBSERVED` catches
    them before this term.)
    """
    if signals.heard_age_days is None:
        return 0.0
    return _decay(signals.heard_age_days, weights.recency_half_life_days)


def term_volume(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """How much traffic MeshTerm heard from this node, on a log ramp that saturates."""
    return _saturating(signals.packets, weights.volume_saturation)


def term_dm(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """Direct correspondence: mostly how much, and partly how recently.

    The split is 60/40 between volume and recency. Thus a long exchange that became quiet
    still keeps most of its value, because a conversation is a lasting relationship, not an
    event that expires. A contact with no messages gets ``0.0``, because a missing
    conversation is evidence, not a gap.
    """
    if signals.dm_total <= 0:
        return 0.0
    volume = _saturating(signals.dm_total, weights.dm_saturation)
    recency = _decay(signals.dm_age_days, weights.dm_half_life_days) or 0.0
    return 0.6 * volume + 0.4 * recency


def term_channel(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """How much this contact posts on the channels that are configured on the device.

    Returns ``None`` when the contact could not be attributed at all. A channel message has
    no sender key, only a ``Name: `` prefix. Thus a contact whose name another contact also
    has (or whose name never appeared as a prefix) is not measured, which is different from
    silent. If this term gives zero for such a contact, each contact whose name collides
    with another name silently goes down. But that collision is a property of the name, not
    of the node.
    """
    if not signals.channel_attributed:
        return None
    return _saturating(signals.channel_posts, weights.channel_saturation)


def term_hops(signals: ContactSignals, weights: ScoreWeights) -> float | None:
    """Topological closeness: a hyperbolic falloff, ``1.0`` direct and ``0.5`` at the midpoint.

    The falloff is hyperbolic instead of exponential, because the difference between four
    hops and five is almost not important. But the difference between zero and one is very
    important.
    """
    if signals.hops is None:
        return None
    return 1.0 / (1.0 + max(0.0, signals.hops) / weights.hops_midpoint)


def term_distance(signals: ContactSignals, midpoint_km: float | None) -> float | None:
    """Geographic closeness, scaled against the median distance of this mesh itself.

    The midpoint is the median known distance of the population, not a constant. Thus the
    term has the same meaning on a dense downtown mesh and on a sparse rural mesh. 50 km is
    far on the first and usual on the second, so a hardcoded threshold is wrong on one of
    them at least. The term is unknown when one of the two ends advertises no position. It
    is also unknown when too few contacts advertise a position for a median to have a
    meaning.
    """
    if signals.distance_km is None or not midpoint_km:
        return None
    return 1.0 / (1.0 + max(0.0, signals.distance_km) / midpoint_km)


def term_grace(signals: ContactSignals, weights: ScoreWeights) -> float:
    """The newcomer bonus, in points (not ``0..1``), which decays linearly to zero.

    Returns ``0.0`` for a contact with no arrival time. That case is protected completely
    (:data:`PROTECT_UNOBSERVED`), instead of getting a bonus that it can keep forever.
    """
    if signals.known_days is None:
        return 0.0
    remaining = 1.0 - (max(0.0, signals.known_days) / weights.grace_days)
    return weights.grace * max(0.0, remaining)


# -- protections ------------------------------------------------------------------------


def protection_for(signals: ContactSignals) -> str | None:
    """Why the sweep can never take this contact, or ``None`` if the score decides.

    The function checks in :data:`PROTECTION_ORDER` (the most deliberate claim first). Thus
    a contact that is watched and also messaged is explained by the star that the user set.

    Args:
        signals: The collected evidence of the contact.

    Returns:
        One of the ``PROTECT_*`` constants, or ``None``.
    """
    claims = {
        PROTECT_LOCKED: signals.locked,
        PROTECT_WATCHED: signals.watched,
        PROTECT_MESSAGED: signals.dm_outbound > 0,
        PROTECT_ADMIN: signals.has_admin,
        PROTECT_UNOBSERVED: signals.known_days is None,
    }
    for reason in PROTECTION_ORDER:
        if claims[reason]:
            return reason
    return None


# -- the population pass ----------------------------------------------------------------


#: How many contacts must advertise a position before their median is trusted as the
#: midpoint of the distance term. Below this number, the term is removed for all contacts
#: (if not, one or two nodes calibrate it). The median fill then handles it the same as any
#: other unknown.
_MIN_LOCATED = 4

#: The term names, in the order that the weighted sum uses them.
_TERM_NAMES = ("dm", "recency", "volume", "channel", "hops", "distance")


def _fill_unknowns(column: list[float | None]) -> list[float]:
    """Replace each ``None`` in the column of one term with the median of the known values.

    This is the only rule that keeps the sweep honest (refer to the module docstring). A
    contact that could not be measured on an axis gets exactly the position of an average
    peer on that axis. Thus the axes that were measurable make the decision. If no contact
    has a value in a column, all the column becomes ``0.5``. That value is neutral, and
    thus has no effect after the weights apply, because it moves each contact by the same
    amount and changes no order.

    Args:
        column: The value of one term for each contact, with ``None`` for unknown.

    Returns:
        The same column with the gaps filled.
    """
    known = [v for v in column if v is not None]
    fill = median(known) if known else 0.5
    return [fill if v is None else v for v in column]


def _self_distance(
    contact: Contact, self_lat: float | None, self_lon: float | None
) -> float | None:
    """The great-circle km from our node to ``contact``, or ``None`` if an end has no position."""
    if self_lat is None or self_lon is None or not contact.has_location:
        return None
    return haversine_km(self_lat, self_lon, float(contact.lat), float(contact.lon))


def percentile_rank(score: float, population: Sequence[float]) -> int:
    """The percentile rank of ``score`` in ``population``, as an integer ``0`` to ``100``.

    The function uses the standard mid-rank definition: all the values strictly below, plus
    half of all the equal values. Thus, in a field of identical scores, all of them read
    ``50`` instead of ``0`` or ``100``, and the median contact reads approximately ``50``.

    Args:
        score: The value to place.
        population: All the scores in the population, ``score`` included.

    Returns:
        The percentile rank, rounded to an integer.
    """
    if not population:
        return 0
    below = sum(1 for value in population if value < score)
    equal = sum(1 for value in population if value == score)
    return int(round(100.0 * (below + 0.5 * equal) / len(population)))


def rank_contacts(
    contacts: Sequence[Contact],
    signals: dict[str, ContactSignals],
    *,
    self_lat: float | None = None,
    self_lon: float | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> list[ScoredContact]:
    """Score and rank a full contact table, strongest first.

    One pass calculates each term (and leaves the unknowns as ``None``). One pass fills the
    gaps of each term with the population median of that term. Then the function adds the
    weighted sum and the grace. The percentile uses the **whole** population, protected
    contacts included. The reason: they are real contacts, and a rank that silently
    excludes them is not a rank against your contacts at all.

    Args:
        contacts: The contacts to rank (our node is not one of them).
        signals: The collected evidence, keyed by the 12-hex node id. A contact with no
            entry gets its score from an empty :class:`ContactSignals`, which protects it
            as unobserved.
        self_lat: The advertised latitude of our node, if it has one.
        self_lon: The advertised longitude of our node, if it has one.
        weights: The weights for the score.

    Returns:
        One :class:`ScoredContact` for each input contact, highest score first. The name
        breaks ties, so that the order is stable between runs.
    """
    if not contacts:
        return []

    gathered: list[ContactSignals] = []
    for contact in contacts:
        node = node_id(contact)
        found = signals.get(node) or ContactSignals(node=node)
        gathered.append(replace(found, distance_km=_self_distance(contact, self_lat, self_lon)))

    # The distance term calibrates against the spread of this mesh itself. Thus it must
    # have the whole population before any one contact can get a score on it.
    located = [s.distance_km for s in gathered if s.distance_km is not None]
    midpoint = median(located) if len(located) >= _MIN_LOCATED else None

    columns: dict[str, list[float | None]] = {
        "dm": [term_dm(s, weights) for s in gathered],
        "recency": [term_recency(s, weights) for s in gathered],
        "volume": [term_volume(s, weights) for s in gathered],
        "channel": [term_channel(s, weights) for s in gathered],
        "hops": [term_hops(s, weights) for s in gathered],
        "distance": [term_distance(s, midpoint) for s in gathered],
    }
    filled = {name: _fill_unknowns(column) for name, column in columns.items()}

    scores: list[float] = []
    for index, sig in enumerate(gathered):
        terms = {name: filled[name][index] for name in _TERM_NAMES}
        total = (
            terms["dm"] * weights.dm
            + terms["recency"] * weights.recency
            + terms["volume"] * weights.volume
            + terms["channel"] * weights.channel
            + terms["hops"] * weights.hops
            + terms["distance"] * weights.distance
            + term_grace(sig, weights)
        )
        scores.append(total)

    ranked = [
        ScoredContact(
            contact=contact,
            signals=sig,
            score=score,
            percentile=percentile_rank(score, scores),
            protection=protection_for(sig),
        )
        for contact, sig, score in zip(contacts, gathered, scores, strict=True)
    ]
    ranked.sort(key=lambda scored: (-scored.score, scored.contact.name.casefold()))
    return ranked


def sweep_candidates(ranked: Sequence[ScoredContact], keep: int) -> list[ScoredContact]:
    """The contacts that a "keep the strongest ``keep``" sweep removes, weakest first.

    Protected contacts are never candidates. Also (and this is the subtle point), they
    **do not use** one of the kept slots. For example, take a table of 100 contacts, with
    30 protected and ``keep=50``. The sweep works only on the sweepable contacts, and it
    keeps the 50 strongest of them. Thus 50 unprotected contacts stay, plus the 30
    protected. If the protections count against the target, a star on a node silently
    makes the next sweep deeper. That is the opposite of the purpose of a star.
    Thus the rungs of the picker report their own real removal counts, not arithmetic on
    the table size (refer to :func:`~meshterm.ui.sweep_screen.archive_contacts`).

    Args:
        ranked: The full ranking from :func:`rank_contacts`, strongest first.
        keep: How many unprotected contacts to keep.

    Returns:
        The victims, weakest first. Thus the preview reads from the bottom up, and a
        truncated part of it still shows the contacts that go first.
    """
    sweepable = [scored for scored in ranked if not scored.protected]
    if keep >= len(sweepable):
        return []
    victims = sweepable[keep:] if keep > 0 else list(sweepable)
    return list(reversed(victims))


def node_id(contact: Contact) -> str:
    """The 12-hex canonical id that is the key of the history of a contact.

    Refer to ``observations.node``.
    """
    ident = contact.public_key or contact.key_prefix or ""
    return ident.lower().removeprefix("0x")[:12]
