# SPDX-License-Identifier: Apache-2.0
"""Typed events that the always-on mesh event hub carries.

These are the domain-level events that go from the connected device to all the
subscribers that want them (refer to :class:`~meshterm.services.event_hub.EventHub`).
They are plain dataclasses with no I/O dependencies, and they are in this module with the
other domain models. Thus the hub, its subscribers, and the tests all use the same types,
and they do not import the device layer.

The hub carries the unrequested inbound streams to which a client reacts: overheard
packets (:class:`~meshterm.core.models.Observation`), inbound text messages
(:class:`~meshterm.core.models.Message`), and delivery acknowledgements
(:class:`~meshterm.core.models.Ack`). To add a new kind (contact updates, path changes),
add an :class:`EventKind` member and put the payload into a :class:`MeshEvent`. A
subscriber that does not ask for the new kind does not change.

This module does not carry the replies that match a request (the ``TRACE_DATA`` of a
trace, the result of a login). The command that sent the request waits for its reply
directly. Thus these replies work when the hub runs and also when it does not run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .models import Ack, Message, Observation, utcnow


class EventKind(str, Enum):
    """The classes of event that the hub can send to its subscribers.

    It is a ``str`` enum, so that the values are easy to read in the log and when they are
    serialized. New client features add their own members here.
    """

    OBSERVATION = "observation"
    MESSAGE = "message"
    ACK = "ack"


@dataclass(slots=True)
class MeshEvent:
    """One event that the hub gives to its subscribers.

    It is a thin envelope. :attr:`kind` selects which subscribers receive it, and
    :attr:`payload` carries the data for that kind (an
    :class:`~meshterm.core.models.Observation` for :attr:`EventKind.OBSERVATION`). An
    accessor for each kind, such as :attr:`observation`, gives callers a typed value. Thus
    the callers do not have to match on ``kind`` themselves.

    Attributes:
        kind: The class of this event. It sets the payload type and the subscribers that
            receive the event.
        payload: The data object for the kind.
        received_at: The time when the hub sent the event (UTC).
    """

    kind: EventKind
    payload: object = None
    received_at: datetime = field(default_factory=utcnow)

    @property
    def observation(self) -> Observation | None:
        """The carried :class:`Observation`, or ``None`` if this event is not an observation."""
        return self.payload if isinstance(self.payload, Observation) else None

    @property
    def message(self) -> Message | None:
        """The carried :class:`Message`, or ``None`` if this event is not a message."""
        return self.payload if isinstance(self.payload, Message) else None

    @property
    def ack(self) -> Ack | None:
        """The carried :class:`Ack`, or ``None`` if this event is not an acknowledgement."""
        return self.payload if isinstance(self.payload, Ack) else None

    @classmethod
    def observation_event(cls, obs: Observation) -> MeshEvent:
        """Wrap an :class:`Observation` as an :attr:`EventKind.OBSERVATION` event."""
        return cls(kind=EventKind.OBSERVATION, payload=obs)

    @classmethod
    def message_event(cls, message: Message) -> MeshEvent:
        """Wrap a :class:`Message` as an :attr:`EventKind.MESSAGE` event."""
        return cls(kind=EventKind.MESSAGE, payload=message)

    @classmethod
    def ack_event(cls, ack: Ack) -> MeshEvent:
        """Wrap an :class:`Ack` as an :attr:`EventKind.ACK` event."""
        return cls(kind=EventKind.ACK, payload=ack)
