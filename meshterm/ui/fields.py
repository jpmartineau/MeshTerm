# SPDX-License-Identifier: Apache-2.0
"""The shared shapes that a report is built from, with one constructor for each concept.

A node has the same meaning in ``contacts``, ``monitor``, ``courier list``, ``trace``, and
``records``. It must look the same in all five, and a program must parse it the same in
all five. A review cannot guarantee this, but the construction can. Each listing that
mentions a node asks this module for the column. Thus there is only one definition of the
names of the plain lanes of a node and of the content of its machine object.

Each constructor returns one :class:`~meshterm.ui.report.Column`. The column has both
projections of one typed value. A row holds the typed value, for example a
:class:`NodeRef`, a :class:`Position`, a :class:`~datetime.datetime`, or a bare float. A
row never holds a formatted cell. Thus the plain face can print ``+5.1`` and the machine
face can print ``5.1``, and neither face parses the output of the other face.

The most important constructor is :func:`node`, and it is the reason for the design:
**one machine key expands to as many plain columns as the listing has room for.**
``contacts`` uses four (a name, a role, a hash, and a key). The live stream of ``monitor``
uses two. The table of hops of a trace uses one. But all four documents have the same
object with five keys, because the lanes are a choice of rendering and the shape is not.

There are two absences, and they are separate on purpose. ``None`` means "we do not know".
In the plain face it is :data:`~meshterm.ui.script.NONE`, and in JSON it is ``null``. A
word that replaces an absence is a different thing. ``unknown`` in a ``TYPE`` column is a
word that the user understands, so the plain face keeps it. But the document writes
``null``, because a consumer already has one spelling for "there is nothing here". A
second spelling makes a special case for the consumer.
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
    """The identity of a node on the mesh, and nothing else.

    This is the concept that both faces use most. It has the identity only. The time when
    the node was heard, the signal strength, and the position that the node reports belong
    to the **row**. They are different in each listing, and a node object that had them
    would have a different meaning in each listing.

    Attributes:
        name: The name that the node advertises. It is ``None`` if no name was ever
            heard, also when a listing puts a bare hash in the place of the name.
        key: The full public key, in lowercase hex. It is **never truncated**, because the
            caller cannot give a truncated key back to ``--to`` or ``--path``. It is
            ``None`` if only a hash was ever heard.
        hash: The short id that comes from the key. Its width is the width that the
            listing uses to address the node. This module never makes it from ``key`` by
            its own choice.
        type: The advertised role, in the words of the lexicon (``node``, ``repeater``,
            ``room``, ``sensor``). It is ``None`` if the advert did not give it.
        is_self: Whether this is our node. The plain face draws it like any other hop, on
            purpose. "You already know who this is" is true for the person at the prompt.
            It is not true for the person who reads the file afterwards. The machine face
            gives it as ``self``, because a parser has no way to find it.
    """

    name: str | None = None
    key: str | None = None
    hash: str | None = None
    type: str | None = None
    is_self: bool = False


@dataclass(frozen=True, slots=True)
class Position:
    """A shared location, in decimal degrees.

    The document has the values as they are stored, not rounded. The five decimals of the
    plain face are a concession to the width of the column. A consumer must not get that
    concession.
    """

    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class ChannelRef:
    """The identity of one channel slot. The secret is not in it, on purpose.

    A channel object goes through listings and send receipts. If a key went with it in all
    of them, commands would print a key when nobody asked for one. The key appears only
    where the caller asked for it (``channels share``). It is in its own field, beside
    this object.

    Attributes:
        slot: The index, from 0, that the ``INDEX`` of each ``channels`` subcommand takes.
        name: The channel name.
        public: Whether the key comes from the name (a ``#`` channel or a default of the
            firmware) and is not secret.
        hash: The channel hash, in lowercase hex.
    """

    slot: int
    name: str
    public: bool
    hash: str | None = None


@dataclass(frozen=True, slots=True)
class Rendered:
    """A typed value, with the one plain text that only its own registry can make.

    The cell of almost every column can come from its value alone. Thus a
    :class:`~meshterm.ui.report.Lane` can be a function of one argument. A *setting* is the
    exception. Only the spec of a key can tell what ``config set`` accepts for that key.
    An enum needs its number. An empty string needs ``""``. A preference does not have the
    unit that its page shows. The spec is for each row and not for each column.

    Thus the row has both parts, and the code makes them one time, where the spec is
    available. The plain face prints ``text`` and the document emits ``value``. Neither
    face must parse the output of the other face. The rest of this module has the same
    rule, but it keeps it in other ways.

    Attributes:
        value: The typed value. It is ``None`` if the source never reported a value.
        text: The text that the plain face prints, in the form that the matching ``set``
            accepts.
    """

    value: Any
    text: str


def node_json(ref: NodeRef | None) -> dict[str, Any] | None:
    """One node as its machine object, or ``null`` if there is no node."""
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
    """One position as its machine object, or ``null`` if no position was ever shared.

    The whole object is ``null``. It is not a pair of ``null`` values, because a latitude
    without a longitude is not half of a location. It is no location.
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

#: How each plain lane of a node makes its cell. A listing selects the lanes that it has
#: room for. The machine object has the same five keys in each case.
_NODE_LANES = {
    "name": lambda ref: script.name(ref.name if ref else None),
    # The word, not a glyph. A `▲` with no colour needs a legend, and the CLI has no
    # legends. `unknown` is a word that the user can act on. The document writes `null`.
    "type": lambda ref: ref.type if ref and ref.type else "unknown",
    "hash": lambda ref: ((ref.hash or "").lower() if ref else "") or script.NONE,
    "key": lambda ref: ((ref.key or "").lower() if ref else "") or script.NONE,
}


def node(key: str = "node", *, lanes: Sequence[tuple[str, str]] = (("name", "NAME"),)) -> Column:
    """One node: the shared object with five keys, and the plain lanes that fit.

    Args:
        key: The machine key. It is also the key at which the row holds its
            :class:`NodeRef`.
        lanes: ``(part, header)`` pairs that name the plain columns to draw, in order. The
            parts are ``name``, ``type``, ``hash``, and ``key``. A listing that has one
            column to use draws the name. A table of hops draws the hash, because the hash
            joins the table to the route line above it.

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

    It is one column and not two. A pair of coordinates is one fact for the user. The user
    puts the pair as it is in the search box of a map. Two columns of ``-`` for each node
    that never shared a position are worse than one column.
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
    """One channel slot: the shared object with four keys, and the plain lanes that fit.

    Args:
        key: The machine key.
        lanes: ``(part, header)`` pairs. The parts are ``slot``, ``name``, ``type``, and
            ``hash``. ``type`` is the word of the plain face for the boolean ``public``.
            ``public`` and ``private`` are clear in a column. With ``true`` and ``false``,
            the heading must say which value has which meaning.
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
    """The scope of a flood as its machine object, or ``null`` if the packet has none.

    ``state`` is ``scoped`` (``region`` gives the name), ``unknown`` (it is scoped, but no
    known region gives ``code``), or ``unscoped``. ``code`` is the first transport code of
    the packet, in lowercase hex. It is kept also when it is not resolved, so that you can
    see that two packets have the same region. A direct packet is ``null``. No repeater
    filters it by region, so it has no scope to report.
    """
    if scope is None:
        return None
    return {"state": scope.state, "region": scope.region, "code": scope.code}


def _scope_cell(scope: Scope | None) -> str:
    """The plain word for a scope: the region, ``unknown`` (not resolved), or ``unscoped``."""
    if scope is None:
        return script.NONE
    if scope.state == "scoped" and scope.region:
        return script.name(scope.region)
    return "unknown" if scope.scoped else "unscoped"


def scope(key: str = "scope", header: str = "SCOPE") -> Column:
    """The scope of a flood as one lane and one object.

    The scope is the region in which a repeater relays the flood. The row holds a
    :class:`~meshterm.core.regions.Scope`, or ``None`` for a direct packet. The plain lane
    is a word and never the code. A hash of four digits in a column of region names looks
    like a region that nobody has. The document has the code for a consumer that needs it.
    """
    return Column(key=key, lanes=(Lane(header=header, render=_scope_cell),), json=scope_json)


def regions(key: str = "regions", header: str = "REGIONS") -> Column:
    """A set of region names: joined in the plain face, an array in the document.

    The regions are what a repeater relays. The row holds a sequence of bare names and
    never the wildcard. "Unscoped" is a separate concept (a :func:`flag` beside this
    column), because ``*`` is not the name of a region. If it was in the array, a consumer
    that reads the array for names would have to know that it must skip it. An empty
    sequence is a real answer: "no regions". It is the word ``none`` in the plain face and
    ``[]`` in the document. ``None`` means "not known". It is the absent token in the plain
    face and ``null`` in the document.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=_regions_cell),),
        json=lambda names: None if names is None else [str(n) for n in names],
    )


def when(key: str, header: str, *, absent: str = script.NONE) -> Column:
    """A time that the user reads as an age: ``5m``, ``3h``, ``never``.

    This is the default form for each time in a listing. The user typed the command to ask
    "was it recent?". An ISO instant makes the user do arithmetic to get the answer.
    ``--absolute`` changes the whole language back for one run. The document is always in
    UTC, because it is read in another place and often at a later time.

    Args:
        key: The machine key. By convention it ends in ``_at``. A JSON key is a type hint
            for a parser, and the suffix is the hint.
        header: The plain heading. It must name the *fact* (``HEARD``, ``RECORDED``) and
            not the form. ``--absolute`` changes the form under the heading, and a column
            with the heading ``AGE`` that holds an ISO instant has a heading that is not
            true.
        absent: The plain text for an unknown time. Use ``never`` if the row exists and the
            event did not occur (a node that was not heard yet, or a conversation with no
            message in it). Use the default token if the row has no such time.

    Returns:
        The column.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: _time(v, absent=absent), align="right"),),
    )


def instant(key: str, header: str) -> Column:
    """A time that is the fact itself. It is always printed as an absolute time.

    The setting of ``--absolute`` does not change this. Three listings need this, and they
    have the same reason: the instant is the answer, and it is not a way to say how long
    ago something occurred. The first is the clock of the device. The second is an
    appointment that you set with ``--at``, which is a wall-clock time that the caller
    selected. The third is the ``TIME`` column of a live capture, where each row would
    otherwise be ``now``.
    """
    return Column(key=key, lanes=(Lane(header=header, render=script.stamp),))


def snr(key: str, header: str) -> Column:
    """A signal-to-noise reading: ``+5.1`` in the plain face, ``5.1`` in the document.

    The plain sign is a convention of the column. A lane of readings is easy to compare
    only when each reading shows its direction. A number in JSON has its own sign, and a
    ``+`` at the start would make it a string.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: script.number(v, "+.1f"), align="right"),),
        json=lambda v: None if v is None else round(float(v), 1),
    )


def integer(key: str, header: str) -> Column:
    """A whole number, aligned right so that the user can compare a column of them by eye."""
    return Column(key=key, lanes=(Lane(header=header, render=script.number, align="right"),))


def decimal(key: str, header: str, spec: str = ".1f") -> Column:
    """A measured number. It is formatted for the column and is not rounded in the document."""
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: script.number(v, spec), align="right"),),
    )


def flag(key: str, header: str) -> Column:
    """A boolean: ``yes`` and ``no`` for the user, ``true`` and ``false`` for a parser.

    ``None`` is a third answer and stays a third answer. ``acked`` on a channel message is
    not ``false``. It means "a channel has no acknowledgement". These are different things
    for a person who counts delivery failures.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=_flag_cell),),
    )


def word(key: str, header: str) -> Column:
    """A member of a closed set (a state, a role, or an outcome), as its own lowercase word.

    Both faces use the same word. Thus the ``no ack`` of ``courier send`` is the same in a
    document as on the line where the help lists it.
    """
    return Column(key=key, lanes=(Lane(header=header, render=lambda v: v or script.NONE),))


def name(key: str, header: str) -> Column:
    """A bare name that is not the name of a node: a channel label, a conversation, or a device."""
    return Column(key=key, lanes=(Lane(header=header, render=script.name),))


def path(key: str, header: str) -> Column:
    """A place in this filesystem: a config directory, a database, or a new file.

    This is a separate constructor and not :func:`word`. The typed value is a
    :class:`~pathlib.Path`, and no other part of the lane vocabulary changes one into text.
    ``word`` gives it directly to Rich, which cannot render it. The machine face was
    already correct: :func:`~meshterm.ui.report.normalise` always wrote a Path as its
    string. Thus this constructor completes the plain half of a shape that had only one
    half.

    The plain face prints the path as the host writes it, with the separators. The user
    usually pastes a path into their own shell.
    """
    return Column(key=key, lanes=(Lane(header=header, render=_path_cell),))


def free(key: str, header: str) -> Column:
    """Free text that a stranger wrote. It is escaped so that it can never end its own record.

    It is always the last lane in a listing, because it is the only field with no limit on
    its width. The document has it **raw and not shortened**. A JSON string can hold a
    newline, so the escaping is necessary only in plain text. The code must not do it for
    a consumer.
    """
    return Column(key=key, lanes=(Lane(header=header, render=script.text),))


def route(key: str = "route", header: str = "ROUTE") -> Column:
    """A sequence of hops that a trace walked: arrows in the plain face, an array in the document.

    The document has an array of nodes. The row holds :class:`NodeRef` objects in the
    order of propagation. Thus the hops of the document are the same objects that the rows
    of a listing have, and a consumer writes its node handler only one time. The value is
    ``None`` if no answer came back. It is never ``[]``, because that would say that the
    walk had no hops.
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
    """A forced path exactly as the radio got it: ``a1,d4,a1``, or absent.

    This is the one line on either face that can go back into ``--path``, so it keeps its
    commas and has nothing added. ``None`` means that the device selected the route of the
    walk. The plain face says ``auto``, because the user needs a word there. The document
    says ``null``, because ``auto`` is not a path spec and R3 already has a token for
    "there is no spec here".
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: v if v else "auto"),),
    )


def rendered(key: str, header: str) -> Column:
    """The value of a setting: text that ``set`` accepts (plain), the typed value (document).

    This column takes a :class:`Rendered`. Refer to it for the reason why this one concept
    needs both parts in the row. The plain face is designed for a round trip in these
    commands: ``config show > f`` and then the input of ``f`` must work. The typed value
    in the document is the largest gain of the machine face over the plain face: ``9`` and
    not ``"9"``.
    """
    return Column(
        key=key,
        lanes=(Lane(header=header, render=_rendered_cell),),
        json=lambda r: None if r is None else r.value,
    )


def hexid(key: str, header: str) -> Column:
    """A hash or a key by itself, in lowercase. It is never truncated and never elided."""
    return Column(
        key=key,
        lanes=(Lane(header=header, render=lambda v: (v or "").lower() or script.NONE),),
        json=lambda v: (v or None) and str(v).lower(),
    )


def note(key: str, header: str) -> Column:
    """Prose for the user, with no machine face.

    The DESCRIPTION of a preference is an example. It explains a preference to a person who
    decides whether to change it, and a program has no decision to make from it. The
    listing declares it as a column and does not add it in a hidden way. Thus the two
    faces of the listing are stated in one place.
    """
    return Column(key=key, lanes=(Lane(header=header, render=script.text),), json=None)


def plain_only(column: Column) -> Column:
    """The same concept, drawn for the user and not in the document.

    Use it for a lane that the machine face says in a better way in another place, and not
    that it does not say it. The ``PEER`` of a transcript is an example. It is one column
    that holds a channel or a contact, whichever the conversation had, because the user
    has the conversation in front of them. The document keeps ``channel`` and ``node``
    separate, because a parser must know which of the two it has, and it cannot find that
    from the label.
    """
    from dataclasses import replace

    return replace(column, json=None)


def hidden(key: str) -> Column:
    """A fact that the document has and the plain face has no room for.

    It is the opposite of :func:`note`. ``config show`` prints the key and the value of a
    setting, because the user wants these. The document also says the type of the setting,
    the name of the number of an enum, and whether the value was withheld.
    """
    return Column(key=key)


def _regions_cell(names: Sequence[str] | None) -> str:
    """The plain cell of a region set: names joined with commas, ``none`` if empty, or absent."""
    if names is None:
        return script.NONE
    return ", ".join(script.name(n) for n in names) if names else "none"


def _rendered_cell(value: Rendered | None) -> str:
    """The cell of one setting: the text that its own spec made, or the absent token."""
    return script.NONE if value is None else value.text


def _path_cell(value: Any) -> str:
    """A path as its own text, or the absent token if there is no path."""
    return script.NONE if value is None else str(value)


def _position_cell(where: Position | None) -> str:
    """The cell of one position: the pair of coordinates, or the absent token."""
    return script.NONE if where is None else script.location(where.lat, where.lon)


def _flag_cell(value: bool | None) -> str:
    """The cell of one boolean. ``None`` stays an absence and does not become ``no``."""
    return script.NONE if value is None else ("yes" if value else "no")


def _time(value: Any, *, absent: str) -> str:
    """Render a time in the language that this run uses (refer to :func:`when`).

    ``absent`` stays when the run changes to timestamps, because it is not a choice of
    rendering. ``never`` says that the event did not occur. The user asked for
    ``--absolute`` to get a different *form* of time, and not to get one fact less.
    """
    if not isinstance(value, datetime) and value is not None:
        return normalise(value)  # pragma: no cover - a row with another type is a bug
    if value is None:
        return absent
    return script.stamp(value) if _absolute() else script.age(value, absent=absent)


def _absolute() -> bool:
    """Whether this run prints absolute times.

    The function reads the value from the preference registry and not from a flag of the
    module. By a rule of this project, how MeshTerm behaves is a preference. Thus
    ``--absolute`` is that preference, overridden for one run (refer to
    :func:`~meshterm.cli.main_callback`). It is not a second switch that has the same
    meaning in a place where the user cannot find it.
    """
    from ..core.preferences import current

    return str(current().cli_time_format) == "absolute"
