# SPDX-License-Identifier: Apache-2.0
"""Repeated traces with duty-cycle pacing, and helpers to parse traces.

:func:`run_traces` is the measurement primitive on which the TX optimizer and the path
probe are built. It runs a trace N times with pacing, can report the progress, and stores
each trace. The ``trace`` tool itself intentionally does *not* loop. It transmits exactly
one trace each time that it runs, because repeaters penalize (and can blacklist) nodes
that send bursts of traffic. But it uses the path parsing and the node-name resolution of
this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..core.connection import Device
from ..core.models import Contact, NameKeyResolver, NodeResolver, TraceResult

# The trace arithmetic is in ``core``, because the device layer uses it to size its own
# sends. This module exports the names again, so that the old public names of the runner
# continue to work.
from ..core.tracing import (  # noqa: F401 - exported again for the callers of this module
    TRACE_TIMEOUT_BASE_S,
    TRACE_TIMEOUT_CEILING_S,
    TRACE_TIMEOUT_FLOOD_S,
    TRACE_TIMEOUT_PER_HOP_S,
    path_hash_flags,
    trace_timeout,
)

ProgressCallback = Callable[[int, int, TraceResult], None]

_HEX_DIGITS = frozenset("0123456789abcdef")


def make_node_resolver(
    contacts: list[Contact] | None,
    stored_names: dict[str, str] | None = None,
) -> NodeResolver:
    """Build a resolver that names a trace hop from its hash.

    Trace replies identify each repeater only by a short hash: the first bytes of its
    public key. This resolver changes that hash into the name of the contact when we know
    the node. Thus the results show names instead of hex that has no meaning for the
    user. For an unknown node, it uses the raw hash. ``stored_names`` adds the names that
    the recorder *ever* heard (the latest advertised name of each node in the
    repository) to the contact list of the device. Thus a node that the companion never
    added, or forgot later, still resolves. The contacts are first in the list, thus
    they win.

    Args:
        contacts: The known contacts to resolve against.
        stored_names: The fallback names, indexed by stored node id (a hex key prefix).

    Returns:
        A callable that takes a hop label. It returns a contact name when the hash
        matches a known contact, else the label with no change (``None`` = our device,
        which passes through with no change).
    """
    # (name, public_key, key_prefix), all in lower case, for prefix matches that ignore
    # case.
    entries: list[tuple[str, str, str]] = []
    for c in contacts or []:
        pub = (c.public_key or "").lower().removeprefix("0x")
        prefix = (c.key_prefix or "").lower().removeprefix("0x")
        if c.name and (pub or prefix):
            entries.append((c.name, pub, prefix))
    for node, name in (stored_names or {}).items():
        ident = node.lower().removeprefix("0x")
        if name and ident:
            entries.append((name, "", ident))

    # Cached for each label: the render paths ask for the same few hop hashes at each
    # paint (for each hop, each row, and each frame). ``entries`` does not change during
    # the life of this closure. Thus the resolver scans each different label exactly one
    # time.
    memo: dict[str, str | None] = {}

    def resolve(label: str | None) -> str | None:
        if not label:
            return label
        if label in memo:
            return memo[label]
        needle = label.lower().removeprefix("0x")
        result = label
        for name, pub, prefix in entries:
            # The hash is a prefix of the key of the node. Match it against the full
            # public key, or against the stored prefix (which can be shorter) in either
            # direction.
            if pub and pub.startswith(needle):
                result = name
                break
            if prefix and (prefix.startswith(needle) or needle.startswith(prefix)):
                result = name
                break
        memo[label] = result
        return result

    return resolve


def make_node_type_resolver(
    contacts: list[Contact] | None,
) -> Callable[[str | None], int | None]:
    """Build a resolver that gives the *node type* of a hop from its hash.

    This is the type equivalent of :func:`make_node_resolver`. It changes a trace or relay
    hash into the advertised node type of the contact (a ``NODE_TYPE_*`` constant). Thus a
    route graph can mark a repeater with its own glyph, instead of a generic dot. It
    matches the hash against the key of each contact as the name resolver does: as a
    prefix, in either direction. A hash that we cannot identify, or a contact with no type,
    resolves to ``None``.

    Args:
        contacts: The known contacts to resolve against.

    Returns:
        A callable that takes a hop hash and returns its node type, or ``None`` when it is
        unknown.
    """
    entries: list[tuple[str, int]] = []
    for c in contacts or []:
        ident = (c.public_key or c.key_prefix or "").lower().removeprefix("0x")
        if ident and c.node_type is not None:
            entries.append((ident, c.node_type))

    memo: dict[str, int | None] = {}  # cached for each label, as in make_node_resolver

    def type_of(label: str | None) -> int | None:
        if not label:
            return None
        if label in memo:
            return memo[label]
        needle = label.lower().removeprefix("0x")
        result = None
        for ident, node_type in entries:
            if ident.startswith(needle) or needle.startswith(ident):
                result = node_type
                break
        memo[label] = result
        return result

    return type_of


def make_key_resolver(contacts: list[Contact] | None) -> NodeResolver:
    """Build a resolver that expands the stored key prefix of a node to its full public key.

    The observations of the recorder identify a node only by a short key prefix (the part
    that was heard on the air). But a contact on the device has the full public key of the
    node. This resolver changes the stored prefix back into the full key. Thus a screen can
    show as much of the key as fits, and does not stop at the twelve stored hex digits. A
    prefix that does not start the key of any contact resolves to itself, and the stored
    prefix stays.

    Args:
        contacts: The known contacts to resolve against.

    Returns:
        A callable that takes a stored key prefix. It returns the full public key of the
        contact whose key starts with that prefix, or the prefix with no change when no
        key matches.
    """
    keys = [pub for c in contacts or [] if (pub := (c.public_key or "").lower().removeprefix("0x"))]

    memo: dict[str, str] = {}  # cached for each label, as in make_node_resolver

    def resolve(label: str | None) -> str | None:
        if not label:
            return label
        if label in memo:
            return memo[label]
        needle = label.lower().removeprefix("0x")
        result = next((pub for pub in keys if pub.startswith(needle)), label)
        memo[label] = result
        return result

    return resolve


def make_name_key_resolver(
    contacts: list[Contact] | None,
    stored_names: dict[str, str] | None = None,
) -> NameKeyResolver:
    """Build a resolver that finds the key of a display name.

    The app-wide colour rule takes the hue of each name from the key of the node. Some
    places in the UI (the inline sender of a channel message, an ``@mention``, the origin
    of a route graph) have only a *name*. This resolver changes that name (casefolded)
    back into a key. The contacts of the device win (their keys are canonical). Then the
    stored names of the recorder add the nodes that the companion never added as
    contacts. Thus a known node that is not a contact still gets its own hue. A name that
    no node has resolves to ``None``. The caller renders it muted, because colour is only
    for identities with a key.

    Args:
        contacts: The known contacts to resolve against (name → public key/key prefix).
        stored_names: The latest advertised name of each stored node id in the recorder
            (:meth:`~meshterm.persistence.repository.Repository.node_names`). This
            function inverts it into a fallback from name → node id.

    Returns:
        A callable that takes a display name and returns the best key hex that we have
        for it, or ``None`` when the name matches no known node.
    """
    keys: dict[str, str] = {}
    for node, name in (stored_names or {}).items():
        ident = node.lower().removeprefix("0x")
        if name and ident:
            keys.setdefault(name.casefold(), ident)
    for c in contacts or []:
        ident = (c.public_key or c.key_prefix or "").lower().removeprefix("0x")
        if c.name and ident:
            keys[c.name.casefold()] = ident  # contacts win over stored names

    def key_of(name: str) -> str | None:
        if not name:
            return None
        return keys.get(name.casefold())

    return key_of


def parse_trace_path(spec: str, contacts: list[Contact] | None = None) -> str:
    """Parse a path spec from the user into the hex path string that ``send_trace`` expects.

    The MeshCore trace protocol forces a route through a list of repeaters. The address of
    each repeater is the first bytes of its public key (the "path hash"). This follows the
    comma-separated path field in the MeshCore mobile apps, for example ``"3d,f2,3d"``.

    Each comma-separated token can be a contact name (resolved to its key prefix, and the
    case is ignored) or a raw hex key prefix. One spec can have both types. The path-hash
    *width* comes from the hex tokens that you type (each hop uses the same width).
    Contact names are truncated to that width. If the spec has only contact names and no
    hex token, the full key prefix of each name is used.

    Args:
        spec: A comma-separated path, for example ``"3d5f7a,Alice,f2a1b3"``.
        contacts: The known contacts, used to resolve names to key prefixes.

    Returns:
        A comma-separated hex string of prefixes with the same width, for example
        ``"3d5f7a,d4e5f6,f2a1b3"``.

    Raises:
        ValueError: If a token is not a known contact and not valid hex, if the hex
            tokens have different widths, or if the spec has no usable hops.
    """
    by_name = {c.name.casefold(): c.key_prefix for c in (contacts or [])}
    # (hex_text, is_name) for each hop. Names keep their full prefix until the width is
    # known.
    tokens: list[tuple[str, bool]] = []
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        key = token.casefold()
        if key in by_name:
            tokens.append((by_name[key].lower().removeprefix("0x"), True))
        else:
            tokens.append((token.lower().removeprefix("0x"), False))
    if not tokens:
        raise ValueError("path is empty")

    # The explicit hex tokens set the width, and names are truncated to match. With names
    # only, use the (uniform) length of the resolved prefixes.
    hex_widths = {len(text) for text, is_name in tokens if not is_name}
    if len(hex_widths) > 1:
        raise ValueError("all hops must use the same number of hex digits")
    width = hex_widths.pop() if hex_widths else max(len(text) for text, _ in tokens)
    if width == 0 or width % 2:
        raise ValueError("hex key prefixes must have an even number of digits")

    hops: list[str] = []
    for text, is_name in tokens:
        hop = text[:width] if is_name else text
        if len(hop) != width or any(ch not in _HEX_DIGITS for ch in hop):
            raise ValueError(f"{text!r} is not a known contact or a {width // 2}-byte hex prefix")
        hops.append(hop)
    return ",".join(hops)


async def run_traces(
    device: Device,
    target: str,
    *,
    samples: int = 5,
    path: str | None = None,
    cooldown_s: float = 1.0,
    timeout: float | None = None,
    on_result: ProgressCallback | None = None,
    persist: Callable[[TraceResult], Awaitable[None] | None] | None = None,
) -> list[TraceResult]:
    """Run ``samples`` traces to ``target``, with pacing between transmissions.

    Args:
        device: The connected device through which to trace.
        target: The name or key prefix of the destination node.
        samples: The number of traces to run.
        path: An optional explicit path to force on each trace.
        cooldown_s: The delay between traces, to obey the duty cycle of the radio.
        timeout: The reply timeout for each trace, in seconds. With ``None`` (the
            default), each trace sets its own wait for the route through
            :func:`trace_timeout`.
        on_result: An optional callback, called as ``(completed, total, result)`` after
            each trace, for example to move a progress bar.
        persist: An optional callback to store each trace (sync or async).

    Returns:
        The list of the :class:`TraceResult` objects, in order.
    """
    results: list[TraceResult] = []
    for i in range(samples):
        result = await device.run_trace(target, path=path, timeout=timeout)
        results.append(result)

        if persist is not None:
            maybe = persist(result)
            if asyncio.iscoroutine(maybe):
                await maybe
        if on_result is not None:
            on_result(i + 1, samples, result)

        if i < samples - 1 and cooldown_s > 0:
            await asyncio.sleep(cooldown_s)
    return results
