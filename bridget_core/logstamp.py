# Copyright (C) 2026 Daniel Miller
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Written for this fork of cloverross/bridget; not present upstream.
#
# bridget is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. bridget is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# You should have received a copy of the GNU General Public License along with
# bridget. If not, see <https://www.gnu.org/licenses/>.

# bridget_core.logstamp — an ISO-8601 UTC stamp on every emitted line.
"""Prefix every line bridget writes to stdout/stderr with `[<ISO-8601 UTC>] `.

Until mg-35b1, `~/.pogo/bridget.log` carried no dates at all. That is not a
missing nicety — it is an instrument that cannot return a negative:

    grep -cE "2026-08-0[789]" ~/.pogo/bridget.log   ->  0

Zero on a live file, with watchers actively spawning. The zero meant "this
format has no dates", but it *reads* exactly like "there was no activity in
those three days", and there is nothing in the output to tell the two apart. An
architect trying to establish whether a dormant code path had cost us any
missed Discord replies could establish the exposure and not the cost, and had
to report the gap instead of the answer.

Every other log in this fleet we reason from — pogod.log, deadman.log,
notify.log, pogo-deploy.log — is timestamped, and the pogo-reminders pollers
already emit exactly the `[2026-08-09T09:58:06Z] ` prefix reproduced here. The
consistency is the point, not the format: one anchored pattern (`^\\[<date>`)
has to work across the whole fleet's logs, bridget included.

**Why wrap the stream instead of stamping at the call sites.** bridget emits
its log through ~60 bare `print()` calls scattered across 3000 lines, and
launchd captures the process's raw stdout/stderr into the two log files. Adding
a `log()` helper would date the 60 lines someone remembered to convert and
silently leave everything else — every future `print()`, every uncaught
traceback, every warning a library writes to stderr — undated, which is the
same defect with a smaller radius. Wrapping the stream stamps whatever the
process emits, from whatever depth, forever.

Two deliberate properties:

- **Stamped per line, not per write.** A multi-line message gets a stamp on
  each of its lines, including indented continuations. It is slightly noisier
  to read and it is the only version where a date grep returns whole messages
  rather than their first lines — an undateable continuation line is the
  original defect in miniature.
- **The stamp is taken when the line is written, not when it is flushed.**
  stdout is block-buffered onto a file, so a flush can land seconds or minutes
  after the event; stamping here (rather than by piping the process through a
  `ts`-style filter) is what makes the timestamp the event's time.

A blank line stays blank: a line with no content is not an event, and stamping
it would only produce a line of trailing whitespace.

**What this still does not date**, said out loud so the next reader does not
have to rediscover it: anything written to file descriptor 1 or 2 without going
through `sys.stdout` / `sys.stderr`. In practice that is an interpreter-level
crash dump (a segfault traceback from `faulthandler`), and nothing else —
bridget's three `subprocess.run` call sites all pass `capture_output=True`, so
no child process inherits the log's descriptors, and discord.py's warnings reach
`logging.lastResort`, which resolves `sys.stderr` at emit time and is therefore
stamped like everything else.
"""
from __future__ import annotations

import datetime
import sys
import threading

#: The fleet's line prefix, rendered as `[2026-08-09T09:58:06Z] `. Seconds
#: precision, `Z`, no microseconds — this is what the pogo-reminders pollers
#: emit and what every `grep -E '2026-08-0[789]'` in the fleet is written
#: against. Do not widen it without widening those.
STAMP_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def utc_stamp(now: datetime.datetime | None = None) -> str:
    """`2026-08-09T09:58:06Z` for `now` (default: this instant, UTC).

    An aware datetime in any zone is converted to UTC; a naive one is assumed
    to be local, which is what `astimezone` already does.
    """
    return (now or _utc_now()).astimezone(datetime.timezone.utc).strftime(STAMP_FORMAT)


class LineTimestamper:
    """A write-through text-stream wrapper that stamps the start of each line.

    Not an `io.TextIOBase` subclass on purpose: it has to stand in for whatever
    launchd, a terminal, or a test handed us, so everything it does not
    implement is delegated to the wrapped stream rather than reimplemented
    against a base class that assumes it owns a buffer.

    It holds no buffer of its own. A write that ends mid-line is passed through
    immediately and the *next* write continues that line unstamped — so a
    partial line is never withheld from the log waiting for a newline that a
    crashing process may never emit.
    """

    def __init__(self, stream, clock=None):
        self._stream = stream
        self._clock = clock or _utc_now
        # bridget runs `mg` in a thread-pool executor (mg-e5b8), so two threads
        # can reach this concurrently; the lock keeps a prefix attached to the
        # line it belongs to instead of interleaving into the middle of one.
        self._lock = threading.RLock()
        self._at_line_start = True

    def write(self, text: str) -> int:
        if not text:
            return 0
        segments = text.split('\n')
        last = len(segments) - 1
        out = []
        with self._lock:
            for i, segment in enumerate(segments):
                if segment:
                    if self._at_line_start:
                        out.append(f'[{utc_stamp(self._clock())}] ')
                        self._at_line_start = False
                    out.append(segment)
                if i != last:
                    out.append('\n')
                    self._at_line_start = True
            self._stream.write(''.join(out))
        # The io contract is "characters of `text` written", not bytes emitted:
        # the caller must not learn about the prefix through the return value.
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        # Everything else — isatty, fileno, encoding, close — belongs to the
        # wrapped stream. Read `_stream` out of __dict__ so an instance that
        # never finished __init__ raises AttributeError instead of recursing.
        try:
            stream = self.__dict__['_stream']
        except KeyError:  # pragma: no cover — only during a failed __init__
            raise AttributeError(name) from None
        return getattr(stream, name)


def install_line_timestamps(clock=None) -> list:
    """Wrap `sys.stdout` and `sys.stderr` so every line they carry is stamped.

    Idempotent: an already-wrapped stream is left alone, so importing twice (or
    re-execing into the venv, which starts a fresh process anyway) cannot nest
    prefixes. Returns the wrappers it installed, newly-wrapped only.
    """
    installed = []
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        if stream is None or isinstance(stream, LineTimestamper):
            continue
        if not hasattr(stream, 'write'):  # pragma: no cover — pythonw et al.
            continue
        wrapper = LineTimestamper(stream, clock=clock)
        setattr(sys, name, wrapper)
        installed.append(wrapper)
    return installed
