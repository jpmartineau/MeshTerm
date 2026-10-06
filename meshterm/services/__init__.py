# SPDX-License-Identifier: Apache-2.0
"""Algorithm layer: pure logic for tracing, TX search, and path exploration.

A service can depend on :mod:`meshterm.core` (the
:class:`~meshterm.core.connection.Device` interface and the domain models), on the types
of the persistence layer, and on the app context. A service must never depend on ``ui/``
or ``tools/``. No code in this package renders. Thus it is easy to do a unit test of each
service against the simulator.
"""
