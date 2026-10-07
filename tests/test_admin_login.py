# SPDX-License-Identifier: Apache-2.0
"""A repeater that says nothing has not said that the password is wrong.

The bug: a user administered a repeater that was down, and MeshTerm forgot the admin
password of that repeater. The login returned ``False``. Each caller read ``False`` as
"wrong password", and MeshTerm deleted the credential for a node that never answered.

Two faults caused this. First, the ``meshcore`` library function ``send_login_sync`` waits
for ``LOGIN_SUCCESS`` and for nothing else. The firmware does send a refusal (a
``LOGIN_FAILED`` frame), but the function times out on it in the same way as on silence,
and returns the same ``None``. Second, the credential policy was in five places, at the
call sites.

Now the device listens for the refusal frame itself and returns a three-way
:class:`~meshterm.core.models.LoginResult`. One method,
:meth:`~meshterm.core.admin_store.AdminStore.record`, decides what the result means for the
stored password. It remembers the password on accepted, forgets it on refused, and does
not change it on silence.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import DeviceCommandError, MeshCoreDevice, MockDevice
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import Contact, LoginResult
from meshterm.persistence.repository import Repository

_NODE = Contact(name="Yagi-Repeater", public_key="a1b2c3d4" * 8, key_prefix="a1b2c3d4")


# --- what the three outcomes mean -----------------------------------------------------


def test_only_an_accepted_login_is_truthy() -> None:
    """``if not await device.admin_login(...)`` keeps the meaning "we are not logged in".

    The three-way result replaced a bool. A call site that only asks "am I logged in?" reads
    the result in the same way as before. A call site that acts on the failure must name
    the failure that it acts on.
    """
    assert LoginResult.ACCEPTED
    assert not LoginResult.REFUSED
    assert not LoginResult.NO_REPLY


def test_the_two_failures_are_distinguishable() -> None:
    """The two failures were the same ``False``. The fix is that a caller can tell them apart."""
    assert LoginResult.REFUSED is not LoginResult.NO_REPLY


# --- the credential policy, in the only place that owns it ----------------------------


@pytest.fixture()
def store(tmp_path: Path) -> AdminStore:
    """An admin store with a password already remembered for the node."""
    store = AdminStore(tmp_path / "admin.json")
    store.remember(_NODE, "hunter2")
    return store


def test_silence_leaves_the_remembered_password_exactly_where_it_was(store) -> None:  # noqa: ANN001
    """The main regression. The node was down, so it never gave a verdict on the password."""
    store.record(_NODE, "hunter2", LoginResult.NO_REPLY)

    assert store.get(_NODE) == "hunter2"


def test_a_refusal_clears_the_password_because_the_node_said_so(store) -> None:  # noqa: ANN001
    """Only a node that answered "no" can say that the password is wrong."""
    store.record(_NODE, "hunter2", LoginResult.REFUSED)

    assert store.get(_NODE) is None


def test_a_successful_login_remembers_the_password_that_worked(store) -> None:  # noqa: ANN001
    """This includes a password that the user just typed. That is how MeshTerm stores one."""
    store.forget(_NODE)

    store.record(_NODE, "correct-horse", LoginResult.ACCEPTED)

    assert store.get(_NODE) == "correct-horse"


def test_silence_does_not_invent_a_password_either(tmp_path: Path) -> None:
    """A no-reply from a node with no stored password leaves no stored password."""
    store = AdminStore(tmp_path / "admin.json")

    store.record(_NODE, "typed-once", LoginResult.NO_REPLY)

    assert store.get(_NODE) is None


def test_a_node_that_goes_quiet_after_working_keeps_its_password(store) -> None:  # noqa: ANN001
    """A real sequence: the login worked yesterday, the node is down today, it works tomorrow."""
    store.record(_NODE, "hunter2", LoginResult.ACCEPTED)
    store.record(_NODE, "hunter2", LoginResult.NO_REPLY)
    store.record(_NODE, "hunter2", LoginResult.NO_REPLY)

    assert store.get(_NODE) == "hunter2"


# --- reading the wire: which listeners are active, and for how long -------------------


class _Subscription:
    """A replacement for the Subscription handle of meshcore, with its unsubscribe method."""

    def __init__(self, mc, event_type, callback) -> None:  # noqa: ANN001
        self.mc = mc
        self.event_type = event_type
        self.callback = callback

    def unsubscribe(self) -> None:
        if self in self.mc.subscriptions:
            self.mc.subscriptions.remove(self)


class _Event:
    def __init__(self, type_, payload=None) -> None:  # noqa: ANN001
        self.type = type_
        self.payload = payload or {}


class _FakeCommands:
    """``send_login_sync`` with the behaviour of the real library.

    It waits for LOGIN_SUCCESS and for nothing else. It returns a success that it heard,
    and ``None`` for all other results, a refusal included. The ``late`` argument models
    the case that broke a healthy node: the answer arrives after the function gave up.
    Only a listener that lives longer than the function can catch it.
    """

    def __init__(  # noqa: ANN003
        self, mc, *, answer=None, late=None, late_delay: float = 0.0, send_delay: float = 0.0
    ) -> None:  # noqa: ANN001
        self._mc = mc
        self._answer = answer
        self._late = late
        self._late_delay = late_delay
        # How long the send takes to return. The time is the mesh-request lock of the
        # library, then a MSG_SENT that it matches by event type only. None of it is the
        # time of the node.
        self._send_delay = send_delay

    async def send_login_sync(self, pubkey, password):  # noqa: ANN001
        from meshcore import EventType

        self._mc.sent.append((pubkey, password))
        self._mc.armed = [sub.event_type for sub in self._mc.subscriptions]
        await asyncio.sleep(self._send_delay)
        if self._answer is not None:
            self._mc.dispatch(self._answer)
            if self._answer.type is EventType.LOGIN_SUCCESS:
                return self._answer
            return None  # the function never waits for a refusal
        if self._late is not None:
            asyncio.get_running_loop().call_later(self._late_delay, self._mc.dispatch, self._late)
        return None


class _FakeMeshCore:
    def __init__(self, **commands) -> None:  # noqa: ANN003
        self.subscriptions: list[_Subscription] = []
        self.sent: list[tuple] = []
        self.armed: list = []  # the event types that were subscribed when the send went out
        self.commands = _FakeCommands(self, **commands)

    def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001
        sub = _Subscription(self, event_type, callback)
        self.subscriptions.append(sub)
        return sub

    def dispatch(self, event) -> None:  # noqa: ANN001
        for sub in list(self.subscriptions):
            if sub.event_type is event.type:
                sub.callback(event)


@pytest.fixture()
def quick_budget(monkeypatch):  # noqa: ANN001
    """Make the reply budget short, so that a no-reply test does not wait ten seconds."""
    # The patch is where the device reads the budget. The budget calculation is in
    # ``core.tracing``, and ``core.connection`` binds the name at import. Thus that
    # binding is the one that the device uses.
    import meshterm.core.connection as connection

    monkeypatch.setattr(connection, "trace_timeout", lambda hops: 0.05)


def _device(mc) -> MeshCoreDevice:  # noqa: ANN001
    device = MeshCoreDevice(port="mock")
    device._mc = mc
    return device


def _login_success(prefix: str | None = "a1b2c3d4a1b2"):
    from meshcore import EventType

    return _Event(EventType.LOGIN_SUCCESS, {"pubkey_prefix": prefix} if prefix else {})


def _login_failed(prefix: str | None = "a1b2c3d4a1b2"):
    from meshcore import EventType

    return _Event(EventType.LOGIN_FAILED, {"pubkey_prefix": prefix} if prefix else {})


def test_a_login_the_node_accepts_reads_as_accepted(quick_budget) -> None:  # noqa: ANN001
    """The normal case: the session is open."""
    mc = _FakeMeshCore(answer=_login_success())

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.ACCEPTED
    assert mc.sent == [(_NODE.public_key, "hunter2")]


def test_an_answer_that_lands_after_the_library_gave_up_is_still_the_answer() -> None:
    """The reported regression: a healthy node, the right password, a no-reply dialog.

    ``send_login_sync`` does not start to listen until its own send returns. That send
    waits for a MSG_SENT that the library matches by event type only. Thus a scheduled
    advert or the courier can use our MSG_SENT, and the send stalls. Each answer that
    arrives during the stall goes to no listener and is lost. The device owns the wait and
    arms it before the send. Thus a login that the repeater accepted reads as accepted.
    """
    mc = _FakeMeshCore(late=_login_success(), late_delay=0.05)

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.ACCEPTED


def test_a_late_refusal_is_still_a_refusal() -> None:
    """The same window, the other verdict. This verdict must also clear the password."""
    mc = _FakeMeshCore(late=_login_failed(), late_delay=0.05)

    assert asyncio.run(_device(mc).admin_login(_NODE, "wrong")) is LoginResult.REFUSED


def test_a_refusal_frame_reads_as_refused(quick_budget) -> None:  # noqa: ANN001
    """The wait of the library times out on a refusal, so the app must hear the frame itself.

    Without this, a wrong password would report no-reply and MeshTerm would keep it for
    ever. This is the opposite of the reported bug. It is also the reason that the code does
    not assume a refusal.
    """
    mc = _FakeMeshCore(answer=_login_failed())

    assert asyncio.run(_device(mc).admin_login(_NODE, "wrong")) is LoginResult.REFUSED


def test_silence_reads_as_no_reply(quick_budget) -> None:  # noqa: ANN001
    """The case that started this work: nothing came back, so nothing is known."""
    mc = _FakeMeshCore()

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.NO_REPLY


def test_a_terse_answer_with_no_key_prefix_still_counts(quick_budget) -> None:  # noqa: ANN001
    """The firmware adds the key prefix of the answering node only when the frame has one.

    If the filter demanded a key prefix, each answer from terse firmware would become a
    no-reply. This path exists to stop that failure, so the filter must not cause it again.
    """
    mc = _FakeMeshCore(answer=_login_failed(prefix=None))

    assert asyncio.run(_device(mc).admin_login(_NODE, "wrong")) is LoginResult.REFUSED


def test_an_answer_meant_for_a_different_node_is_not_ours(quick_budget) -> None:  # noqa: ANN001
    """Two admin flows can overlap. The rejection from another node must not clear our password."""
    mc = _FakeMeshCore(answer=_login_failed("ffeeddccbbaa"))

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.NO_REPLY


def test_the_listener_is_armed_before_the_request_goes_out(quick_budget) -> None:  # noqa: ANN001
    """Both answers are subscribed before the send, so an answer during the send has a listener."""
    from meshcore import EventType

    mc = _FakeMeshCore(answer=_login_success())

    asyncio.run(_device(mc).admin_login(_NODE, "hunter2"))

    assert EventType.LOGIN_SUCCESS in mc.armed
    assert EventType.LOGIN_FAILED in mc.armed


def test_a_companion_error_is_watched_for_because_the_library_erases_it() -> None:
    """``send_login_sync`` changes its own ERROR to a bare ``None``, so the reason is lost.

    A companion that refuses to send and a node that never answers both give
    :attr:`LoginResult.NO_REPLY`. This is correct, because neither says anything about the
    password. But the two faults are different, and a user repairs them in different ways.
    Only the frame shows which fault it is. The frame has no correlation to the login, so
    the device logs it and never reads it as a verdict.
    """
    from meshcore import EventType

    mc = _FakeMeshCore(answer=_login_success())

    asyncio.run(_device(mc).admin_login(_NODE, "hunter2"))

    assert EventType.ERROR in mc.armed


def test_the_node_gets_its_whole_budget_even_when_the_send_was_slow(quick_budget) -> None:  # noqa: ANN001
    """The regression that this fix is for: the node paid for the queue before it.

    The budget is the time for the answer of the node. But the count once started before
    ``send_login_sync``. That function waits for the mesh-request lock of the library
    before it transmits. Each telemetry poll and each courier retry holds the lock for a
    whole round trip. Thus a send that took longer than the budget left the node no time to
    answer. A repeater that was up, listening, and ready to answer was recorded as silent.
    """
    mc = _FakeMeshCore(late=_login_success(), late_delay=0.01, send_delay=0.2)

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.ACCEPTED


def test_the_listeners_are_released_even_when_the_send_blows_up() -> None:
    """Each attempt makes one pair of subscriptions. Leaked pairs would collect in a session."""

    class _Exploding(_FakeCommands):
        async def send_login_sync(self, pubkey, password):  # noqa: ANN001
            raise RuntimeError("the companion dropped the link")

    mc = _FakeMeshCore()
    mc.commands = _Exploding(mc)

    with pytest.raises(RuntimeError):
        asyncio.run(_device(mc).admin_login(_NODE, "hunter2"))
    assert mc.subscriptions == []


def test_a_companion_side_error_is_silence_not_a_denial(quick_budget) -> None:  # noqa: ANN001
    """The request never left the radio, so the node cannot have rejected it."""
    from meshcore import EventType

    mc = _FakeMeshCore()

    async def _errored(pubkey, password):  # noqa: ANN001
        return _Event(EventType.ERROR, {"reason": "timeout"})

    mc.commands.send_login_sync = _errored

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.NO_REPLY


def test_the_reply_budget_is_sized_to_the_route_the_login_has_to_walk() -> None:
    """A budget of one or two seconds for a neighbour would stop a multi-hop repeater too soon.

    The login goes out along the route of the contact, and the answer comes back along it.
    Thus the wire carries twice the stored one-way hops. ``run_trace`` makes its budget in
    the same way.
    """
    import meshterm.core.connection as connection

    asked: list[int] = []
    real = connection.trace_timeout

    def spy(hops):  # noqa: ANN001
        asked.append(hops)
        return 0.01

    connection.trace_timeout = spy
    try:
        two_hops = Contact(name="Hub-Far", public_key="a1b2c3d4" * 8, route_hops=("3d", "f2"))
        asyncio.run(_device(_FakeMeshCore()).admin_login(two_hops, "hunter2"))
        asyncio.run(_device(_FakeMeshCore()).admin_login(_NODE, "hunter2"))
    finally:
        connection.trace_timeout = real

    # Each login asks twice: for its own walk, and for the floor of a contact with no route
    # (hops ``0``). The budget is never less than the floor.
    assert asked == [4, 0, 0, 0]
    assert real(4) > real(2) > real(1)  # also, the budget grows with the walk


def test_a_known_short_route_never_buys_less_patience_than_no_route_at_all() -> None:
    """A known route to a node must not make MeshTerm stop waiting for it sooner.

    A trace budget is for one small packet on an explicit path. A login is an admin
    exchange, out and back. If the code used the trace budget as it is, a contact with a
    one-hop route would get a shorter window than a contact with no route, which floods.
    Thus the flood budget is the floor, and the route can only make the budget longer.
    """
    import meshterm.core.connection as connection
    import meshterm.core.tracing as tracing

    budgets: list[float] = []
    real = connection.trace_timeout

    def spy(hops):  # noqa: ANN001
        # The real shape, made smaller so that the no-reply that it causes is not a long wait.
        budget = real(hops) / 1000
        budgets.append(budget)
        return budget

    connection.trace_timeout = spy
    try:
        near = Contact(name="Hub-Near", public_key="a1b2c3d4" * 8, route_hops=("3d",))
        asyncio.run(_device(_FakeMeshCore()).admin_login(near, "hunter2"))
    finally:
        connection.trace_timeout = real

    walked, floor = budgets
    assert walked < floor  # the trace budget as it is gives the shorter of the two
    assert real(2) < real(0) == tracing.TRACE_TIMEOUT_FLOOD_S  # the same at full scale


# --- the simulator gives the same three results ---------------------------------------


async def test_the_simulator_can_model_a_node_that_is_simply_down() -> None:
    """``--mock`` must be able to follow the path of a down repeater, or nobody sees the dialog."""
    device = MockDevice(admin_password="secret")
    await device.connect()
    node = (await device.get_contacts())[0]
    device._unreachable.add(node.name)

    assert await device.admin_login(node, "secret") is LoginResult.NO_REPLY
    assert await device.admin_login(node, "wrong") is LoginResult.NO_REPLY  # it is still silence


# --- the callers ----------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path):
    """A context with the simulator as its device and an admin store on disk."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "admin.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


async def _mock_node(ctx, *, down: bool) -> Contact:  # noqa: ANN001
    """The simulated repeater, unreachable if ``down`` is true, with a stored password."""
    device = await ctx.device()
    node = next(c for c in await device.get_contacts() if c.name == "Yagi-Repeater")
    if down:
        device._unreachable.add(node.name)
    ctx.admin_store.remember(node, "admin")
    return node


async def test_the_scripted_tool_keeps_the_password_when_the_node_is_down(ctx) -> None:  # noqa: ANN001
    """``meshterm repeater-admin <node> <cmd>`` for a repeater that is off the air."""
    from meshterm.tools.repeater_admin import RepeaterAdminTool

    node = await _mock_node(ctx, down=True)

    with pytest.raises(DeviceCommandError, match="did not answer"):
        await RepeaterAdminTool().run(ctx, {"node": node.name, "command": "get tx"})

    assert ctx.admin_store.get(node) == "admin"


async def test_the_scripted_tool_clears_the_password_the_node_rejected(ctx) -> None:  # noqa: ANN001
    """The other half: a node that is reachable and says no has a bad password."""
    from meshterm.tools.repeater_admin import RepeaterAdminTool

    node = await _mock_node(ctx, down=False)
    ctx.admin_store.remember(node, "not-the-password")

    with pytest.raises(DeviceCommandError, match="wrong password"):
        await RepeaterAdminTool().run(ctx, {"node": node.name, "command": "get tx"})

    assert ctx.admin_store.get(node) is None


async def test_the_tx_optimizer_keeps_the_password_when_the_node_is_down(ctx) -> None:  # noqa: ANN001
    """The sweep logs in before it tunes. A down node must not cost the credential."""
    from meshterm.tools.tx_optimize import TxOptimizeTool

    node = await _mock_node(ctx, down=True)

    with pytest.raises(DeviceCommandError, match="did not answer"):
        await TxOptimizeTool()._login(ctx, node, {})

    assert ctx.admin_store.get(node) == "admin"


async def test_the_tx_optimizer_clears_the_password_the_node_rejected(ctx) -> None:  # noqa: ANN001
    """The optimizer also forgets a password that the node refused."""
    from meshterm.tools.tx_optimize import TxOptimizeTool

    node = await _mock_node(ctx, down=False)
    ctx.admin_store.remember(node, "not-the-password")

    with pytest.raises(DeviceCommandError, match="wrong password"):
        await TxOptimizeTool()._login(ctx, node, {})

    assert ctx.admin_store.get(node) is None


# --- the interactive flow --------------------------------------------------------------


async def _step_until(predicate, *, limit: int = 200):
    """Yield to the event loop until ``predicate()`` is true (the convention of the TUI tests)."""
    value = predicate()
    for _ in range(limit):
        if value:
            return value
        await asyncio.sleep(0)
        value = predicate()
    return value


async def _run_login(ctx, node):  # noqa: ANN001
    """Run ``ui.repeater_admin._login`` to its end, and read and close its dialog.

    Returns the result of the flow and the words that it showed on the screen. Both are
    important. What happens to the password is one half of the fix. The other half is that
    the flow does not tell the user that a password was wrong when it was not.
    """
    from meshterm.ui.repeater_admin import _login
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.prompt import ButtonDialog
    from meshterm.ui.tui.session import TuiSession

    ctx.ui = TuiUi(TuiSession())
    device = await ctx.device()
    task = asyncio.ensure_future(_login(ctx, device, node))
    try:
        dialog = None
        for _ in range(200):
            top = ctx.ui.session.top
            if isinstance(top, ButtonDialog):
                dialog = top
                break
            if task.done():
                break
            await asyncio.sleep(0)
        shown = ""
        if dialog is not None:
            prompt = dialog._prompt
            shown = prompt.plain if hasattr(prompt, "plain") else str(prompt)
            dialog.resolve("ok")
        return await task, shown
    finally:
        if not task.done():
            task.cancel()


async def test_the_admin_flow_says_no_reply_and_keeps_the_password(ctx) -> None:  # noqa: ANN001
    """The screen that JP was on. It must not report a wrong password, and must not clear it."""
    node = await _mock_node(ctx, down=True)

    ok, shown = await _run_login(ctx, node)

    assert ok is False
    assert "No reply" in shown and "kept" in shown
    assert "wrong password" not in shown
    assert ctx.admin_store.get(node) == "admin"


async def test_the_admin_flow_still_clears_a_password_the_node_rejected(ctx) -> None:  # noqa: ANN001
    """This behaviour was always correct. The test makes sure that it stays correct."""
    node = await _mock_node(ctx, down=False)
    ctx.admin_store.remember(node, "not-the-password")

    ok, shown = await _run_login(ctx, node)

    assert ok is False
    assert "wrong password" in shown and "cleared" in shown
    assert ctx.admin_store.get(node) is None


# --- no call site decides this locally again --------------------------------------------


def test_every_login_caller_goes_through_the_one_credential_policy() -> None:
    """A sixth caller must not make the rule again, as the other five did, with errors.

    The test reads the source on purpose. The failure is not a wrong branch. The failure is
    a call site that never asks the question. Each code path that logs in must record the
    result through the store, and none can call :meth:`AdminStore.forget` to do it.
    """
    import ast

    import meshterm

    def calls_admin_login(tree: ast.AST) -> bool:
        """A real call, not the word in a docstring. This is why the test parses the source."""
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "admin_login"
            for node in ast.walk(tree)
        )

    root = Path(meshterm.__file__).parent
    callers = [
        path
        for path in root.rglob("*.py")
        if calls_admin_login(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert callers, "the scan found no callers at all — it has stopped testing anything"
    for path in callers:
        source = path.read_text(encoding="utf-8")
        assert "admin_store.record(" in source, f"{path.name} logs in without recording"
        assert "admin_store.forget(" not in source, (
            f"{path.name} decides the credential policy itself; use admin_store.record()"
        )
