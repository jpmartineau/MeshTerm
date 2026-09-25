# SPDX-License-Identifier: Apache-2.0
"""The shared shapes a report is built out of — one constructor per concept.

A node means the same thing in ``contacts``, ``monitor``, ``courier list``, ``trace`` and
``records``, and it should *look* the same in all five and parse the same in all five. The
way to guarantee that is not review, it is construction: every listing that mentions a
node asks this module for the column, so there is exactly one definition of what a node's
plain lanes are called and what its machine object holds.

Each constructor returns one :class:`~meshterm.ui.report.Column`, which carries both
projections of one typed value. The typed value is what a row holds — a :class:`NodeRef`,
a :class:`Position`, a :class:`~datetime.datetime`, a bare float — never a formatted cell,
so the plain face can print ``+5.1`` and the machine face ``5.1`` without either one
parsing the other's output back.

The interesting one is :func:`node`, and it is what earns the design: **one machine key
expands to as many plain columns as the surface has room for.** ``contacts`` spends four
(a name, a role, a hash, a key), ``monitor``'s live stream spends two, a trace's per-hop
table spends one — and all four documents carry the same five-key object, because the
lanes are a rendering choice and the shape is not.

Two absences, kept apart on purpose. ``None`` is *we do not know*, and it reads as
:data:`~meshterm.ui.script.NONE` plain and ``null`` in JSON. A word standing in for an
absence is not the same thing: ``unknown`` in a ``TYPE`` column is a word a reader
understands, so the plain face keeps it, while the document writes ``null`` — because a
consumer already has one spelling for "there is nothing here" and a second one costs it a
special case.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from . import script
from .report import Column, Lane, normalise

if TYPE_CHECKING:
    from ..core.regions import Scope

# -- the shared value shapes -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A mesh participant's identity, and nothing else.

    The most repeated concept in either face. *Identity only*: when a node was heard, how
    strong it came in, where it says it is — those belong to the **row**, because they
    differ per surface and a node object that carried them would mean something different
    in every one.

    Attributes:
        name: The name the node advertises, or ``None`` where none was ever heard —
            including where a surface substitutes a bare hash for one.
        key: The full public key, lowercase hex, **never truncated**: a key cut short is
            not something a caller can hand back to ``--to`` or ``--path``. ``None`` where
            only a hash was ever heard.
        hash: The short derived id, at the width the surrounding surface addressed the
            node by — never ``key`` shortened by this module's own choosing.
        type: The advertised role in the lexicon's words (``node``, ``repeater``,
            ``room``, ``sensor``), or ``None`` where the advert never said.
        is_self: Whether this is our own node. The plain face draws it like any other hop,
            which is the point: "you already know who this is" is true of the person at
            the prompt and false of whoever reads the file afterwards. The machine face
            carries it as ``self``, because a parser has no way to work it out at all.
    """

    name: str | None = None
    key: str | None = None
    hash: str | None = None
    type: str | None = None
    is_self: bool = False


@dataclass(frozen=True, slots=True)
class Position:
    """A shared location, in decimal degrees.

    Unrounded in the document, as stored: the plain face's five decimals are a column-width
    concession and a consumer should not inherit one.
    """

    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class ChannelRef:
    """One channel slot's identity. The secret is deliberately not in it.

    A channel object travels through listings and send receipts, and a key that rode along
    in all of them would be a key printed by commands nobody asked to print one. It
    appears only where the caller explicitly asked (``channels share``), under its own
    field, beside this.

    Attributes:
        slot: The 0-based index every ``channels`` subcommand's ``INDEX`` takes.
        name: The channel name.
        public: Whether the key is derived from the name (a ``#`` channel or a firmware
            default) rather than secret.
        hash: The channel hash, lowercase hex.
    """

    slot: int
    name: str
    public: bool
    hash: str | None = None


@dataclass(frozen=True, slots=True)
class Rendered:
    """A typed value beside the one plain rendering only its own registry can produce.

    Almost every cell is derivable from its value alone, which is what lets a
    :class:`~meshterm.ui.report.Lane` be a function of one argument. A *setting* is the
    exception: what ``config set`` takes back for a given key is a question only that key's
    spec can answer — an enum wants its number, an empty string wants ``""``, a preference
    drops the unit its page shows — and the spec is per row, not per column.

    So the row carries both halves, computed once where the spec is in hand. The plain
    face prints ``text`` and the document emits ``value``, and neither has to parse the
    other's output to get at what it needs, which is the same rule the rest of this module
    keeps by other means.

    Attributes:
        value: The typed value, or ``None`` where the source never reported one.
        text: What the plain face prints, in the form the matching ``set`` accepts back.
    """

    value: Any
    text: str


def node_json(ref: NodeRef | None) -> dict[str, Any] | None:
    """One node as its machine object, or ``null`` where there is no node at all."""
    if ref is None:
        return None
    return {
        "name": ref.name or None,
        "key": (ref.key or None) and ref.key.lower(),
        "hash": (ref.hash or None) and ref.hash.lower(),
        "type": ref.type or None,
        "self": ref.is_self,
    }


def position_json(where: Position | None) -> dict[str, Any] | None:
    """One position as its machine object, or ``null`` where none was ever shared.

    The whole object is ``null`` rather than a pair of ``null``s: a latitude without a
    longitude is not half a location, it is no location.
    """
    return None if where is None else {"lat": where.lat, "lon": where.lon}


def channel_json(channel: ChannelRef | None) -> dict[str, Any] | None:
    """One channel as its machine object, or ``null`` for an empty slot."""
    if channel is None:
        return None
    return {
        "slot": channel.slot,
        "name": channel.name,
        "public": channel.public,
        "hash": (channel.hash or None) and channel.hash.lower(),
    }


# -- the constructors ------------------------------------------------------------------

#: How each of a node's plain lanes reads its cell. A surface picks the ones it has room
#: for; the machine object is the same five keys either way.
_NODE_LANES = {
    "name": lambda ref: script.name(ref.name if ref else None),
    # The word, not a glyph: `▲` without its colour would need a legend and the CLI has no
    # legends. `unknown` is a word a reader acts on; the document writes `null`.
    "type": lambda ref: ref.type if ref and ref.type else "unknown",
    "hash": lambda ref: ((ref.hash or "").lower() if ref else "") or script.NONE,
    "key": lambda ref: ((ref.key or "").lower() if ref else "") or script.NONE,
}


def node(key: str = "node", *, lanes: Sequence[tuple[str, str]] = (("name", "NAME"),)) -> Column:
    """One node: the shared five-key object, and whichever plain lanes fit.

    Args:
        key: The machine key, and the key the row holds its :class:`NodeRef` at.
        lanes: ``(part, header)`` pairs naming the plain columns to draw, in order. The
            parts are ``name``, ``type``, ``hash`` and ``key``. A surface with one column
            to spend draws the name; a per-hop table draws the hash, because that is what
            joins it to the route line above it.

    Returns:
        The column.
    """
    return Column(
        key=key,
        lanes=tuple(Lane(header=header, render=_NODE_LANES[part]) for part, header in lanes),
        json=node_json,
    )


def position(key: str = "position", header: str = "LOCATION") -> Column:
    """A shared location: one plain column, one machine object.

    One column and not two. A coordinate pair is one fact to a reader, it is what goes
    into a map's search box verbatim, and two columns of ``-`` for every node that has
    never shared a position is worse than one.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=_position_cell),),
        json=position_json,
    )


def channel(
    key: str = "channel",
    *,
    lanes: Sequence[tuple[str, str]] = (("name", "NAME"),),
) -> Column:
    """One channel slot: the shared four-key object, and whichever plain lanes fit.

    Args:
        key: The machine key.
        lanes: ``(part, header)`` pairs; the parts are ``slot``, ``name``, ``type`` and
            ``hash``. ``type`` is the plain face's word for the ``public`` boolean —
            ``public``/``private`` reads where ``true``/``false`` would need the heading
            to say which way round it runs.
    """
    parts = {
        "slot": lambda c: script.number(c.slot if c else None),
        "name": lambda c: script.name(c.name if c else None),
        "type": lambda c: ("public" if c.public else "private") if c else script.NONE,
        "hash": lambda c: ((c.hash or "").lower() if c else "") or script.NONE,
    }
    return Column(
        key=key,
        lanes=tuple(Lane(header=header, render=parts[part]) for part, header in lanes),
        json=channel_json,
    )


def scope_json(scope: Scope | None) -> dict[str, Any] | None:
    """A flood's scope as its machine object, or ``null`` for a frame that has none.

    ``state`` is ``scoped`` (``region`` names it), ``unknown`` (scoped, no known region
    reproduces ``code``) or ``unscoped``; ``code`` is the frame's first transport code as
    lowercase hex, kept even when unresolved so two frames can be seen to share a region.
    A direct frame is ``null`` — never region-filtered, so it has no scope to report.
    """
    if scope is None:
        return None
    return {"state": scope.state, "region": scope.region, "code": scope.code}


def _scope_cell(scope: Scope | None) -> str:
    """A scope's plain word: the region, ``unknown`` for an unresolved one, or ``unscoped``."""
    if scope is None:
        return script.NONE
    if scope.state == "scoped" and scope.region:
        return script.name(scope.region)
    return "unknown" if scope.scoped else "unscoped"


def scope(key: str = "scope", header: str = "SCOPE") -> Column:
    """A flood's scope — the region a repeater relays it in — as one lane and one object.

    The row holds a :class:`~meshterm.core.regions.Scope` (or ``None`` for a direct frame).
    The plain lane is a word, never the code: a four-digit hash in a column of region
    names reads as a region nobody has, and the document carries the code for whoever
    needs it.
    """
    return Column(key=key, lanes=(Lane(header=header, render=_scope_cell),), json=scope_json)


def when(key: str, header: str, *, absent: str = script.NONE) -> Column:
    """A time the reader reads as an age: ``5m``, ``3h``, ``never``.

    The default form for every time in a listing, because "recently?" is the question the
    reader typed the command to ask and an ISO instant makes them do arithmetic to answer
    it. ``--absolute`` swaps the whole language back for a run; the document is UTC
    regardless, since it is read somewhere else and often later.

    Args:
        key: The machine key. It ends in ``_at`` by convention — a JSON key is a type hint
            for a parser, and the suffix is the hint.
        header: The plain heading. It must name the *fact* — ``HEARD``, ``RECORDED`` —
            rather than the form, because ``--absolute`` changes the form underneath it
            and a column headed ``AGE`` holding an ISO instant is a heading that lies.
        absent: What an unknown time reads as plain. ``never`` where the row exists and
            the event has not happened (a node not yet heard, a conversation with nothing
            said in it); the default token where the row has no such time at all.

    Returns:
        The column.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: _time(v, absent=absent), align="right"),),
    )


def instant(key: str, header: str) -> Column:
    """A time that *is* the fact, printed absolutely whatever ``--absolute`` says.

    Three surfaces want this and each for the same reason — the instant is the answer, not
    a way of saying how long ago something was. The device's own clock. An appointment set
    with ``--at``, which is a wall-clock time the caller chose. And a live capture's own
    ``TIME`` column, every row of which would otherwise read ``now``.
    """
    return Column(key=key, lanes=(Lane(header=header, render=script.stamp),))


def snr(key: str, header: str) -> Column:
    """A signal-to-noise reading: ``+5.1`` plain, ``5.1`` in the document.

    The plain sign is a column convention — a lane of readings only reads as a ladder when
    every one of them declares its direction. A number in JSON carries its own sign and a
    leading ``+`` would make it a string.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: script.number(v, "+.1f"), align="right"),),
        json=lambda v: None if v is None else round(float(v), 1),
    )


def integer(key: str, header: str) -> Column:
    """A whole number, right-aligned so a column of them is comparable by eye."""
    return Column(key=key, lanes=(Lane(header=header, render=script.number, align="right"),))


def decimal(key: str, header: str, spec: str = ".1f") -> Column:
    """A measured number, formatted for the column and left unrounded in the document."""
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: script.number(v, spec), align="right"),),
    )


def flag(key: str, header: str) -> Column:
    """A boolean: ``yes``/``no`` for a reader, ``true``/``false`` for a parser.

    ``None`` is a third answer and stays one — ``acked`` on a channel message is not
    ``false``, it is *there is no acknowledgement on a channel*, and the two are different
    things to anyone counting delivery failures.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=_flag_cell),),
    )


def word(key: str, header: str) -> Column:
    """A member of a closed set — a state, a role, an outcome — as its own lowercase word.

    Both faces say the same word, so ``courier send``'s ``no ack`` reads the same in a
    document as it does on the line the help lists it on.
    """
    return Column(key=key, lanes=(Lane(header=header, render=lambda v: v or script.NONE),))


def name(key: str, header: str) -> Column:
    """A bare name that is not a node's — a channel label, a conversation, a device."""
    return Column(key=key, lanes=(Lane(header=header, render=script.name),))


def path(key: str, header: str) -> Column:
    """A place on this filesystem: a config directory, a database, a file just written.

    Its own constructor rather than :func:`word` because the *typed* value is a
    :class:`~pathlib.Path` and nothing else in the lane vocabulary turns one into text —
    ``word`` hands it straight to Rich, which cannot render it. The machine face was
    already right: :func:`~meshterm.ui.report.normalise` has always spelled a Path as its
    string, so this closes the plain half of a shape that was only ever half-declared.

    Printed as the host writes it, separators and all, since the one thing a reader does
    with a path is paste it back into their own shell.
    """
    return Column(key=key, lanes=(Lane(header=header, render=_path_cell),))


def free(key: str, header: str) -> Column:
    """Free text a stranger filled in: escaped so it can never end its own record.

    Always the last lane in a listing, because it is the one field with no width. The
    document carries it **raw and unshortened** — a JSON string holds a newline natively,
    so the escaping is a plain-text necessity rather than something to inflict on a
    consumer.
    """
    return Column(key=key, lanes=(Lane(header=header, render=script.text),))


def route(key: str = "route", header: str = "ROUTE") -> Column:
    """A walked hop sequence: drawn with arrows plain, an array of nodes in the document.

    The row holds :class:`NodeRef` objects in propagation order, so the document's hops are
    the same objects a listing's rows carry and a consumer's node handler is written once.
    ``None`` where nothing came home — never ``[]``, which would claim a walk of no hops.
    """
    return Column(
        key=key,
        lanes=(
            Lane(
                header=header,
                render=lambda hops: (
                    script.route([(h.name, h.hash) for h in hops]) if hops else script.NONE
                ),
            ),
        ),
        json=lambda hops: None if hops is None else [node_json(hop) for hop in hops],
    )


def spec(key: str = "path", header: str = "path") -> Column:
    """A forced path exactly as the radio was given it: ``a1,d4,a1``, or absent.

    The one line on either face that round-trips into ``--path``, so it keeps its commas
    and grows nothing. ``None`` means the device routed the walk itself: the plain face
    says ``auto`` because a reader needs a word there, and the document says ``null``
    because ``auto`` is not a path spec and R3 already has a token for "there is no spec
    here".
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: v if v else "auto"),),
    )


def rendered(key: str, header: str) -> Column:
    """A setting's value: what ``set`` takes back plain, and the typed value in the document.

    Takes a :class:`Rendered` (see there for why this one concept needs both halves in the
    row). The round-trip claim is the plain face's whole design for these commands —
    ``config show > f`` and feeding ``f`` back in must work — and the document's typed
    value is the machine face's largest single gain over it: ``9`` and not ``"9"``.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=_rendered_cell),),
        json=lambda r: None if r is None else r.value,
    )


def hexid(key: str, header: str) -> Column:
    """A hash or a key on its own, lowercase, never truncated and never elided."""
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: (v or "").lower() or script.NONE),),
        json=lambda v: (v or None) and str(v).lower(),
    )


def note(key: str, header: str) -> Column:
    """Prose for a reader, with no machine face at all.

    A preference's DESCRIPTION is the case: it explains a preference to somebody deciding
    whether to change it, and there is nothing in it a program would branch on. Declaring
    it as a column rather than smuggling it in keeps the listing's two faces stated in one
    place.
    """
    return Column(key=key, lanes=(Lane(header=header, render=script.text),), json=None)


def plain_only(column: Column) -> Column:
    """The same concept, drawn for a reader and left out of the document.

    For a lane the machine face says *better* somewhere else rather than not at all. A
    transcript's ``PEER`` is the case: one column holding whichever of a channel or a
    contact the conversation had, because a reader has the conversation in front of them —
    where the document keeps ``channel`` and ``node`` apart, since a parser has to know
    which of the two it is holding and cannot tell from the label.
    """
    from dataclasses import replace

    return replace(column, json=None)


def hidden(key: str) -> Column:
    """A fact the document carries and the plain face has no room for.

    The mirror of :func:`note`. ``config show`` prints a setting's key and its value —
    that is what a reader came for — while the document also says what type it is, what an
    enum's number is called, and whether the value was withheld.
    """
    return Column(key=key)


def _rendered_cell(value: Rendered | None) -> str:
    """One setting's cell: the text its own spec produced, or the absent token."""
    return script.NONE if value is None else value.text


def _path_cell(value: Any) -> str:
    """A path as its own text, or the absent token where there is none."""
    return script.NONE if value is None else str(value)


def _position_cell(where: Position | None) -> str:
    """One position's cell: the pair, or the absent token."""
    return script.NONE if where is None else script.location(where.lat, where.lon)


def _flag_cell(value: bool | None) -> str:
    """One boolean's cell, keeping ``None`` as an absence rather than a ``no``."""
    return script.NONE if value is None else ("yes" if value else "no")


def _time(value: Any, *, absent: str) -> str:
    """Render a time in whichever language this run speaks (see :func:`when`).

    ``absent`` survives the switch to timestamps, because it is not a rendering choice:
    ``never`` says the event has not happened, and ``--absolute`` was asked for a different
    *form* of time, not for one fewer fact.
    """
    if not isinstance(value, datetime) and value is not None:
        return normalise(value)  # pragma: no cover - a row holding something else is a bug
    if value is None:
        return absent
    return script.stamp(value) if _absolute() else script.age(value, absent=absent)


def _absolute() -> bool:
    """Whether this run prints absolute times.

    Read from the preference registry rather than a module flag, because *how MeshTerm
    behaves* is a preference by this project's own rule — and ``--absolute`` is then
    simply that preference overridden for one run (see
    :func:`~meshterm.cli.main_callback`), rather than a second switch saying the same
    thing in a place a reader cannot find.
    """
    from ..core.preferences import current

    return str(current().cli_time_format) == "absolute"
