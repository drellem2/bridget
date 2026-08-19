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

"""The thread-CREATION rate limit and backlog coalescing (mg-7dda).

mg-27e0 bounded the number of threads bridget holds OPEN. mg-2ab2 then measured
that population rising monotonically — 966, 968, 967, 971 — across the window in
which Daniel's client went from unreadable to readable, and concluded the
standing count was not the variable. What the same window did contain was a
RATE: 122 thread creations in the 06:00Z hour, peak 27 in the 06:56Z minute,
against 36 for the whole rest of that day, as a 71h28m DNS outage's backlog
flushed. mg-879c measured the same flush on the DM side — 171 messages into one
DM in under five minutes.

**A cap on the standing count does not bound a burst. These are different
quantities and until this ticket only one of them had a control.** These tests
pin the other one.

They make NO claim about the render failure. mg-27e0 shipped a causal claim that
had to be retracted from eight places; mg-2ab2 left a ~2h45m gap between the
burst decaying (~07:00Z) and Daniel reporting the channel readable (09:45Z)
explicitly unclosed, and server-side data cannot see a client reload. This is
hygiene that is worth having whatever broke the client —
`TestItMakesNoCausalClaim` is the tripwire that keeps it that way.

What is pinned:

  * under the threshold NOTHING changes: one conversation, one thread.
  * over it, a drained backlog from one correspondent becomes ONE thread rather
    than N. This is the "deduplication in the bridge" Daniel named; the
    duplication limit that already exists folds repeats of one CONDITION, and a
    drained backlog is many different subjects from the same correspondent.
  * the ceiling is a ceiling: coalescing alone only reduces the rate, because N
    first-time correspondents in one window are N threads.
  * nothing is dropped and nothing is delayed, on any branch.
  * a burst says so, in the file people grep and on the card the human reads,
    and never in the words the OTHER stranded path uses.
  * the `relay:` beat stops averaging a flush into invisibility.

It also carries a PRE-FIX CONTROL: with the rate limit switched off, the same
traffic reproduces one thread per message.
"""
import asyncio
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))

from bridget_core.burst import ThreadBurstLimiter  # noqa: E402
from bridget_core.relaylog import RelayLedger  # noqa: E402
from test_threading import (  # noqa: E402
    FakeTextChannel,
    FakeUser,
    load_threaded,
    mail,
)


class Clock:
    """A hand-wound clock. `t` is epoch seconds and only moves when told."""

    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def subject_for(n: int) -> str:
    """A distinct subject per mail, spelled in letters rather than digits.

    The duplication limit (mg-5521) normalises digit runs, so numbered subjects
    are ONE condition to it and get folded into a single conversation — which
    would leave these tests measuring a creation rate against a population of
    one. Same reasoning, and same helper, as tests/test_thread_cap.py.
    """
    return 'topic ' + ''.join(chr(ord('a') + int(d)) for d in str(n))


def limiter(**kw) -> ThreadBurstLimiter:
    kw.setdefault('window', 60)
    kw.setdefault('coalesce_above', 5)
    kw.setdefault('ceiling', 8)
    return ThreadBurstLimiter(**kw)


# --- the core limiter ------------------------------------------------------

class TestUnderTheThresholdNothingChanges(unittest.TestCase):
    """The state essentially all traffic is in. 36 threads across a whole day
    is ~0.03/min; the threshold is in the low tens."""

    def test_every_conversation_creates_its_own_thread(self):
        clock = Clock()
        lim = limiter(clock=clock)
        for n in range(4):
            adm = lim.admit('mayor', f'k{n}')
            self.assertEqual(adm.kind, 'create')
            self.assertEqual(adm.conversation, f'k{n}')
            clock.tick(1)

    def test_a_slow_stream_never_accumulates_a_rate(self):
        """The window is trailing, not cumulative. One creation a minute is not
        a burst however long it runs — which is the difference between this and
        the standing bound, where the same traffic accumulates forever."""
        clock = Clock()
        lim = limiter(clock=clock)
        for n in range(50):
            self.assertEqual(lim.admit('mayor', f'k{n}').kind, 'create')
            self.assertLessEqual(lim.rate(), 1,
                                 'a trailing window accumulated like a total')
            clock.tick(61)
        self.assertIsNone(lim.report(), 'a trickle opened a burst episode')


class TestABacklogFromOneCorrespondentIsOneThread(unittest.TestCase):
    """The headline. This is the coalescing Daniel asked for."""

    def test_past_the_threshold_mail_folds_onto_the_open_thread(self):
        clock = Clock()
        lim = limiter(clock=clock)
        for n in range(5):
            self.assertEqual(lim.admit('mayor', f'k{n}').kind, 'create')
        adm = lim.admit('mayor', 'k5')
        self.assertEqual(adm.kind, 'coalesce')
        self.assertEqual(adm.conversation, 'k4', 'folded onto the wrong thread')
        self.assertEqual(adm.folded, 1)

    def test_the_fold_count_climbs_and_the_rate_does_not(self):
        clock = Clock()
        lim = limiter(clock=clock)
        for n in range(5):
            lim.admit('mayor', f'k{n}')
        for n in range(5, 60):
            adm = lim.admit('mayor', f'k{n}')
            self.assertEqual(adm.kind, 'coalesce')
        self.assertEqual(lim.rate(), 5,
                         '55 folded mails must not have charged the rate')

    def test_a_different_correspondent_still_gets_their_own_thread(self):
        """Coalescing is on the correspondent axis. Folding two agents'
        backlogs together would put the human's reply in the wrong thread, and
        `Conversation.agent` is what routes it."""
        clock = Clock()
        lim = limiter(clock=clock)
        for n in range(5):
            lim.admit('mayor', f'k{n}')
        self.assertEqual(lim.admit('doctor', 'd0').kind, 'create')
        self.assertEqual(lim.admit('doctor', 'd1').conversation, 'd0')

    def test_an_expired_anchor_does_not_capture_a_later_episode(self):
        clock = Clock()
        lim = limiter(clock=clock, anchor_ttl=300)
        for n in range(5):
            lim.admit('mayor', f'k{n}')
        self.assertEqual(lim.admit('mayor', 'k5').kind, 'coalesce')
        clock.tick(301)
        # A fresh burst, hours later in wall-clock terms for the anchor.
        for n in range(10, 15):
            lim.admit('mayor', f'k{n}')
        adm = lim.admit('mayor', 'k15')
        self.assertEqual(adm.conversation, 'k14',
                         'the stale anchor should have been dropped, not reused')


class TestTheCeilingIsACeiling(unittest.TestCase):
    """Coalescing REDUCES the creation rate; it does not bound it. N
    first-time correspondents in one window are N threads, and without this
    branch the "rate limit" would be a hope."""

    def test_a_wide_burst_stops_creating_at_the_ceiling(self):
        clock = Clock()
        lim = limiter(clock=clock, coalesce_above=5, ceiling=8)
        kinds = [lim.admit(f'agent{n}', f'k{n}').kind for n in range(20)]
        self.assertEqual(kinds.count('create'), 8, 'the ceiling did not hold')
        self.assertEqual(kinds.count('over'), 12)
        self.assertEqual(lim.rate(), 8)

    def test_the_ceiling_lifts_when_the_window_slides(self):
        clock = Clock()
        lim = limiter(clock=clock, coalesce_above=5, ceiling=8)
        for n in range(20):
            lim.admit(f'agent{n}', f'k{n}')
        clock.tick(61)
        self.assertEqual(lim.rate(), 0)
        self.assertEqual(lim.admit('agent0', 'k0').kind, 'create',
                         'the conversation stranded by the ceiling never got '
                         'its thread')

    def test_coalescing_wins_over_the_ceiling(self):
        """The cheap remedy gets first refusal: a correspondent with an anchor
        folds even past the ceiling, because folding costs no creation at all."""
        clock = Clock()
        lim = limiter(clock=clock, coalesce_above=5, ceiling=8)
        lim.admit('mayor', 'k0')
        for n in range(1, 20):
            lim.admit(f'agent{n}', f'k{n}')
        self.assertEqual(lim.admit('mayor', 'kx').kind, 'coalesce')


class TestRollback(unittest.TestCase):

    def test_a_creation_that_failed_gives_the_budget_back(self):
        clock = Clock()
        lim = limiter(clock=clock)
        adm = lim.admit('mayor', 'k0')
        self.assertEqual(lim.rate(), 1)
        lim.rollback(adm)
        self.assertEqual(lim.rate(), 0)

    def test_it_withdraws_its_own_admission_not_the_latest(self):
        """Deliveries interleave across watchers at every `await`, so "the most
        recent creation" is not reliably the one that failed."""
        clock = Clock()
        lim = limiter(clock=clock)
        first = lim.admit('mayor', 'k0')
        clock.tick(1)
        lim.admit('doctor', 'd0')
        lim.rollback(first)
        self.assertEqual(lim.rate(), 1)
        # doctor's anchor survived; mayor's went with the rollback.
        self.assertEqual(lim.anchor_for('doctor'), 'd0')
        self.assertEqual(lim.anchor_for('mayor'), '')

    def test_it_leaves_an_anchor_a_later_mail_has_moved_on(self):
        clock = Clock()
        lim = limiter(clock=clock)
        first = lim.admit('mayor', 'k0')
        clock.tick(1)
        lim.admit('mayor', 'k1')
        lim.rollback(first)
        self.assertEqual(lim.anchor_for('mayor'), 'k1')


class TestItSaysSoOutLoud(unittest.TestCase):
    """A burst that is silently absorbed is a burst nobody can tell happened."""

    def test_the_episode_gets_an_onset_a_running_line_and_a_close(self):
        clock = Clock()
        lim = limiter(clock=clock)
        for n in range(5):
            lim.admit('mayor', f'k{n}')
        self.assertIsNone(lim.report(), 'no episode is open yet')
        lim.admit('mayor', 'k5')            # first fold opens the episode
        self.assertEqual(lim.report().phase, 'onset')
        self.assertIsNone(lim.report(), 'the running line is rate-limited')
        clock.tick(61)
        # The window has slid; the rate is back to zero, so the episode closes.
        report = lim.report()
        self.assertEqual(report.phase, 'over')
        self.assertEqual(report.coalesced, 1)
        self.assertGreaterEqual(report.peak, 5)

    def test_a_long_burst_costs_one_line_per_window(self):
        clock = Clock()
        lim = limiter(clock=clock)
        phases = []
        for _ in range(10):
            for n in range(6):
                lim.admit('mayor', f'k{clock.t}-{n}')
            report = lim.report()
            if report:
                phases.append(report.phase)
            clock.tick(30)
        self.assertEqual(phases[0], 'onset')
        self.assertTrue(all(p == 'continuing' for p in phases[1:]))
        self.assertLessEqual(len(phases), 6,
                             '10 half-window cycles must not be 10 lines')

    def test_the_close_carries_the_episode_totals(self):
        clock = Clock()
        lim = limiter(clock=clock, coalesce_above=5, ceiling=8)
        for n in range(20):
            lim.admit(f'agent{n}', f'k{n}')
        lim.report()
        clock.tick(61)
        report = lim.report()
        self.assertEqual(report.phase, 'over')
        self.assertEqual(report.over_ceiling, 12)
        self.assertGreater(report.elapsed, 0)


class TestSwitchedOffAndStrangeClocks(unittest.TestCase):

    def test_a_disabled_limiter_admits_everything(self):
        lim = ThreadBurstLimiter(coalesce_above=0, ceiling=0)
        self.assertFalse(lim.enabled)
        for n in range(100):
            self.assertEqual(lim.admit('mayor', f'k{n}').kind, 'off')
        self.assertIsNone(lim.report())

    def test_a_clock_that_steps_backwards_does_not_wedge(self):
        """An NTP step must not leave the limiter refusing to create forever.
        The same direction `DuplicateLimiter.decide` takes on a negative
        elapsed: deliver rather than suppress."""
        clock = Clock()
        lim = limiter(clock=clock)
        for n in range(5):
            lim.admit('mayor', f'k{n}')
        clock.tick(-3600)
        self.assertEqual(lim.admit('doctor', 'd0').kind, 'create')


# --- the adapter -----------------------------------------------------------

def threaded(above='5', ceiling='8', cap='0', **kw):
    """A bridget with threading on and the creation-rate limit configured.

    The STANDING bound is switched off by default (cap='0'). It is a different
    control on a different quantity, and leaving it on would have these tests
    measuring their thread counts against whichever of the two happened to fire
    first. tests/test_thread_cap.py returns the favour.
    """
    b = load_threaded(BRIDGET_MAX_LIVE_THREADS=cap,
                      BRIDGET_THREAD_BURST_ABOVE=above,
                      BRIDGET_THREAD_BURST_CEILING=ceiling, **kw)
    channel = FakeTextChannel(555, client=b.client)
    b.client.channels[555] = channel
    clock = Clock()
    b.BURST._clock = clock
    return b, channel, FakeUser(), clock


async def deliver(b, user, n, sender='mayor'):
    await b.deliver_mail(user, f'f{n}', mail(subject=subject_for(n), sender=sender,
                                             msg_id=f'id-{n}'))


class TestADrainedBacklogIsOneThread(unittest.IsolatedAsyncioTestCase):

    async def test_forty_mails_from_one_agent_do_not_open_forty_threads(self):
        b, channel, user, clock = threaded(above='5', ceiling='0')
        for n in range(1, 41):
            await deliver(b, user, n)
        self.assertEqual(len(channel.threads), 5,
                         'the backlog opened a thread per message')

    async def test_every_one_of_them_still_reaches_a_surface(self):
        """The limit may fold, and may withhold a thread. It may never drop.
        A limiter whose failure mode is a lost alert has become the defect."""
        b, channel, user, clock = threaded(above='5', ceiling='0')
        for n in range(1, 41):
            await deliver(b, user, n)
        posted = sum(len(t.sent) for t in channel.threads)
        self.assertEqual(posted, 40, 'mail went missing between the folds')
        self.assertEqual(len(user.sent), 40, 'the DM surface lost mail')

    async def test_the_folded_card_says_why_it_is_in_this_thread(self):
        b, channel, user, clock = threaded(above='5', ceiling='0')
        for n in range(1, 8):
            await deliver(b, user, n)
        folded = channel.threads[-1].sent[-1]
        self.assertIn('backlog burst', folded)
        self.assertIn(subject_for(7), folded,
                      "the mail's own subject must ride on the card")

    async def test_two_agents_bursting_get_a_thread_each(self):
        b, channel, user, clock = threaded(above='5', ceiling='0')
        for n in range(1, 21):
            await deliver(b, user, n, sender='mayor' if n % 2 else 'doctor')
        agents = {c.agent for c in b.CONVERSATIONS.values() if c.thread_live}
        self.assertEqual(agents, {'mayor', 'doctor'})

    async def test_a_reply_is_never_charged_and_never_folded(self):
        """A reply resolves onto a conversation that already has a thread, so
        it opens nothing. Charging it would make an ordinary conversation read
        as a burst."""
        b, channel, user, clock = threaded(above='2', ceiling='0')
        await deliver(b, user, 1)
        before = b.BURST.rate()
        for i in range(20):
            await b.deliver_mail(user, f'r{i}', mail(
                subject=subject_for(1), msg_id=f'r-{i}',
                in_reply_to='id-1', refs='id-1'))
        self.assertEqual(b.BURST.rate(), before)
        self.assertEqual(len(channel.threads), 1)


class TestTheCeilingOnTheAdapterPath(unittest.IsolatedAsyncioTestCase):

    async def test_past_the_ceiling_the_mail_is_delivered_without_a_thread(self):
        b, channel, user, clock = threaded(above='5', ceiling='8')
        for n in range(1, 21):
            await deliver(b, user, n, sender=f'agent{n}')
        self.assertEqual(len(channel.threads), 8)
        self.assertEqual(len(user.sent), 20, 'the ceiling dropped mail')

    async def test_it_does_not_claim_the_log_channel_is_unreachable(self):
        """The other stranded path says "log channel unreachable". Reusing that
        wording here would make two different controls indistinguishable in the
        one place the human sees either of them."""
        b, channel, user, clock = threaded(above='5', ceiling='8')
        for n in range(1, 21):
            await deliver(b, user, n, sender=f'agent{n}')
        last = user.sent[-1]
        self.assertIn('ceiling', last)
        self.assertNotIn('unreachable', last)

    async def test_a_stranded_conversation_gets_its_thread_once_the_rate_falls(self):
        b, channel, user, clock = threaded(above='5', ceiling='8')
        for n in range(1, 21):
            await deliver(b, user, n, sender=f'agent{n}')
        opened = len(channel.threads)
        clock.tick(61)
        # The same conversation's next message — it names the mail that was
        # delivered without a thread, so it resolves onto that conversation
        # rather than rooting a new one.
        await b.deliver_mail(user, 'f20b', mail(
            subject=subject_for(20), sender='agent20', msg_id='id-20b',
            in_reply_to='id-20', refs='id-20'))
        self.assertEqual(len(channel.threads), opened + 1,
                         'the paced conversation never got its thread')
        conv = b.CONVERSATIONS.get('id-20')
        self.assertIsNotNone(conv.thread_id)


class TestTheRemedyIsCheckedForTheDefectItRemedies(unittest.IsolatedAsyncioTestCase):
    """A remedy is an artifact of the same kind as the defect, so it is subject
    to it. Two ways this one could exhibit an unbounded burst of its own."""

    async def test_a_concurrent_burst_cannot_outrun_the_ceiling(self):
        """The check-then-act race — the exact shape THREAD_ADMISSION_LOCK
        exists for one level down. `admit()` records as it decides, with no
        `await` between, so N coroutines cannot all read "under the ceiling"
        and all create. A decide/commit split like the duplication limit's
        would have opened that window across the thread-create await."""
        b, channel, user, clock = threaded(above='5', ceiling='8')
        await asyncio.gather(*[deliver(b, user, n, sender=f'agent{n}')
                               for n in range(1, 41)])
        self.assertEqual(len(channel.threads), 8,
                         'a concurrent burst outran the creation ceiling')
        self.assertEqual(len(user.sent), 40, 'and it must still have cost nobody a mail')

    async def test_an_anchor_the_store_has_pruned_is_not_folded_onto(self):
        """Folding onto a conversation the store no longer holds would
        re-create an empty one and open a thread the limiter never charged for
        — a hole in the ceiling wearing a fold's clothes."""
        b, channel, user, clock = threaded(above='5', ceiling='0')
        for n in range(1, 7):
            await deliver(b, user, n)
        anchor = b.BURST.anchor_for('mayor')
        self.assertTrue(anchor)
        b.CONVERSATIONS.forget(anchor)
        before = b.BURST.rate()
        await deliver(b, user, 7)
        self.assertEqual(b.BURST.rate(), before + 1,
                         'the thread opened for a pruned anchor went uncharged')

    def test_its_own_bookkeeping_is_bounded(self):
        """The anchors are a per-correspondent map, and a fleet with a
        pathological number of senders must not turn the fix into a leak."""
        clock = Clock()
        lim = limiter(clock=clock, coalesce_above=5, ceiling=0, max_anchors=10)
        for n in range(500):
            lim.admit(f'agent{n}', f'k{n}')
            clock.tick(1)
        self.assertLessEqual(lim.summary()['anchors'], 10,
                             'the anchor map grew without bound')
        self.assertLessEqual(lim.rate(), 61,
                             'the creation window grew without bound')


class TestTheLogSaysABurstHappened(unittest.IsolatedAsyncioTestCase):

    async def test_the_onset_line_names_the_rate_and_the_threshold(self):
        b, channel, user, clock = threaded(above='5', ceiling='0')
        for n in range(1, 10):
            await deliver(b, user, n)
        report = b.BURST.report()
        line = b.format_thread_burst(report)
        self.assertTrue(line.startswith('thread-burst:'))
        self.assertIn('backlog', line)

    async def test_its_token_is_not_the_relay_or_dedup_token(self):
        """`grep -c relay:` must keep counting deliveries and nothing else."""
        b, channel, user, clock = threaded(above='5', ceiling='0')
        for n in range(1, 10):
            await deliver(b, user, n)
        line = b.format_thread_burst(b.BURST.report())
        self.assertNotIn('relay:', line)
        self.assertNotIn('dedup:', line)

    async def test_the_status_line_states_the_thresholds(self):
        b, channel, user, clock = threaded(above='5', ceiling='8')
        line = b.thread_burst_status()
        self.assertIn('5', line)
        self.assertIn('8', line)

    async def test_switched_off_says_so_loudly(self):
        b, channel, user, clock = threaded(above='0', ceiling='0')
        line = b.thread_burst_status()
        self.assertIn('OFF', line)
        self.assertIn('RATE', line)

    async def test_the_settings_command_shows_it(self):
        b, channel, user, clock = threaded(above='5', ceiling='8')
        for n in range(1, 4):
            await deliver(b, user, n)
        out = b.handle_command('settings')
        self.assertIn('Creation rate', out)


class TestConfigRefusesAnUnworkablePair(unittest.TestCase):

    def test_a_ceiling_below_the_coalescing_threshold_is_refused(self):
        """It would strand first-time correspondents on the DM path while the
        coalescing that would have absorbed them never engages."""
        with self.assertRaises(SystemExit):
            load_threaded(BRIDGET_THREAD_BURST_ABOVE='30',
                          BRIDGET_THREAD_BURST_CEILING='10')


# --- the relay beat stops averaging a flush into invisibility --------------

class TestTheRelayBeatReportsBurstsAsBursts(unittest.TestCase):
    """`relay: 171 delivered in the last 262762s` is the line the 71.6h outage
    of mg-879c recovered with. Every number in it is true and it reads as a
    trickle over three days; it was a backlog flushed in under five minutes."""

    def _ledger(self, clock):
        return RelayLedger(started=clock.t, interval=3600, clock=clock)

    def test_the_mg_879c_line_now_says_burst(self):
        clock = Clock()
        ledger = self._ledger(clock)
        ledger.due()                      # the first-cycle beat
        clock.tick(262_500)               # three days of nothing
        for _ in range(171):              # ...then 171 in 268 seconds
            ledger.record()
            clock.tick(268 / 171)
        beat = ledger.due()
        self.assertEqual(beat.delivered, 171)
        self.assertTrue(beat.is_burst)
        self.assertLess(beat.span, 300)
        self.assertGreater(beat.peak, 20)

    def test_a_steady_stream_is_not_a_burst(self):
        """Volume is not the test — misreporting is. A stream whose span fills
        its window is being reported at its real rate."""
        clock = Clock()
        ledger = self._ledger(clock)
        ledger.due()
        for _ in range(3600):
            ledger.record()
            clock.tick(1)
        beat = ledger.due()
        self.assertEqual(beat.delivered, 3600)
        self.assertFalse(beat.is_burst)
        self.assertGreater(beat.peak, 50)

    def test_two_mails_arriving_together_are_not_a_burst(self):
        clock = Clock()
        ledger = self._ledger(clock)
        ledger.due()
        clock.tick(3600)
        ledger.record()
        ledger.record()
        self.assertFalse(ledger.due().is_burst)

    def test_the_line_keeps_its_token_and_its_shape(self):
        b = load_threaded()
        clock = Clock()
        ledger = self._ledger(clock)
        ledger.due()
        clock.tick(262_500)
        for _ in range(171):
            ledger.record()
            clock.tick(1)
        line = b.format_relay_beat(ledger.due())
        self.assertTrue(line.startswith('relay: 171 delivered in the last '),
                        'the grep token and the leading facts must not move')
        self.assertIn('BURST', line)
        self.assertIn('peak', line)

    def test_an_idle_beat_is_unchanged(self):
        b = load_threaded()
        clock = Clock()
        ledger = self._ledger(clock)
        ledger.due()
        clock.tick(3600)
        line = b.format_relay_beat(ledger.due())
        self.assertIn('relay: 0 delivered in the last 3600s', line)
        self.assertNotIn('BURST', line)


# --- the control -----------------------------------------------------------

class TestPreFixControl(unittest.IsolatedAsyncioTestCase):
    """With the rate limit switched off, the same traffic reproduces the fault
    — one thread per message, at whatever rate the backlog drains at."""

    async def test_without_the_limit_a_backlog_opens_a_thread_per_message(self):
        b, channel, user, clock = threaded(above='0', ceiling='0')
        for n in range(1, 41):
            await deliver(b, user, n)
        self.assertEqual(len(channel.threads), 40,
                         'the control did not reproduce the unbounded rate')


class TestItMakesNoCausalClaim(unittest.TestCase):
    """mg-27e0 shipped a causal claim — that ~966 open threads were why the
    channel would not render — into a commit message, the README, the
    CHANGELOG, two source comments and a design doc. It was false, and
    correcting it took eight edits plus a regression test.

    This ticket's justification is that an unbounded creation RATE is a
    liability whatever broke the client — not that the burst broke it. mg-2ab2
    left a ~2h45m gap between the burst decaying and the channel being reported
    readable, and no server-side measurement closes it. So the same tripwire,
    on this ticket's prose: it may say a burst happened, because that was
    measured. It may not say what the burst did to a client.
    """

    #: Assembled rather than spelled, so this file can carry the tripwire
    #: without tripping it — the same trick `subject_for` plays on the
    #: duplication limit.
    FORBIDDEN = tuple(a + b for a, b in (
        ('unrender', 'able'),
        ('cannot ', 'render'),
        ('could not ', 'render'),
        ('would not ', 'render'),
        ('broke the ', 'client'),
        ('made the channel ', 'unreadable'),
    ))

    def _texts(self):
        yield 'bridget_core/burst.py', (REPO / 'bridget_core' / 'burst.py').read_text()
        script = (REPO / 'bridget').read_text()
        # Only this ticket's own region of the adapter. mg-27e0's retracted
        # wording is quoted elsewhere in the file — by the corrections that
        # retract it — and a whole-file grep would fire on those.
        start = script.index('def format_burst_fold')
        end = script.index('def format_relay_beat')
        yield 'bridget (mg-7dda region)', script[start:end]

    def test_no_shipped_prose_says_the_burst_broke_anything(self):
        offenders = []
        for name, text in self._texts():
            low = text.lower()
            offenders.extend(f'{name}: {p!r}' for p in self.FORBIDDEN if p in low)
        self.assertEqual(offenders, [],
                         f'a causal claim leaked back in: {offenders}')

    def test_the_tripwire_would_actually_fire(self):
        """Guard the guard. A phrase list that matches nothing is a test that
        passes for the wrong reason, which is how the claim survived eight
        files the first time."""
        low = 'the 971 threads made the channel unrender' + 'able'
        self.assertTrue(any(p in low for p in self.FORBIDDEN))

    def test_the_module_states_what_it_does_not_establish(self):
        """Saying nothing false is not the same as saying what is unknown. The
        gap has to be named where someone reading the fix will find it."""
        text = (REPO / 'bridget_core' / 'burst.py').read_text()
        self.assertIn('mg-2ab2', text)
        self.assertIn('unclosed', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
