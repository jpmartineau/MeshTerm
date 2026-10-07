# SPDX-License-Identifier: Apache-2.0
"""Tests for the rating of contacts by their worth as a device slot (``core.contact_score``).

The scoring is pure: it has no device, no database, and no screen. Thus these tests examine
the rules on which the safety of the sweep depends:

* An unknown signal is neutral. It does not count against the contact.
* An explicit choice has a higher rank than the arithmetic.
* A newcomer has time to show its worth.
* A caller gets a percentile, not a raw number.
"""

from __future__ import annotations

from meshterm.core.contact_score import (
    PROTECT_ADMIN,
    PROTECT_LOCKED,
    PROTECT_MESSAGED,
    PROTECT_UNOBSERVED,
    PROTECT_WATCHED,
    ContactSignals,
    percentile_rank,
    protection_for,
    rank_contacts,
    sweep_candidates,
    term_channel,
    term_distance,
    term_grace,
    term_hops,
)
from meshterm.core.models import Contact

#: The evidence of a contact. Each test changes one axis of it.
_BASE = dict(heard_age_days=30.0, packets=10, known_days=200.0)


def _contact(name: str, *, lat=None, lon=None) -> Contact:  # noqa: ANN001
    """A contact with a distinct key.

    Thus its hue and its node id differ from the other contacts.
    """
    byte = f"{(sum(map(ord, name)) % 256):02x}"
    return Contact(name=name, public_key=byte * 32, key_prefix=byte * 6, lat=lat, lon=lon)


def _rank(*specs) -> list:  # noqa: ANN001
    """Rank the ``(name, signal-overrides)`` pairs.

    The function returns the scored contacts, strongest first.
    """
    contacts = [_contact(name) for name, _ in specs]
    signals = {}
    for contact, (_, overrides) in zip(contacts, specs, strict=True):
        node = contact.public_key[:12]
        signals[node] = ContactSignals(node=node, **{**_BASE, **overrides})
    return rank_contacts(contacts, signals)


def _by_name(ranked) -> dict:  # noqa: ANN001
    """The ranking, with the contact name as the key. A test can then assert on one row."""
    return {scored.contact.name: scored for scored in ranked}


def test_an_unknown_signal_scores_neutral_not_zero() -> None:
    """A contact that advertises no position gets a middle score on that axis, not the lowest score.

    This is the only rule on which the fairness of the whole sweep depends. Approximately
    half of a real mesh does not advertise a location. Many nodes are never heard as a
    relayed packet, so they have no hop count. A channel poster whose name matches no
    contact cannot be attributed. If any of these gave a score of zero, all the contacts
    that MeshTerm cannot measure would sink together. Then the sweep would archive a contact
    because of the quantity of metadata that its node broadcasts.
    """
    # Two contacts are the same in each measured way. One has no position and no route.
    ranked = _by_name(
        _rank(
            ("Placed", {"hops": 2.0}),
            ("Unplaced", {"hops": None}),
            # Two more contacts with a position. Thus the hop column has a median to fill from.
            ("Near", {"hops": 1.0}),
            ("Far", {"hops": 3.0}),
        )
    )
    # The unmeasured contact is where the median of the measured contacts puts it: between
    # the near contact and the far contact, not below both.
    assert ranked["Near"].score > ranked["Unplaced"].score > ranked["Far"].score
    # It is not at the bottom of the field.
    assert ranked["Unplaced"].percentile > 0


def test_a_column_nobody_can_measure_changes_no_ordering() -> None:
    """If no contact can be scored on an axis, that axis changes the score of each contact equally.

    The fill becomes a flat 0.5. After the weight, this value has no effect: it changes each
    score by the same amount. Thus a signal that the whole mesh does not give can never
    change the order of the field.
    """
    ranked = _by_name(_rank(("A", {"packets": 40}), ("B", {"packets": 3})))
    # No contact has hops, distance, or channel attribution here. The packet counts decide
    # the order.
    assert ranked["A"].score > ranked["B"].score


def test_an_unattributable_channel_name_reads_unknown_not_silent() -> None:
    """A contact whose channel posts cannot be attributed is unmeasured. It is not proven quiet.

    A channel packet has no sender key. It has only a ``Name: `` prefix. Thus a name that two
    contacts share is attributed to neither of them. The score must show "we do not know".
    If it showed "posted nothing", the score of a node would be lower because of its
    *name*.
    """
    unattributed = ContactSignals(node="a" * 12, channel_attributed=False, channel_posts=0)
    silent = ContactSignals(node="b" * 12, channel_attributed=True, channel_posts=0)
    from meshterm.core.contact_score import DEFAULT_WEIGHTS

    assert term_channel(unattributed, DEFAULT_WEIGHTS) is None
    assert term_channel(silent, DEFAULT_WEIGHTS) == 0.0


def test_distance_calibrates_against_this_mesh_rather_than_a_constant() -> None:
    """The distance midpoint is the median of the population, so it has the same meaning everywhere.

    A distance of 50 km is far on a mesh in a city centre. It is not unusual in a valley.
    A threshold in the code would be wrong on one of them.
    """
    near = ContactSignals(node="a" * 12, distance_km=5.0)
    # On a dense mesh (median 5 km), a contact at 5 km is average. On a sparse mesh (median
    # 100 km), the same contact is *close*, and its score is higher.
    assert term_distance(near, 100.0) > term_distance(near, 5.0)
    # There are too few contacts with a position for a median to have a meaning. The term
    # is unknown.
    assert term_distance(near, None) is None


def test_a_newcomer_is_given_a_fortnight_to_earn_its_keep() -> None:
    """The grace bonus keeps a new contact out of the sweep, then becomes smaller until it is zero.

    Each other term rewards evidence that accumulates. Thus a node that MeshTerm first heard
    this morning has the same score, in the arithmetic, as a node that was quiet for a year.
    Both have one advert and no history.
    """
    from meshterm.core.contact_score import DEFAULT_WEIGHTS as w

    assert term_grace(ContactSignals(node="a" * 12, known_days=0.0), w) == w.grace
    # At the middle of the window, the bonus is half.
    assert term_grace(ContactSignals(node="a" * 12, known_days=7.0), w) == w.grace / 2
    # After the window, the bonus is zero and the contact competes on its own worth.
    assert term_grace(ContactSignals(node="a" * 12, known_days=14.0), w) == 0.0
    assert term_grace(ContactSignals(node="a" * 12, known_days=400.0), w) == 0.0

    # End to end: a newcomer that was heard one time has a higher rank than a contact with
    # equally little evidence that MeshTerm has known for months.
    ranked = _by_name(
        _rank(
            ("Newcomer", {"packets": 1, "heard_age_days": 0.1, "known_days": 1.0}),
            ("Stale", {"packets": 1, "heard_age_days": 0.1, "known_days": 300.0}),
        )
    )
    assert ranked["Newcomer"].score > ranked["Stale"].score


def test_protections_outrank_the_arithmetic() -> None:
    """A lock, a star, a sent message, an admin login, or an unheard contact is never a candidate.

    A heuristic must not overrule an explicit choice. It must also not punish a contact
    because MeshTerm does not have the information.
    """
    assert protection_for(ContactSignals(node="a" * 12, **_BASE, locked=True)) == PROTECT_LOCKED
    assert protection_for(ContactSignals(node="a" * 12, **_BASE, watched=True)) == PROTECT_WATCHED
    assert protection_for(ContactSignals(node="a" * 12, **_BASE, dm_outbound=1)) == PROTECT_MESSAGED
    assert protection_for(ContactSignals(node="a" * 12, **_BASE, has_admin=True)) == PROTECT_ADMIN
    # There is no arrival time. The contact is older than this history (or a user added it
    # by hand). Thus each evidence term is empty, and the reason shows nothing about the node.
    assert protection_for(ContactSignals(node="a" * 12)) == PROTECT_UNOBSERVED
    # Normal evidence and no claims: the score decides.
    assert protection_for(ContactSignals(node="a" * 12, **_BASE)) is None


def test_inbound_messages_alone_do_not_protect() -> None:
    """Messages that a contact sent to us do not protect it.

    Each node on the mesh can send us a message.
    """
    inbound_only = ContactSignals(node="a" * 12, **_BASE, dm_total=20, dm_outbound=0)
    assert protection_for(inbound_only) is None


def test_a_protected_contact_still_ranks_but_is_never_swept() -> None:
    """The sweep does not select a protected contact, but the rank still includes it.

    A percentile that removed the protected contacts without a message would not be a rank
    against *your contacts*.
    """
    ranked = _rank(
        ("Strong", {"packets": 90, "heard_age_days": 0.1}),
        ("Starred", {"packets": 1, "heard_age_days": 500.0, "watched": True}),
        ("Weak", {"packets": 1, "heard_age_days": 500.0}),
        ("Weaker", {"packets": 0, "heard_age_days": 900.0}),
    )
    by_name = _by_name(ranked)
    assert by_name["Starred"].protected
    assert by_name["Starred"].percentile is not None  # it has a rank like each other contact
    # If the sweep keeps one unprotected contact, it sweeps the other two. It never sweeps
    # the starred contact, also when the score is low.
    victims = {v.contact.name for v in sweep_candidates(ranked, keep=1)}
    assert "Starred" not in victims
    assert victims == {"Weak", "Weaker"}


def test_a_protection_does_not_consume_a_kept_slot() -> None:
    """``keep`` counts unprotected contacts, so a star on a node never makes the next sweep deeper.

    Suppose the count included the protected contacts. Then a star would cost another
    contact its place without a message. This is the opposite of the purpose of a star.
    """
    ranked = _rank(
        ("Starred", {"watched": True}),
        ("A", {"packets": 50}),
        ("B", {"packets": 20}),
        ("C", {"packets": 1}),
    )
    # Three contacts that the sweep can select, and keep two: the sweep selects exactly one,
    # and the starred contact is not changed.
    victims = sweep_candidates(ranked, keep=2)
    assert [v.contact.name for v in victims] == ["C"]


def test_victims_come_back_weakest_first() -> None:
    """The sweep returns the victims weakest first.

    The user reads the preview from the top. Thus the first rows are the contacts that the
    user is least likely to miss.
    """
    ranked = _rank(
        ("Best", {"packets": 90}),
        ("Middle", {"packets": 20}),
        ("Worst", {"packets": 0, "heard_age_days": 900.0}),
    )
    assert [v.contact.name for v in sweep_candidates(ranked, keep=1)] == ["Worst", "Middle"]


def test_percentile_is_a_mid_rank_so_a_flat_field_reads_fifty() -> None:
    """Equal values share their rank.

    They do not all have 0 or 100. This is the standard definition.
    """
    assert percentile_rank(5.0, [5.0, 5.0, 5.0]) == 50
    assert percentile_rank(9.0, [1.0, 2.0, 9.0]) == 83
    assert percentile_rank(1.0, [1.0, 2.0, 9.0]) == 17
    assert percentile_rank(1.0, []) == 0


def test_direct_correspondence_is_the_heaviest_single_signal() -> None:
    """No other lane changes a score as much. A conversation is a decision at both ends.

    This is the heaviest *single* lane. We chose that it is not heavier than two strong
    lanes together. A node that MeshTerm hears all the time and hears often is worth a slot.
    If one axis could overrule two other axes, the weighting would have a single point of
    failure.

    Note what this comparison is really about. A contact that you *replied* to is protected
    (:data:`~meshterm.core.contact_score.PROTECT_MESSAGED`) and never reaches the arithmetic.
    Thus the DM term works on history with inbound messages only: a contact that sent you a
    message and got no answer. In this case, the term must count for a lot, but not for
    everything.
    """
    quiet = {"packets": 2, "heard_age_days": 60.0}
    ranked = _by_name(
        _rank(
            ("Correspondent", {**quiet, "dm_total": 30, "dm_age_days": 20.0}),
            ("Chatty", {**quiet, "channel_attributed": True, "channel_posts": 100}),
            ("Near", {**quiet, "hops": 0.0}),
            ("Nobody", quiet),
        )
    )
    # Compared with contacts that are the same in each other way, the conversation wins by
    # the largest margin.
    base = ranked["Nobody"].score
    assert ranked["Correspondent"].score - base > ranked["Chatty"].score - base
    assert ranked["Correspondent"].score - base > ranked["Near"].score - base


def test_no_single_signal_overrules_two_strong_ones() -> None:
    """A node that is heard all the time *and* often has a higher rank than one maxed lane.

    This is the reason that the score is a weighted sum with a limit on the heaviest term,
    and not a ranking with a dominant key. The sweep must not select a contact that is
    present on the mesh, because it never sent a direct message.
    """
    ranked = _by_name(
        _rank(
            ("Present", {"packets": 100, "heard_age_days": 0.1, "dm_total": 0}),
            ("Inbox", {"packets": 2, "heard_age_days": 60.0, "dm_total": 30, "dm_age_days": 20.0}),
        )
    )
    assert ranked["Present"].score > ranked["Inbox"].score


def test_a_quiet_conversation_keeps_most_of_its_worth() -> None:
    """Correspondence loses its worth much more slowly than adverts.

    It is a relationship, not an event.
    """
    ranked = _by_name(
        _rank(
            ("Recent", {"dm_total": 10, "dm_age_days": 1.0}),
            ("Lapsed", {"dm_total": 10, "dm_age_days": 120.0}),
            ("Never", {"dm_total": 0}),
        )
    )
    assert ranked["Recent"].score > ranked["Lapsed"].score
    # But the old conversation is still worth much more than no conversation.
    gap_to_recent = ranked["Recent"].score - ranked["Lapsed"].score
    gap_to_never = ranked["Lapsed"].score - ranked["Never"].score
    assert gap_to_never > gap_to_recent


def test_fewer_hops_ranks_higher() -> None:
    """Topological closeness is hyperbolic.

    Zero to one hop is important. Four to five hops is not.
    """
    from meshterm.core.contact_score import DEFAULT_WEIGHTS as w

    direct = term_hops(ContactSignals(node="a" * 12, hops=0.0), w)
    one = term_hops(ContactSignals(node="a" * 12, hops=1.0), w)
    four = term_hops(ContactSignals(node="a" * 12, hops=4.0), w)
    five = term_hops(ContactSignals(node="a" * 12, hops=5.0), w)
    assert direct == 1.0
    assert direct - one > four - five


def test_ranking_an_empty_table_is_empty_not_an_error() -> None:
    """A device with no contacts has nothing to rank. The guard of the sweep checks this."""
    assert rank_contacts([], {}) == []


def test_a_contact_with_no_signals_at_all_is_protected() -> None:
    """A contact that is not in the history is protected.

    The sweep does not select it as worthless.
    """
    contact = _contact("Inherited")
    ranked = rank_contacts([contact], {})
    assert ranked[0].protection == PROTECT_UNOBSERVED


def test_a_stronger_contact_never_scores_below_a_weaker_one_on_every_axis() -> None:
    """Dominance is kept: a contact that is better on each axis has a higher rank."""
    ranked = _by_name(
        _rank(
            (
                "Better",
                {
                    "packets": 50,
                    "heard_age_days": 1.0,
                    "dm_total": 5,
                    "dm_age_days": 1.0,
                    "hops": 1.0,
                },
            ),
            ("Worse", {"packets": 2, "heard_age_days": 300.0, "dm_total": 0, "hops": 4.0}),
        )
    )
    assert ranked["Better"].score > ranked["Worse"].score
    assert ranked["Better"].percentile > ranked["Worse"].percentile


def test_a_lock_explains_a_contact_before_any_other_claim_on_it() -> None:
    """A lock means only "keep this one". Thus the preview gives it as the reason."""
    both = ContactSignals(node="a" * 12, **_BASE, locked=True, watched=True, dm_outbound=3)
    assert protection_for(both) == PROTECT_LOCKED
