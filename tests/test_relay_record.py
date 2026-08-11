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

"""Behavioral acceptance for the delivery path's positive record (mg-7c1b).

The defect: `~/.pogo/bridget.log` recorded delivery only when it went wrong, so
a dead relay and an idle one produced a byte-identical zero and nothing in the
file told them apart. Two agents drew conclusions from `grep -c` returning 0
against it on one night — one false ("Discord carried NONE", while bridget had
in fact relayed all 33 alarms within seconds), one true but unjustified ("the
dedup held zero repeats", reported for ~5 cycles with no positive control that
the delivery path was even running).

The ticket's second ask is the point of this file, and it is why these tests
drive the REAL `watch_mailbox` out of the real script rather than a model of it:

    "Whatever is added must be provable: show the line appearing on a real
     relay and absent when the path is stopped. A record nobody has watched
     fail is not known to work."

So every assertion here is paired. The record is shown to appear (D1, D2), and
then shown to be ABSENT in each way the path can stop:

    S1  the loop is not running at all       — the original ambiguity
    S2  the loop turns but every send fails  — the mg-e5b8 wedge shape, the one
                                               a loop-liveness beat is blind to
    S3  the record is switched off           — the PRE-FIX CONTROL: with
                                               BRIDGET_RELAY_HEARTBEAT=0 the
                                               healthy file and the dead file
                                               are byte-identical again, which
                                               is the defect, reproduced

and its volume is bounded in both directions (V1, V2) so the line cannot bury
the exception lines it sits among — Daniel merged a duplication limit into this
repo the same night (mg-5521) because he was drowning in repetition.

`grep -c` is used literally, on a real file, because `grep -c` on a real file is
what both agents got wrong.

Everything runs against a stubbed discord — no live Discord, no live relay.
"""
import asyncio
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'
sys.path.insert(0, str(REPO))

from bridget_core.logstamp import LineTimestamper  # noqa: E402
from bridget_core.relaylog import RelayLedger      # noqa: E402


class _FakeHTTPException(Exception):
    """Stand-in for discord.HTTPException, so the delivery path's
    `except discord.HTTPException` catches our simulated send failures."""


def load_bridget(fake_home: Path, env_overrides: dict | None = None):
    """Import bridget into a fresh namespace rooted at `fake_home`."""
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
        'MG_BIN=/bin/echo\n'
    )

    keys = {'HOME', 'BRIDGET_REPO_DIR'} | set(env_overrides or {})
    saved_env = {k: os.environ.get(k) for k in keys}
    os.environ['HOME'] = str(fake_home)
    os.environ['BRIDGET_REPO_DIR'] = str(REPO)
    for k, v in (env_overrides or {}).items():
        os.environ[k] = v

    fake_discord = mock.MagicMock()
    fake_discord.Intents.default.return_value = mock.MagicMock()
    fake_discord.HTTPException = _FakeHTTPException
    saved_discord = sys.modules.get('discord')
    sys.modules['discord'] = fake_discord
    saved_bridget = sys.modules.pop('bridget', None)

    try:
        loader = SourceFileLoader('bridget', str(SCRIPT))
        spec = importlib.util.spec_from_loader('bridget', loader)
        bridget = importlib.util.module_from_spec(spec)
        loader.exec_module(bridget)
        return bridget
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_discord is not None:
            sys.modules['discord'] = saved_discord
        else:
            sys.modules.pop('discord', None)
        if saved_bridget is not None:
            sys.modules['bridget'] = saved_bridget
        else:
            sys.modules.pop('bridget', None)


def write_mail(mail_dir: Path, name: str, frm: str, subject: str, body: str):
    mail_dir.mkdir(parents=True, exist_ok=True)
    (mail_dir / name).write_text(f"From: {frm}\nSubject: {subject}\n\n{body}\n")


class FakeClock:
    """A hand-wound clock, so a one-hour idle interval costs no wall time.

    The cadence under test is measured in hours; a test that waited for it
    would either be an hour long or would prove a shortened cadence rather than
    the shipped one.
    """

    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class RelayRecordHarness(unittest.TestCase):
    """Shared rig: the real script, a stubbed discord, a wound clock, and a
    real log FILE that the assertions grep."""

    ENV: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bridget-relay-'))
        self.bridget = load_bridget(self.tmp, self.ENV)
        self.clock = FakeClock()
        b = self.bridget
        b.POLL_INTERVAL = 0.001
        b.THREADS_ENABLED = False   # DM is the only surface; simplest real path
        b.run_mg = lambda args: (0, '', '')
        # Re-seat the ledger on the wound clock, keeping the interval the real
        # config produced — the cadence under test is the shipped one.
        b.RELAY = RelayLedger(started=self.clock(),
                              interval=b.CONFIG['relay_heartbeat'],
                              clock=self.clock)
        # The two files launchd actually writes, kept apart here for the same
        # reason the plist keeps them apart: `bridget.log` is StandardOutPath
        # and `bridget.err.log` is StandardErrorPath, so the exception lines a
        # wedge produces do NOT land in the file the ticket is about. That
        # split is precisely why the positive record has to be on stdout — the
        # reader greps bridget.log, and stderr's noise never reaches them.
        self.logfile = self.tmp / 'bridget.log'
        self.errfile = self.tmp / 'bridget.err.log'
        self.logfile.write_text('')
        self.errfile.write_text('')

    def prime_seen_empty(self):
        """Model a running watcher into which fresh mail arrives, rather than a
        cold start that adopts its backlog."""
        b = self.bridget
        b.SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        b.SEEN_FILE.write_text('')

    def run_cycles(self, user, n):
        """Run `watch_mailbox` for exactly n cycles, appending everything it
        prints to the real log file (stamped, as launchd would capture it)."""
        b = self.bridget
        b.client.is_closed = mock.Mock(side_effect=[False] * n + [True])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(LineTimestamper(out)), \
                redirect_stderr(LineTimestamper(err)):
            asyncio.run(b.watch_mailbox(user))
        for buf, path in ((out, self.logfile), (err, self.errfile)):
            with path.open('a') as fh:
                fh.write(buf.getvalue())
        return out.getvalue()

    def grep_c(self, pattern: str, path: Path | None = None) -> int:
        """`grep -c <pattern> <file>` — the exact instrument the ticket is
        about, run for real rather than approximated with `str.count`.

        Defaults to `bridget.log`, which is the file both agents grepped and
        the only one the fix is allowed to be judged on."""
        r = subprocess.run(['grep', '-c', pattern, str(path or self.logfile)],
                           capture_output=True, text=True)
        return int(r.stdout.strip() or 0)

    def ok_user(self):
        user = mock.MagicMock()
        user.send = mock.AsyncMock()
        return user

    def dead_user(self):
        user = mock.MagicMock()
        user.send = mock.AsyncMock(side_effect=_FakeHTTPException('503'))
        return user


class RecordAppearsTest(RelayRecordHarness):
    """D1/D2 — the line appears on a real relay, and on a healthy idle."""

    def test_line_appears_when_mail_is_actually_relayed(self):
        """D1. A real mail through the real deliver_mail produces a `relay:`
        line that counts it."""
        b = self.bridget
        self.prime_seen_empty()
        write_mail(b.MAIL_DIR, '1700000001.aaaa.host',
                   'pm-pogo', 'blackout on host-3', 'the alarm body')

        user = self.ok_user()
        self.run_cycles(user, 1)

        # The mail really went to Discord — not adopted as backlog, not held.
        sent = [str(c.args[0]) for c in user.send.await_args_list if c.args]
        self.assertTrue(any('blackout on host-3' in t for t in sent),
                        'the mail must actually be delivered')
        # ...and the file now says so.
        self.assertEqual(self.grep_c('relay:'), 1)
        self.assertRegex(self.logfile.read_text(),
                         r'relay: 1 delivered in the last \d+s \(1 total since ')

    def test_line_appears_on_a_healthy_idle_with_nothing_to_deliver(self):
        """D2. The load-bearing case, and the one a per-mail line cannot cover:
        no mail at all, and the file still carries a positive. This is what
        makes a later silence mean death rather than a quiet day."""
        self.run_cycles(self.ok_user(), 2)
        self.assertEqual(self.grep_c('relay:'), 1)
        self.assertIn('relay: 0 delivered in the last ', self.logfile.read_text())

    def test_the_line_is_dated_like_every_other_line(self):
        """The record must not have the defect its sibling ticket fixed: an
        undateable positive is a positive you cannot scope to a window."""
        self.run_cycles(self.ok_user(), 1)
        first = self.logfile.read_text().splitlines()[0]
        self.assertRegex(first, r'^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] relay: ')


class RecordAbsentWhenThePathStopsTest(RelayRecordHarness):
    """S1/S2 — the record watched to fail. Absence is the assertion."""

    def test_absent_while_the_loop_is_not_running(self):
        """S1. The original ambiguity, now resolved by the clock: a beat comes
        due and no line appears, because nothing is turning to emit it.

        The pairing is what makes this a proof rather than a tautology — the
        same ledger, the same due beat, emits the moment the loop is restarted.
        """
        self.run_cycles(self.ok_user(), 1)
        self.assertEqual(self.grep_c('relay:'), 1)

        # The relay dies here. Two full idle intervals pass.
        self.clock.advance(2 * self.bridget.CONFIG['relay_heartbeat'])
        self.assertEqual(self.grep_c('relay:'), 1,
                         'a stopped relay must add nothing to the file')

        # Positive control: the beat WAS due the whole time — restart the loop
        # and it lands immediately. So the silence above was the relay's, not
        # the cadence's.
        self.run_cycles(self.ok_user(), 1)
        self.assertEqual(self.grep_c('relay:'), 2)

    def test_absent_while_the_loop_turns_but_every_send_fails(self):
        """S2. The wedge shape from mg-e5b8: mail present, loop iterating,
        Discord unreachable. A loop-liveness beat keeps ticking through this —
        which is why this one is gated on the cycle being delivery-healthy.

        A record that printed here would be worse than no record: it would read
        as a positive across a ~70h outage.
        """
        b = self.bridget
        self.prime_seen_empty()
        write_mail(b.MAIL_DIR, '1700000002.bbbb.host',
                   'pm-pogo', 'urgent', 'please read')

        user = self.dead_user()
        self.clock.advance(2 * b.CONFIG['relay_heartbeat'])
        self.run_cycles(user, 3)

        self.assertGreaterEqual(user.send.await_count, 1,
                                'sanity: sends must have been attempted')
        self.assertEqual(self.grep_c('relay:'), 0,
                         'no positive record while delivery is wedged')
        # And note where the evidence of the wedge actually is: the other file.
        # A reader who greps only bridget.log — which is everyone, because that
        # is the path in every runbook — sees the beat stop and nothing else.
        # The stop IS the signal; before this fix there was no stop to see.
        self.assertEqual(self.grep_c('DM failed for', self.errfile), 3)
        self.assertEqual(self.grep_c('DM failed for'), 0)

    def test_a_recovered_relay_speaks_again(self):
        """The other half of S2: the record must not latch. Once delivery works
        the file says so, so a reader can date the recovery as well as the
        outage."""
        b = self.bridget
        self.prime_seen_empty()
        write_mail(b.MAIL_DIR, '1700000003.cccc.host', 'pm-pogo', 'down', 'x')
        self.run_cycles(self.dead_user(), 2)
        self.assertEqual(self.grep_c('relay:'), 0)

        self.run_cycles(self.ok_user(), 1)
        self.assertEqual(self.grep_c('relay:'), 1)
        self.assertIn('relay: 1 delivered', self.logfile.read_text())


class VolumeIsBoundedTest(RelayRecordHarness):
    """V1/V2 — bounded in both directions, so the record cannot bury the
    exception lines it sits among."""

    def test_a_burst_of_33_alarms_is_one_line_not_33(self):
        """V1. The measured flood, replayed. A per-mail line would put 33 lines
        in the file; the beat folds them into one and keeps the count."""
        b = self.bridget
        self.prime_seen_empty()
        # 33 distinct conditions, so the duplication limit holds none of them
        # and all 33 really are relayed — the shape of the measured incident,
        # where bridget carried every one within seconds.
        for i in range(33):
            write_mail(b.MAIL_DIR, f'17000001{i:02d}.dddd.host',
                       'pm-pogo', f'blackout host-{"x" * (i + 1)}', 'body')

        self.run_cycles(self.ok_user(), 1)

        self.assertEqual(self.grep_c('relay:'), 1)
        self.assertIn('relay: 33 delivered in the last ',
                      self.logfile.read_text())

    def test_a_repeat_the_duplication_limit_held_is_not_counted_as_relayed(self):
        """The count must not overstate. A held repeat reached nothing, so it is
        not "delivered" — it gets a `dedup:` line in the same file instead, and
        the two lines together say what happened without either lying."""
        b = self.bridget
        self.prime_seen_empty()
        for i in range(4):
            write_mail(b.MAIL_DIR, f'17000002{i:02d}.eeee.host',
                       'pm-pogo', 'disk 91% full', 'body')

        self.run_cycles(self.ok_user(), 1)

        self.assertIn('relay: 1 delivered in the last ', self.logfile.read_text())
        self.assertEqual(self.grep_c('dedup: suppressed repeat'), 3)

    def test_idle_cycles_do_not_produce_a_line_each(self):
        """V2. The delivery loop turns every few seconds. One line per cycle
        would be thousands a day — the repetition mg-5521 was merged to stop,
        moved into the log file."""
        interval = self.bridget.CONFIG['relay_heartbeat']
        self.run_cycles(self.ok_user(), 1)          # the startup beat
        for _ in range(20):
            self.clock.advance(interval / 100.0)    # 20 cycles, well inside it
            self.run_cycles(self.ok_user(), 1)
        self.assertEqual(self.grep_c('relay:'), 1)

        # And it does beat once the interval is actually up.
        self.clock.advance(interval)
        self.run_cycles(self.ok_user(), 1)
        self.assertEqual(self.grep_c('relay:'), 2)

    def test_startup_states_the_cadence_so_zero_lines_is_readable(self):
        """The record's own version of the defect: switched off, it produces the
        same nothing as a dead relay. So the setting is stated in the file."""
        self.assertIn('`relay:` line every 3600s when idle',
                      self.bridget.relay_status_line())

    def test_the_default_cadence_is_at_most_a_line_an_hour_when_idle(self):
        """The shipped default, stated as the number a reader cares about:
        24 lines a day of positive record on a silent bridget."""
        self.assertEqual(self.bridget.CONFIG['relay_heartbeat'], 3600)


class PreFixControlTest(RelayRecordHarness):
    """S3 — the defect, reproduced. With the record switched off, a healthy
    bridget and a wedged one write the same bytes about delivery, and every
    conclusion drawn from a `grep -c` zero is unjustified again.

    This is here because a fix whose absence has never been demonstrated is a
    fix nobody has watched fail.
    """

    ENV = {'BRIDGET_RELAY_HEARTBEAT': '0'}

    def _delivery_evidence(self, run) -> str:
        """Everything the run said about delivery, with the timestamps stripped
        (they are the one thing that always differs)."""
        return '\n'.join(
            re.sub(r'^\[[^\]]+\] ', '', line)
            for line in run.splitlines()
            if 'relay:' in line
        )

    def test_healthy_and_dead_are_byte_identical_without_the_record(self):
        b = self.bridget
        self.assertFalse(b.RELAY.enabled, 'sanity: the record is switched off')

        healthy = self.run_cycles(self.ok_user(), 3)

        # A different, fully dead relay: mail present, every send failing.
        self.prime_seen_empty()
        write_mail(b.MAIL_DIR, '1700000004.eeee.host', 'pm-pogo', 'x', 'y')
        dead_user = self.dead_user()
        dead = self.run_cycles(dead_user, 3)

        self.assertEqual(self._delivery_evidence(healthy), '')
        self.assertEqual(self._delivery_evidence(healthy),
                         self._delivery_evidence(dead),
                         'the defect: health and death write the same bytes')
        self.assertEqual(self.grep_c('relay:'), 0)
        # ...and yet one of these runs delivered nothing at all.
        self.assertGreaterEqual(dead_user.send.await_count, 1)

    def test_switching_it_off_is_itself_announced(self):
        """The one thing that must NOT be silent about the silence. Otherwise
        `grep -c relay:` = 0 is ambiguous again — dead relay, or a knob someone
        turned — and the fix has reproduced the defect inside itself."""
        line = self.bridget.relay_status_line()
        self.assertIn('OFF (BRIDGET_RELAY_HEARTBEAT=0)', line)
        self.assertIn('look identical in this log', line)


class LedgerCadenceTest(unittest.TestCase):
    """The cadence rules on their own, where they can be read.

    The adapter tests above prove the wiring; these pin the arithmetic, which
    is the part that decides whether the file is readable a month from now.
    """

    def ledger(self, **kw):
        self.clock = FakeClock()
        return RelayLedger(started=self.clock(), clock=self.clock, **kw)

    def test_first_healthy_cycle_beats_immediately(self):
        """Otherwise a fresh process is indistinguishable from a dead one for a
        whole interval — the defect, narrowed to the first hour."""
        led = self.ledger(interval=3600)
        self.assertIsNotNone(led.due())

    def test_activity_beats_sooner_than_idle_but_not_per_cycle(self):
        led = self.ledger(interval=3600)
        led.due()                       # startup beat
        led.record()
        self.clock.advance(30)
        self.assertIsNone(led.due(), 'inside the active gap: fold, do not beat')
        self.clock.advance(31)
        beat = led.due()
        self.assertIsNotNone(beat)
        self.assertEqual(beat.delivered, 1)

    def test_the_active_gap_never_exceeds_the_idle_interval(self):
        """A short interval must not be lengthened by the activity cap."""
        led = self.ledger(interval=10)
        self.assertEqual(led.active_gap, 10)

    def test_counts_reset_per_beat_but_the_total_does_not(self):
        led = self.ledger(interval=60)
        led.due()
        led.record(3)
        self.clock.advance(60)
        first = led.due()
        self.clock.advance(60)
        second = led.due()
        self.assertEqual((first.delivered, first.total), (3, 3))
        self.assertEqual((second.delivered, second.total), (0, 3))

    def test_disabled_ledger_never_beats(self):
        led = self.ledger(interval=0)
        led.record()
        self.clock.advance(100_000)
        self.assertIsNone(led.due())


if __name__ == '__main__':
    unittest.main(verbosity=2)
