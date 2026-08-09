#!/usr/bin/env python3
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

"""Every line in `~/.pogo/bridget.log` carries an ISO-8601 UTC date (mg-35b1).

The defect this guards is a log that cannot return a negative:

    grep -cE "2026-08-0[789]" ~/.pogo/bridget.log   ->  0

on a file whose mtime was that same afternoon. The zero meant "this format has
no dates" and read as "nothing happened", and no reader could tell which.

So the load-bearing assertion here is not "a stamp appears somewhere". It is
that a *date grep over the real script's real output* returns non-zero — with
the pre-fix build reconstructed alongside it as a positive control, so this
test can actually fail. Everything else is the mechanics that make that true:
per-line rather than per-write stamping, no partial line withheld, the wrapper
stamping at write time rather than flush time, and the supervisor's own lines
sharing the one anchored pattern.

Runs under system python3; the end-to-end cases never get as far as importing
discord, because a missing config file kills bridget first — which is itself
the startup failure you most want dated.
"""
import datetime
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'
SUPERVISE = REPO / 'bridget-supervise'
sys.path.insert(0, str(REPO))

from bridget_core.logstamp import (  # noqa: E402
    LineTimestamper,
    install_line_timestamps,
    utc_stamp,
)

#: The fleet's prefix, anchored at the start of the line. This is the pattern
#: the whole point of the change is to make work — if it has to be loosened to
#: keep this suite green, the change stopped being worth making.
STAMP_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] ')

INSTALL_CALL = 'install_line_timestamps()'


def fixed_clock(*stamps):
    """A clock that yields the given datetimes in order, then repeats the last."""
    remaining = list(stamps)

    def clock():
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return clock


AT = datetime.datetime(2026, 8, 9, 9, 58, 6, tzinfo=datetime.timezone.utc)


class StampFormatTest(unittest.TestCase):
    """The format is copied from the pogo-reminders pollers on purpose: the
    fleet's logs have to stay greppable by one pattern, and bridget was the
    blind spot."""

    def test_matches_the_fleet_prefix_exactly(self):
        self.assertEqual(utc_stamp(AT), '2026-08-09T09:58:06Z')

    def test_a_non_utc_input_is_converted_not_relabelled(self):
        """A `Z` suffix on a local time would be a lie that greps clean."""
        tokyo = datetime.timezone(datetime.timedelta(hours=9))
        self.assertEqual(utc_stamp(AT.astimezone(tokyo)), '2026-08-09T09:58:06Z')

    def test_the_live_clock_is_utc_and_second_precision(self):
        now = utc_stamp()
        self.assertRegex(now, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        parsed = datetime.datetime.strptime(now, '%Y-%m-%dT%H:%M:%SZ').replace(
            tzinfo=datetime.timezone.utc)
        delta = abs((datetime.datetime.now(datetime.timezone.utc) - parsed)
                    .total_seconds())
        self.assertLess(delta, 5, 'the stamp must be UTC, not local time')


class LineTimestamperTest(unittest.TestCase):
    def stamped(self, *writes, clock=None):
        buf = io.StringIO()
        stream = LineTimestamper(buf, clock=clock or fixed_clock(AT))
        for w in writes:
            stream.write(w)
        return buf.getvalue()

    def test_a_line_is_prefixed(self):
        self.assertEqual(self.stamped('logged in as pogo-bridge\n'),
                         '[2026-08-09T09:58:06Z] logged in as pogo-bridge\n')

    def test_every_line_of_a_multi_line_write_is_dated(self):
        """Including indented continuations. A continuation line nobody can
        date is the original defect at smaller radius: `die()` writes its
        remedy on the lines *after* the first one."""
        out = self.stamped('bridget: config not found\n  copy the example\n')
        self.assertEqual(out.splitlines(), [
            '[2026-08-09T09:58:06Z] bridget: config not found',
            '[2026-08-09T09:58:06Z]   copy the example',
        ])

    def test_a_line_split_across_writes_is_stamped_once(self):
        """`print('a', end='')` then `print('b')` is one line, one event."""
        self.assertEqual(self.stamped('spawned ', '3 watchers\n'),
                         '[2026-08-09T09:58:06Z] spawned 3 watchers\n')

    def test_a_partial_line_is_not_withheld(self):
        """No buffer of our own: a process that dies mid-line still leaves the
        half it emitted in the log, prefix and all."""
        self.assertEqual(self.stamped('starting'), '[2026-08-09T09:58:06Z] starting')

    def test_a_blank_line_stays_blank(self):
        """A line with no content is not an event, and stamping it would emit a
        line of trailing whitespace."""
        self.assertEqual(self.stamped('a\n\nb\n').splitlines()[1], '')

    def test_the_stamp_is_taken_when_written_not_when_flushed(self):
        """stdout is block-buffered onto a file, so a flush can land long after
        the event. Two writes at different times get different stamps even
        though nothing flushed in between."""
        later = AT + datetime.timedelta(seconds=41)
        out = self.stamped('first\n', 'second\n', clock=fixed_clock(AT, later))
        self.assertEqual(out.splitlines(), [
            '[2026-08-09T09:58:06Z] first',
            '[2026-08-09T09:58:47Z] second',
        ])

    def test_write_reports_the_characters_it_was_given(self):
        """The io contract. A caller must not learn about the prefix from the
        return value — `TextIOWrapper.write` promises characters written."""
        stream = LineTimestamper(io.StringIO(), clock=fixed_clock(AT))
        self.assertEqual(stream.write('hello\n'), len('hello\n'))
        self.assertEqual(stream.write(''), 0)

    def test_writelines_is_stamped_too(self):
        buf = io.StringIO()
        LineTimestamper(buf, clock=fixed_clock(AT)).writelines(['a\n', 'b\n'])
        self.assertEqual(len(buf.getvalue().splitlines()), 2)
        for line in buf.getvalue().splitlines():
            self.assertRegex(line, STAMP_RE)

    def test_everything_else_delegates_to_the_wrapped_stream(self):
        """It stands in for whatever launchd or a terminal handed us, so an
        attribute it does not implement must reach the real stream."""
        buf = io.StringIO()
        stream = LineTimestamper(buf, clock=fixed_clock(AT))
        self.assertFalse(stream.isatty())
        stream.write('x\n')
        stream.flush()
        self.assertTrue(buf.getvalue())

    def test_concurrent_writers_never_split_a_prefix_from_its_line(self):
        """bridget runs `mg` in a thread-pool executor (mg-e5b8), so two
        threads can reach the stream at once. Without the lock a prefix can be
        emitted for one line and land in the middle of another."""
        buf = io.StringIO()
        stream = LineTimestamper(buf, clock=fixed_clock(AT))
        barrier = threading.Barrier(4)

        def hammer(tag):
            barrier.wait()
            for _ in range(200):
                stream.write(f'{tag}-line\n')

        threads = [threading.Thread(target=hammer, args=(t,)) for t in 'abcd']
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 800)
        for line in lines:
            self.assertRegex(line, r'^\[2026-08-09T09:58:06Z\] [abcd]-line$')


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.saved = (sys.stdout, sys.stderr)
        self.addCleanup(self.restore)

    def restore(self):
        sys.stdout, sys.stderr = self.saved

    def test_it_wraps_both_streams(self):
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        install_line_timestamps(clock=fixed_clock(AT))
        print('out')
        print('err', file=sys.stderr)
        self.assertRegex(sys.stdout._stream.getvalue(), STAMP_RE)
        self.assertRegex(sys.stderr._stream.getvalue(), STAMP_RE)

    def test_installing_twice_does_not_nest_prefixes(self):
        """Idempotent, so an accidental second call cannot produce
        `[stamp] [stamp] msg` — which greps fine and reads as corruption."""
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        first = install_line_timestamps(clock=fixed_clock(AT))
        second = install_line_timestamps(clock=fixed_clock(AT))
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [], 'an already-wrapped stream is left alone')
        print('out')
        self.assertEqual(sys.stdout._stream.getvalue(),
                         '[2026-08-09T09:58:06Z] out\n')


def nostamp_build(tmp: Path) -> Path:
    """The pre-fix bridget, reconstructed: the real script with its one install
    call removed, importing the real `bridget_core` from alongside it.

    This is the positive control. Without it, `test_a_date_grep_finds_the
    _startup` proves only that a regex matches something — not that it would
    have failed before, which is the entire claim.
    """
    src = SCRIPT.read_text()
    assert src.count(INSTALL_CALL) == 1, 'the install call moved; fix this control'
    (tmp / 'bridget_core').symlink_to(REPO / 'bridget_core')
    prefix_free = tmp / 'bridget-prefix-free'
    prefix_free.write_text(src.replace(INSTALL_CALL, 'pass'))
    return prefix_free


def run_startup(script: Path, home: Path) -> subprocess.CompletedProcess:
    """Start bridget with no config. It dies in `load_config()` — before the
    `import discord` line, so this needs no venv and no token — and the death
    is exactly the kind of startup event an undated log loses."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('BRIDGET_')}
    env['HOME'] = str(home)
    return subprocess.run([sys.executable, str(script)],
                          env=env, capture_output=True, text=True, timeout=60)


class EndToEndTest(unittest.TestCase):
    """The real script, in a real process, writing to a real pipe. A wrapper
    that is perfect but never installed would pass every test above."""

    def test_a_date_grep_finds_the_startup_and_used_not_to(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / 'home'
            (home / '.pogo').mkdir(parents=True)
            today = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')

            fixed = run_startup(SCRIPT, home)
            self.assertEqual(fixed.returncode, 1, fixed.stderr)
            self.assertIn('config file not found', fixed.stderr)
            hits = [ln for ln in fixed.stderr.splitlines() if today in ln]
            self.assertTrue(hits, f'no line carries {today}: {fixed.stderr!r}')

            before = run_startup(nostamp_build(tmp), home)
            self.assertIn('config file not found', before.stderr)
            self.assertEqual(
                [ln for ln in before.stderr.splitlines() if today in ln], [],
                'positive control: the pre-fix build must return the zero that '
                'started this ticket, or this test cannot fail')

    def test_no_line_of_a_multi_line_startup_failure_escapes(self):
        """`die()` prints four lines. Dating only the first would leave the
        remedy — the part a reader actually needs — undateable."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / '.pogo').mkdir(parents=True)
            r = run_startup(SCRIPT, home)
            lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
            self.assertGreater(len(lines), 1, 'fixture sanity: die() is multi-line')
            for line in lines:
                self.assertRegex(line, STAMP_RE)


class SuperviseTest(unittest.TestCase):
    """bridget-supervise writes into the same file. Its lines already carried a
    date, but as `[supervise <stamp>]` — which no `^\\[<date>` pattern matches,
    so half the file stayed outside the one grep that is supposed to work."""

    def test_supervisor_lines_share_the_anchored_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith('BRIDGET_')}
            env.update(HOME=str(tmp),
                       BRIDGET_BIN=str(tmp / 'nope'),
                       BRIDGET_ALERT_CMD='/usr/bin/true',
                       BRIDGET_ALERT_STAMP=str(tmp / 'alert.stamp'))
            r = subprocess.run(['bash', str(SUPERVISE)],
                               env=env, capture_output=True, text=True, timeout=30)
            lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
            self.assertTrue(lines, 'fixture sanity: the supervisor logged something')
            for line in lines:
                self.assertRegex(line, STAMP_RE)
            self.assertIn('supervise:', lines[0],
                          'the word stays — a supervisor line is worth '
                          'recognising at a glance')


if __name__ == '__main__':
    unittest.main(verbosity=2)
