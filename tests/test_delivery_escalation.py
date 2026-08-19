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

"""Behavioral acceptance for the delivery-wedge escalation and self-heal (mg-3f08).

The incident: on 2026-08-19, 07:30:00Z to 07:38:10Z, every outbound send failed
with the same line every ~35s —

    deliver failed for <msg-id>, will retry: Cannot connect to host
    discord.com:443 ssl:default [nodename nor servname provided, or not known]

— while the host resolved `discord.com` 5/5 from a shell. Eight minutes of 100%
delivery failure produced no alert, no mail to the mayor, no event, and no
change in any health surface; `supervise` recorded the eventual exit as
`rc=143 after 639957s (healthy run)`. The message stuck in the retry loop was
pogod's own `AGENTS ARE FAILING EVERY TURN` escalation, whose only recipient is
the human, who found out by noticing that nothing had reacted.

mg-879c then dated every failure line in `bridget.err.log` across 2026-08-04..19
and found the SAME aiohttp DNS failure four times — 51 messages stuck over
08-04..10, 10 over 08-14, 164 over a 71.6h outage on 08-16..19, and this one.
Every occurrence ended in a restart and none ended any other way. That is what
these tests are written against: not one wedge, a class of them.

The ticket's own framing is the acceptance criterion — "the real defect is that
nothing notices" — so every assertion here is about what a THIRD PARTY learns,
and each is paired with the pre-fix control showing it learned nothing:

    E1-E4   an outage past the threshold reaches surfaces that are not Discord
    E5-E6   ...and the escalation's own failure is not silent either
    H1-H4   the self-heal exits, is budgeted, and is never silent
    B1-B5   the budget survives the restarts it counts, and fails CLOSED
    W1-W6   the thresholds, the reset, and the repeat cadence

`escalate_delivery_wedge` is driven out of the real `watch_mailbox` in the real
script, against a stubbed discord and a stubbed `mg`/`pogo`, so what is proven
is the shipped wiring and not a model of it.
"""
import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'tests'))
sys.path.insert(0, str(REPO))

import testtmp  # noqa: E402
from bridget_core.logstamp import LineTimestamper  # noqa: E402
from bridget_core.relaylog import RelayLedger  # noqa: E402
from bridget_core.wedgewatch import (  # noqa: E402
    EXIT_SELFHEAL,
    RestartBudget,
    WedgeWatch,
)

SCRIPT = REPO / 'bridget'


class _FakeHTTPException(Exception):
    """Stand-in for discord.HTTPException."""


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


# --------------------------------------------------------------------------
# W: the decision. Pure, so the thresholds under test are the shipped ones and
# an hour of cadence costs no wall time.
# --------------------------------------------------------------------------

class WedgeDecisionTest(unittest.TestCase):

    def watch(self, tmp=None, **kw):
        self.clock = FakeClock()
        budget = None
        if kw.pop('with_budget', True):
            budget = RestartBudget(
                (tmp or testtmp.mkdtemp('wedge')) / 'selfheal.json',
                limit=kw.pop('limit', 3),
                window=kw.pop('window', 3600),
                clock=self.clock)
        return WedgeWatch(escalate_after=kw.pop('escalate_after', 120),
                          selfheal_after=kw.pop('selfheal_after', 300),
                          repeat_interval=kw.pop('repeat_interval', 3600),
                          budget=budget, clock=self.clock, **kw)

    def fail_for(self, w, seconds, step=5):
        """Fail every `step` seconds for `seconds`, returning every verdict."""
        out = []
        for _ in range(int(seconds // step)):
            out.append(w.failing())
            self.clock.advance(step)
        return out

    def test_W1_a_brief_flap_escalates_nothing(self):
        """The threshold is what separates an incident from a rate-limit."""
        w = self.watch()
        verdicts = self.fail_for(w, 115)
        self.assertEqual([v for v in verdicts if v.escalate], [])
        self.assertEqual([v for v in verdicts if v.selfheal], [])

    def test_W2_the_eight_minute_wedge_escalates_and_restarts(self):
        """The measured incident, replayed at the shipped poll interval."""
        w = self.watch()
        verdicts = self.fail_for(w, 490)
        escs = [v.escalate for v in verdicts if v.escalate]
        heals = [v.selfheal for v in verdicts if v.selfheal]
        self.assertEqual(len(escs), 2, 'the threshold alarm and the restart alarm')
        self.assertEqual(len(heals), 1)
        # It fires at the threshold, not at the end of the outage: on the day,
        # a human restarted it at 07:43 — this fires at 07:32.
        self.assertLess(escs[0].stalled_for, 130)
        self.assertEqual(escs[0].escalations, 1)
        self.assertGreaterEqual(heals[0].stalled_for, 300)

    def test_W3_a_healthy_cycle_ends_the_incident(self):
        w = self.watch()
        self.fail_for(w, 200)
        self.assertIsNotNone(w.since)
        w.healthy()
        self.assertIsNone(w.since)
        self.assertEqual(w.cycles, 0)
        # ...and a recurrence is a NEW first alarm, not a continuation. A
        # recurrence is the more interesting of the two and must not be folded
        # into an hourly repeat interval belonging to the outage before it.
        v = self.fail_for(w, 200)
        escs = [x.escalate for x in v if x.escalate]
        self.assertEqual([e.escalations for e in escs], [1])

    def test_W4_a_long_outage_repeats_hourly_not_per_cycle(self):
        """The 71.6h outage of mg-879c: 51,552 poll cycles at 5s. An alarm per
        cycle is not an alarm."""
        w = self.watch()
        verdicts = self.fail_for(w, 3600 * 6, step=5)
        escs = [v.escalate for v in verdicts if v.escalate]
        self.assertEqual(len(escs), 7, 'onset, the restart, then one an hour')
        self.assertEqual([e.escalations for e in escs], [1, 2, 3, 4, 5, 6, 7])

    def test_W5_escalation_off_still_reports_the_restart(self):
        """Guarantee 1: no self-heal is silent, whatever the knobs say.

        The pre-fix state is a process that vanishes and returns with fresh
        counters, leaving the same nothing-happened trace as the wedge itself.
        """
        w = self.watch(escalate_after=0)
        verdicts = self.fail_for(w, 400)
        escs = [v.escalate for v in verdicts if v.escalate]
        heals = [v.selfheal for v in verdicts if v.selfheal]
        self.assertEqual(len(heals), 1)
        self.assertEqual(len(escs), 1, 'the restart forced one out')

    def test_W6_a_backwards_clock_step_does_not_report_a_negative_outage(self):
        """Sleep/wake and NTP step this host; mg-879c's own budget notes it."""
        w = self.watch()
        w.failing()
        self.clock.advance(-500)
        v = w.failing()
        self.assertIsNone(v.escalate)
        self.assertEqual(w.cycles, 1, 'the run restarted rather than going negative')

    def test_W7_the_decision_module_never_touches_the_transport(self):
        """The circularity in mg-f867: an outage cannot be reported through the
        thing that is broken. Tripwire, not proof — it greps."""
        src = (REPO / 'bridget_core' / 'wedgewatch.py').read_text()
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            body = getattr(node, 'body', None)
            if isinstance(body, list) and body:
                first = body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    body.pop(0)
        code = ast.unparse(tree)
        for forbidden in ('discord', 'aiohttp', 'requests'):
            self.assertNotIn(forbidden, code)


# --------------------------------------------------------------------------
# B: the restart budget. The one piece of state that cannot live in the process
# the self-heal restarts.
# --------------------------------------------------------------------------

class RestartBudgetTest(unittest.TestCase):

    def setUp(self):
        self.tmp = testtmp.mkdtemp('budget')
        self.clock = FakeClock()
        self.path = self.tmp / 'selfheal.json'

    def budget(self, **kw):
        return RestartBudget(kw.pop('path', self.path), limit=kw.pop('limit', 3),
                             window=kw.pop('window', 3600), clock=self.clock)

    def test_B1_the_budget_survives_the_restarts_it_counts(self):
        """The defect this class exists to avoid: an in-memory cap on restarts
        is reset BY the restart, which is a flap wearing a rate limiter's
        clothes. Each `budget()` here is a fresh process."""
        for expected in (1, 2, 3):
            granted, spent = self.budget().spend()
            self.assertTrue(granted)
            self.assertEqual(spent, expected)
        granted, spent = self.budget().spend()
        self.assertFalse(granted, 'a fourth process must be refused')
        self.assertEqual(spent, 3)

    def test_B2_the_window_rolls(self):
        b = self.budget()
        for _ in range(3):
            b.spend()
        self.assertFalse(b.spend()[0])
        self.clock.advance(3601)
        self.assertTrue(self.budget().spend()[0])

    def test_B3_an_unwritable_ledger_refuses_the_restart(self):
        """Fail CLOSED. If the write fails, every restart re-reads an empty
        ledger and the cap is unlimited — a budget that fails open is not a
        budget, and unbounded restarts are worse than the wedge."""
        b = self.budget(path=self.tmp / 'nope' / 'deep' / 'selfheal.json')
        with mock.patch('bridget_core.wedgewatch.write_state',
                        side_effect=OSError('read-only file system')):
            granted, spent = b.spend()
        self.assertFalse(granted)
        self.assertIn('could not record the restart', b.last_refusal)

    def test_B4_a_corrupt_ledger_reads_as_empty_but_is_rewritten(self):
        """Fail OPEN on the read: a fresh install and a truncated file both
        deserve a first restart. The write then repairs it, so the fail-open
        lasts exactly one grant rather than forever."""
        self.path.write_text('{not json at all')
        b = self.budget()
        self.assertEqual(b.spent(), 0)
        self.assertTrue(b.spend()[0])
        self.assertEqual(json.loads(self.path.read_text()), [self.clock()])

    def test_B5_a_refusal_says_which_refusal_it_was(self):
        """'the budget is spent' and 'I cannot account for restarts' call for
        different actions from whoever reads the escalation."""
        b = self.budget()
        for _ in range(3):
            b.spend()
        b.spend()
        self.assertIn('restarts already in the last', b.last_refusal)
        self.assertNotIn('could not record', b.last_refusal)

    def test_B6_a_zero_budget_disables_the_self_heal(self):
        w = WedgeWatch(escalate_after=120, selfheal_after=300,
                       budget=self.budget(limit=0), clock=self.clock)
        self.assertFalse(w.selfheal_enabled)
        for _ in range(200):
            v = w.failing()
            self.assertIsNone(v.selfheal)
            self.clock.advance(5)

    def test_B7_a_refused_spend_is_asked_for_once_per_outage(self):
        """Not once per cycle: a refusal re-asked every 5s would rewrite the
        ledger forever and bury itself in its own repetition (mg-5521, one
        subsystem over)."""
        b = self.budget(limit=1)
        b.spend()
        w = WedgeWatch(escalate_after=120, selfheal_after=300, budget=b,
                       clock=self.clock)
        calls = []
        real = b.spend
        b.spend = lambda now=None: (calls.append(now), real(now))[1]
        for _ in range(400):
            w.failing()
            self.clock.advance(5)
        self.assertEqual(len(calls), 1)


# --------------------------------------------------------------------------
# E/H: the shipped wiring, driven out of the real watch_mailbox.
# --------------------------------------------------------------------------

def load_bridget(fake_home: Path, env_overrides: dict | None = None):
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


class WedgeHarness(unittest.TestCase):
    """The real script, a stubbed discord, a wound clock, and real log FILES
    the assertions grep — the same rig test_relay_record.py uses, because the
    reader whose experience is under test is a reader of those files."""

    ENV: dict = {}

    def setUp(self):
        self.tmp = testtmp.mkdtemp('wedge-e2e')
        self.bridget = load_bridget(self.tmp, self.ENV)
        self.clock = FakeClock()
        b = self.bridget
        b.POLL_INTERVAL = 0.001
        b.THREADS_ENABLED = False
        b.RELAY = RelayLedger(started=self.clock(),
                              interval=b.CONFIG['relay_heartbeat'],
                              clock=self.clock)
        self.budget = RestartBudget(self.tmp / '.pogo' / 'bridget.selfheal.json',
                                    limit=b.CONFIG['wedge_restart_budget'],
                                    window=b.CONFIG['wedge_budget_window'],
                                    clock=self.clock)
        b.WEDGE = WedgeWatch(escalate_after=b.CONFIG['wedge_escalate_after'],
                             selfheal_after=b.CONFIG['wedge_selfheal_after'],
                             repeat_interval=b.CONFIG['wedge_repeat'],
                             budget=self.budget, clock=self.clock)
        # The two out-of-band surfaces, recorded rather than executed. Both are
        # `subprocess.run` in the real thing; nothing here shells out.
        self.mg_calls, self.pogo_calls = [], []
        self.mg_rc, self.pogo_rc = 0, 0
        b.run_mg = lambda args: (self.mg_calls.append(args),
                                 (self.mg_rc, '', 'stub mg failure'))[1]
        b.run_pogo = lambda args: (self.pogo_calls.append(args),
                                   (self.pogo_rc, '', 'stub pogo failure'))[1]
        # The self-heal calls os._exit, which would take the test runner with
        # it. Recorded here; H2 proves the real function reaches os._exit.
        self.exits = []
        self.real_selfheal = b.selfheal_delivery_wedge
        b.selfheal_delivery_wedge = lambda heal: self.exits.append(heal)
        self.logfile = self.tmp / 'bridget.log'
        self.errfile = self.tmp / 'bridget.err.log'
        self.logfile.write_text('')
        self.errfile.write_text('')

    def prime_seen_empty(self):
        b = self.bridget
        b.SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        b.SEEN_FILE.write_text('')

    def write_mail(self, name, subject='AGENTS ARE FAILING EVERY TURN'):
        d = self.bridget.MAIL_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(f"From: pogod\nSubject: {subject}\n\nbody\n")

    def dead_user(self):
        """A user whose every send raises the aiohttp DNS error verbatim."""
        user = mock.MagicMock()
        user.send = mock.AsyncMock(side_effect=OSError(
            'Cannot connect to host discord.com:443 ssl:default '
            '[nodename nor servname provided, or not known]'))
        return user

    def ok_user(self):
        user = mock.MagicMock()
        user.send = mock.AsyncMock()
        return user

    def run_cycles(self, user, n, step=5):
        """Run watch_mailbox for n cycles, winding the clock `step` per cycle."""
        b = self.bridget
        b.client.is_closed = mock.Mock(side_effect=[False] * n + [True])
        real_sleep = asyncio.sleep

        async def stepping_sleep(_):
            self.clock.advance(step)
            await real_sleep(0)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(b.asyncio, 'sleep', stepping_sleep), \
                redirect_stdout(LineTimestamper(out)), \
                redirect_stderr(LineTimestamper(err)):
            asyncio.run(b.watch_mailbox(user))
        for buf, path in ((out, self.logfile), (err, self.errfile)):
            with path.open('a') as fh:
                fh.write(buf.getvalue())
        return out.getvalue()

    def grep_c(self, pattern, path=None):
        r = subprocess.run(['grep', '-c', pattern, str(path or self.logfile)],
                           capture_output=True, text=True)
        return int(r.stdout.strip() or 0)


class EscalationReachesSomebodyTest(WedgeHarness):
    """E1-E6. What a third party learns, and the control showing they used to
    learn nothing."""

    def wedge(self, cycles=120):
        self.prime_seen_empty()
        self.write_mail('1700000010.aaaa.host')
        return self.run_cycles(self.dead_user(), cycles)

    def test_E1_an_outage_reaches_the_local_event_log(self):
        self.wedge()
        emits = [c for c in self.pogo_calls if c[:2] == ['events', 'emit']]
        self.assertTrue(emits, 'no pogo event for eight minutes of total failure')
        self.assertIn('--type=bridget_delivery_wedged', emits[0])

    def test_E2_an_outage_reaches_the_mayors_maildir(self):
        self.wedge()
        sends = [c for c in self.mg_calls if c[:2] == ['mail', 'send']]
        self.assertTrue(sends)
        self.assertEqual(sends[0][2], self.bridget.CONFIG['mail_recipient'])
        self.assertIn('--from=bridget', sends[0])

    def test_E3_the_report_carries_the_cause_not_just_the_fact(self):
        """'delivery is failing' is not actionable. The resolver error is what
        told the mayor to test the host resolver and find it answering 5/5."""
        self.wedge()
        body = [a for a in
                next(c for c in self.mg_calls if c[:2] == ['mail', 'send'])
                if a.startswith('--body=')][0]
        self.assertIn('nodename nor servname provided', body)
        self.assertIn('resolver probe:', body)
        self.assertIn('mail waiting', body)

    def test_E4_the_escalation_does_not_travel_over_discord(self):
        """The circularity that made the 2026-08-19 alarm void: the escalation
        and its transport failed together. Every send this loop attempted was
        the stuck mail itself, never the report."""
        out = self.wedge()
        self.assertIn('delivery-wedge:', out)
        self.assertGreaterEqual(self.grep_c('delivery-wedge:'), 1)
        # Both out-of-band surfaces are subprocesses against local files.
        self.assertTrue(self.pogo_calls and self.mg_calls)

    def test_E5_if_both_surfaces_refuse_the_log_says_so_loudly(self):
        """The escalation path is an artifact of the same kind as the defect,
        so it can exhibit it. If both refuse, the failure of the report is
        itself reported — to stdout AND stderr."""
        self.mg_rc, self.pogo_rc = 3, 1
        self.wedge()
        self.assertGreaterEqual(self.grep_c('delivery-wedge-unreported:'), 1)
        self.assertGreaterEqual(
            self.grep_c('delivery-wedge-unreported:', self.errfile), 1)

    def test_E6_one_surface_surviving_is_not_reported_as_a_failure(self):
        """Redundancy means either alone is enough. A false 'nothing outside
        this log knows' would train its reader to ignore the real one."""
        self.mg_rc, self.pogo_rc = 3, 0
        self.wedge()
        self.assertEqual(self.grep_c('delivery-wedge-unreported:'), 0)

    def test_E7_a_delivery_count_is_never_inflated_by_an_outage(self):
        """`grep -c 'relay:'` counts deliveries and only deliveries — the whole
        value of the positive record (mg-7c1b). A third token must not break it."""
        self.wedge()
        self.assertEqual(self.grep_c('relay:'), 0)
        self.assertGreaterEqual(self.grep_c('delivery-wedge:'), 1)

    def test_E8_the_pre_fix_control_a_short_flap_alarms_nobody(self):
        """The threshold is real: below it, this is indistinguishable from the
        pre-fix bridge, which is correct."""
        self.prime_seen_empty()
        self.write_mail('1700000010.bbbb.host')
        self.run_cycles(self.dead_user(), 10)   # 50s < 120s
        self.assertEqual(self.grep_c('delivery-wedge:'), 0)
        self.assertEqual([c for c in self.mg_calls if c[:2] == ['mail', 'send']], [])
        self.assertEqual(self.pogo_calls, [])

    def test_E9_recovery_ends_the_incident_and_the_positive_resumes(self):
        self.wedge()
        before = len(self.mg_calls)
        self.prime_seen_empty()
        self.run_cycles(self.ok_user(), 3)
        self.assertGreaterEqual(self.grep_c('relay:'), 1)
        self.assertEqual(len(self.mg_calls), before, 'no alarm on a healthy cycle')


class SelfHealTest(WedgeHarness):
    """H1-H4. The action, and the guarantee that it is never silent."""

    def test_H1_a_wedge_past_the_threshold_restarts_the_process(self):
        self.prime_seen_empty()
        self.write_mail('1700000010.cccc.host')
        self.run_cycles(self.dead_user(), 120)
        self.assertEqual(len(self.exits), 1)
        self.assertGreaterEqual(self.exits[0].stalled_for, 300)
        self.assertEqual(self.exits[0].spent, 1)

    def test_H2_the_real_self_heal_exits_with_the_supervisors_code(self):
        """`os._exit`, not `sys.exit`: this runs inside an asyncio task, where
        SystemExit is the task's to interpret — a self-heal that may quietly
        not heal is the defect class, reintroduced by the remedy.

        The shipped function, not the harness's recorder. `os._exit` is patched
        because an unpatched one would take the test runner with it, which is
        also the proof that it is the real thing.
        """
        b = self.bridget
        heal = mock.MagicMock(stalled_for=312.0, cycles=62,
                              since=self.clock(), spent=1, budget=3)
        with mock.patch.object(b.os, '_exit') as ex, \
                redirect_stdout(io.StringIO()) as out:
            self.real_selfheal(heal)
        ex.assert_called_once_with(EXIT_SELFHEAL)
        self.assertIn('delivery-selfheal:', out.getvalue())
        self.assertIn(str(EXIT_SELFHEAL), out.getvalue())

    def test_H2b_the_supervisor_names_that_exit_code(self):
        """Without this branch the line reads `exited rc=75 ... (too fast)`,
        which is the sentence a bad token produces. `rc=143 after 639957s
        (healthy run)` over the 2026-08-19 outage is the same failure one
        incident earlier: true, and it told nobody anything."""
        sup = (REPO / 'bridget-supervise').read_text()
        self.assertIn(f'rc == {EXIT_SELFHEAL}', sup)
        self.assertIn('self-healed a wedged delivery path', sup)

    def test_H3_the_restart_is_escalated_before_it_happens(self):
        """Ordered, and the order is load-bearing: an escalation racing its own
        exit is a report that may not exist."""
        self.prime_seen_empty()
        self.write_mail('1700000010.dddd.host')
        self.run_cycles(self.dead_user(), 120)
        sends = [c for c in self.mg_calls if c[:2] == ['mail', 'send']]
        restart_reports = [c for c in sends
                           if any('restarting now' in a for a in c)]
        self.assertTrue(restart_reports, 'the restart itself was never reported')

    def test_H4_an_exhausted_budget_reports_instead_of_restarting(self):
        """A real network outage costs three respawns and then stops, leaving a
        live bridget escalating rather than a flapping one."""
        for _ in range(self.bridget.CONFIG['wedge_restart_budget']):
            self.budget.spend()
        self.prime_seen_empty()
        self.write_mail('1700000010.eeee.host')
        self.run_cycles(self.dead_user(), 120)
        self.assertEqual(self.exits, [])
        sends = [c for c in self.mg_calls if c[:2] == ['mail', 'send']]
        # Two: the threshold alarm at 120s, and — the part H4 is really about —
        # a SECOND one at 300s saying the promised restart is not coming. The
        # first alarm says "will restart after 300s"; without the second, that
        # promise is quietly broken and the next word on it is an hour later.
        self.assertEqual(len(sends), 2)
        bodies = [[a for a in c if a.startswith('--body=')][0] for c in sends]
        self.assertIn('will restart after', bodies[0])
        self.assertIn('not restarting', bodies[1])
        self.assertIn('needs a human', bodies[1])
        self.assertIn('restarts already in the last', bodies[1])


class RestartIntoABrokenResolverTest(WedgeHarness):
    """R1-R2. The remedy is an artifact of the same kind as the defect.

    The self-heal restarts bridget *during* a delivery outage, so it multiplies
    the number of startups that happen while the resolver is wedged — and
    startup sends a DM. Until mg-3f08 that send was guarded by `except
    discord.HTTPException`, which does not catch aiohttp's
    ClientConnectorDNSError (an OSError). So the greeting raised, the exception
    propagated out of `watch_mailbox`, and the delivery watcher died: a live
    bridget with no delivery loop, reached by fixing the wedge.
    """

    def test_R1_a_greeting_that_cannot_send_does_not_kill_the_watcher(self):
        """The state every self-heal restart lands in while DNS is still broken:
        a fresh process whose first act is a DM into a wedged resolver.

        The greeting is proven to have actually failed (else this asserts
        nothing), and the watcher is proven to have survived it far enough to
        run the whole outage and escalate.
        """
        self.prime_seen_empty()
        self.write_mail('1700000010.ffff.host')
        self.run_cycles(self.dead_user(), 120)
        self.assertGreaterEqual(
            self.grep_c('startup DM failed', self.errfile), 1,
            'the greeting did not fail, so this proves nothing')
        self.assertGreaterEqual(self.grep_c('delivery-wedge:'), 1,
                                'the watcher died before it could ever alarm')
        self.assertEqual(len(self.exits), 1)

    def test_R2_the_greeting_catches_more_than_HTTPException(self):
        """A tripwire against re-narrowing it. Read off the AST rather than
        grepped, so the comment explaining the history cannot satisfy it."""
        import ast
        tree = ast.parse(SCRIPT.read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == 'send_startup_dm')
        caught = {ast.unparse(h.type) for n in ast.walk(fn)
                  if isinstance(n, ast.Try) for h in n.handlers if h.type}
        self.assertIn('Exception', caught)
        self.assertNotIn('discord.HTTPException', caught)


class StartupSaysWhetherItIsOnTest(WedgeHarness):
    """A reader who has never seen a `delivery-wedge:` line cannot tell 'no
    outage' from 'switched off'. This file made that exact mistake one level
    down (mg-7c1b/mg-879c), so the setting is stated at startup."""

    def test_the_cadence_is_announced(self):
        line = self.bridget.wedge_status_line()
        self.assertIn('escalate out of band after 120s', line)
        self.assertIn(f'exit {EXIT_SELFHEAL}', line)

    def test_switching_it_off_is_itself_announced(self):
        b = self.bridget
        b.WEDGE = WedgeWatch(escalate_after=0, selfheal_after=0, budget=None)
        self.assertIn('OFF', b.wedge_status_line())
        self.assertIn('mg-3f08', b.wedge_status_line())


if __name__ == '__main__':
    unittest.main(verbosity=2)
