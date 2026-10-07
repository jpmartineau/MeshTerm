# SPDX-License-Identifier: Apache-2.0
"""The exit statuses that ``meshterm`` returns, and the meaning of each status.

The return value of a scripted run is its report. The CLI tells what happened in plain
text on stdout, and it tells if the run was successful in ``$?``. The codes have classes,
instead of only 0 and 1. Thus a caller can identify each type of failure, and it does not
have to read the text. For example, it is good to try again after a timeout, but never
after a bad key.

The set is small and closed. A result that does not agree with one of the specific
causes below is :data:`FAILURE`. No part of MeshTerm makes a new code of its own.

+------+--------------+--------------------------------------------------------+
| Code | Name         | Meaning                                                |
+======+==============+========================================================+
| 0    | ``OK``       | The command did what it was asked to do.               |
+------+--------------+--------------------------------------------------------+
| 1    | ``FAILURE``  | It failed for a reason that has no more specific code: |
|      |              | a bad config or preference value, a file that cannot   |
|      |              | be read, or an unhandled fault.                        |
+------+--------------+--------------------------------------------------------+
| 2    | ``USAGE``    | The command line itself was wrong: an unknown flag, a  |
|      |              | missing argument, or a value that the parser refused.  |
|      |              | Click returns this code itself. It is in this table so |
|      |              | that the table is complete.                            |
+------+--------------+--------------------------------------------------------+
| 3    | ``NO_DEVICE``| No companion could be selected: none is connected,     |
|      |              | none matches ``--port``/``--ble``/``--tcp``, or the    |
|      |              | choice was ambiguous. Nothing was transmitted.         |
+------+--------------+--------------------------------------------------------+
| 4    | ``DEVICE``   | A device was reached, but the operation failed: a      |
|      |              | command error, a timeout, or a link that was lost      |
|      |              | during the run. It is reasonable to try again.         |
+------+--------------+--------------------------------------------------------+
| 5    | ``NO_RESULT``| The command ran and completed, and there was nothing   |
|      |              | to report: an empty list, a target that never replied, |
|      |              | or a conversation with no messages. stdout is empty.   |
|      |              | This result is not an error.                           |
+------+--------------+--------------------------------------------------------+

:data:`NO_RESULT` is the code that is most important to explain. A script wants this
code most, and few tools give it. Most tools exit with 0 when they find nothing, the same
as when they find something. A caller of such a tool must count the output lines to know
an empty mesh from a full mesh, thus it must parse the output. ``grep`` is the usual
exception: it exits with 1 when it finds no line, and with 2 on an error. In MeshTerm,
1 is already :data:`FAILURE`, so "nothing to report" has a code of its own. With this
code, the caller can make a branch on the status instead.
"""

from __future__ import annotations

#: The command did what it was asked to do.
OK = 0

#: A failure with no more specific code: a bad value, a file that cannot be read, a fault.
FAILURE = 1

#: The command line was wrong. Click returns this code itself. It is here so that the set
#: is complete.
USAGE = 2

#: No companion device could be selected. Nothing was transmitted.
NO_DEVICE = 3

#: A device was reached, but the operation failed. It is reasonable to try again.
DEVICE = 4

#: The command completed with nothing to report. This result is not an error. stdout is
#: empty.
NO_RESULT = 5

#: All the codes, by name, for the ``--help`` epilog and the documentation.
MEANINGS: dict[int, str] = {
    OK: "success",
    FAILURE: "failure",
    USAGE: "usage error",
    NO_DEVICE: "no device found, or the selection was ambiguous",
    DEVICE: "the device was reached but the operation failed",
    NO_RESULT: "nothing to report (empty result)",
}
