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

"""The duplication limit (mg-5521).

Daniel: "stall-watch etc send me emails which becomes crazy annoying on discord
... bc they duplicate a lot, if there was some sort of duplication limit that
would be fine."

Two properties carry the whole feature, and both are load-bearing in a way that
is easy to get wrong:

  * digits normalise, so an alert whose fire count drifts 90/91/92 is ONE
    condition — but an mg-id does not, because two approvals for two different
    ids are two decisions and folding them would suppress the second;
  * a first occurrence is never delayed and never dropped, and a *failed* send
    is never counted as one — otherwise a transient Discord outage would be
    laundered into a silently swallowed alert by the very limit that exists to
    stop alerts being lost.

The adapter half re-runs the measured 2026-08-11 subject counts through
`deliver_mail` and asserts the flood actually shrinks, which is the number the
ticket asked to move.
"""
import asyncio
import datetime
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))

import testtmp  # noqa: E402

from bridget_core.ratelimit import (  # noqa: E402
    DuplicateLimiter,
    alert_key,
    claims_ancestry,
    normalize_subject,
)
from test_threading import FakeTextChannel, FakeUser, load_threaded, mail  # noqa: E402

# The subject counts the mayor measured in ~/.macguffin/mail/human/new/ on
# 2026-08-11, verbatim from the ticket. 109 messages; 8 rows; but the three
# ack-watch FLEET BLACKOUT rows are one condition whose fire count drifts.
MEASURED = [
    (31, 'hey-feed', '[hey/screener] 1 first-time sender(s) waiting — go look'),
    (17, 'doctor', 'AGENTS ARE FAILING EVERY TURN — mayor (server_error)'),
    (13, 'ack-watch',
     'ack-watch: FLEET BLACKOUT — 90 fires delivered in the last 3h0m0s, NONE completed'),
    (11, 'stall-watch', 'stall-watch: work piling up'),
    (10, 'watchdog', 'URGENT: watchdog flapping on com.pogo.gh-issues'),
    (9, 'deploy', 'nightly deploy HUNG on 2026-08-08 — one run took 31h43m to finish'),
    (9, 'ack-watch',
     'ack-watch: FLEET BLACKOUT — 92 fires delivered in the last 3h0m0s, NONE completed'),
    (9, 'ack-watch', 'ack-watch: 1 whole cohort(s) below the completion floor'),
    (5, 'ack-watch',
     'ack-watch: FLEET BLACKOUT — 91 fires delivered in the last 3h0m0s, NONE completed'),
]


class FakeClock:
    """A clock the tests drive, so a backoff measured in hours runs instantly."""

    def __init__(self, start=1_786_000_000.0):   # 2026-08-11, near enough
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def make_limiter(tmp: Path, clock=None, **kw):
    kw.setdefault('base_interval', 900)
    kw.setdefault('max_interval', 14400)
    kw.setdefault('ttl', 86400)
    return DuplicateLimiter(tmp / 'dedup.json', clock=clock, **kw)


def fire(limiter, sender, subject, conversation=''):
    """One firing of a condition, decided and then committed as delivered."""
    decision = limiter.decide(sender, subject)
    limiter.commit(decision, sender=sender, subject=subject,
                   conversation=conversation)
    return decision


# --- normalisation ---------------------------------------------------------

class TestNormalizeSubject(unittest.TestCase):
    def test_digit_runs_fold_to_N(self):
        self.assertEqual(normalize_subject('90 fires delivered'), 'n fires delivered')

    def test_the_three_ack_watch_rows_are_one_condition(self):
        """The measured trap: literal-subject dedup misses most of the volume."""
        keys = {
            alert_key(sender, subject)
            for _, sender, subject in MEASURED
            if 'FLEET BLACKOUT' in subject
        }
        self.assertEqual(len(keys), 1, f'fire-count drift split the condition: {keys}')

    def test_durations_and_dates_fold_too(self):
        a = normalize_subject('nightly deploy HUNG on 2026-08-08 — one run took 31h43m')
        b = normalize_subject('nightly deploy HUNG on 2026-08-09 — one run took 4h07m')
        self.assertEqual(a, b)

    def test_mg_ids_survive_normalisation(self):
        """An mg-id is identity, not drift. Folding it would suppress the
        second of two different approvals — the mail the limit has least right
        to hold back."""
        a = normalize_subject('approval needed mg-4fc0')
        b = normalize_subject('approval needed mg-9a13')
        self.assertNotEqual(a, b)
        self.assertIn('mg-4fc0', a)

    def test_an_all_digit_mg_id_is_still_an_id(self):
        self.assertIn('mg-5521', normalize_subject('polecat mg-5521 wedged'))

    def test_reply_prefixes_are_stripped(self):
        self.assertEqual(normalize_subject('Re: Re: work piling up'),
                         normalize_subject('work piling up'))

    def test_whitespace_and_case_fold(self):
        self.assertEqual(normalize_subject('  Work   Piling  Up '),
                         normalize_subject('work piling up'))

    def test_an_empty_subject_does_not_explode(self):
        self.assertEqual(normalize_subject(''), '')
        self.assertEqual(normalize_subject(None), '')


class TestAlertKey(unittest.TestCase):
    def test_the_sender_is_part_of_the_condition(self):
        """Two watchers reporting 'work piling up' are two conditions."""
        self.assertNotEqual(alert_key('stall-watch', 'work piling up'),
                            alert_key('ack-watch', 'work piling up'))

    def test_the_same_watcher_repeating_itself_is_one_condition(self):
        self.assertEqual(alert_key('stall-watch', 'work piling up'),
                         alert_key('Stall-Watch', 'Work Piling Up'))


class TestClaimsAncestry(unittest.TestCase):
    def test_a_broadcast_alert_claims_none(self):
        self.assertFalse(claims_ancestry({'in_reply_to': '', 'references': []}))

    def test_a_reply_claims_its_parent(self):
        self.assertTrue(claims_ancestry({'in_reply_to': 'id-1', 'references': []}))
        self.assertTrue(claims_ancestry({'in_reply_to': '', 'references': ['id-1']}))


# --- the limit itself ------------------------------------------------------

class TestFirstOccurrence(unittest.TestCase):
    def setUp(self):
        self.tmp = testtmp.mkdtemp('dedup')
        self.clock = FakeClock()
        self.limiter = make_limiter(self.tmp, clock=self.clock)

    def test_the_first_firing_is_always_delivered(self):
        d = self.limiter.decide('stall-watch', 'work piling up')
        self.assertTrue(d.deliver)
        self.assertEqual(d.kind, 'first')

    def test_it_is_delivered_with_no_delay(self):
        """Nothing is queued or held: the decision is available synchronously
        at the instant the mail is polled."""
        before = self.clock.now
        d = self.limiter.decide('stall-watch', 'work piling up')
        self.assertEqual(d.at, before)

    def test_every_distinct_condition_gets_its_own_first(self):
        """Distinct by *key*: the three FLEET BLACKOUT rows are one condition,
        so the second and third of them are repeats, not firsts."""
        seen = set()
        for _, sender, subject in MEASURED:
            key = alert_key(sender, subject)
            if key in seen:
                continue
            seen.add(key)
            self.assertTrue(fire(self.limiter, sender, subject).deliver,
                            f'{subject!r} was suppressed on its first firing')
        self.assertEqual(len(seen), 7, 'nine measured rows are seven conditions')

    def test_a_condition_quiet_past_the_ttl_is_news_again(self):
        fire(self.limiter, 'stall-watch', 'work piling up')
        self.clock.advance(86401)
        d = self.limiter.decide('stall-watch', 'work piling up')
        self.assertTrue(d.deliver)
        self.assertEqual(d.kind, 'first')
        self.assertEqual(d.delivered, 1, 'a new episode restarts the backoff')


class TestBackoff(unittest.TestCase):
    def setUp(self):
        self.tmp = testtmp.mkdtemp('dedup')
        self.clock = FakeClock()
        self.limiter = make_limiter(self.tmp, clock=self.clock)

    def test_an_immediate_repeat_is_suppressed(self):
        fire(self.limiter, 'stall-watch', 'work piling up')
        d = self.limiter.decide('stall-watch', 'work piling up')
        self.assertFalse(d.deliver)
        self.assertEqual(d.kind, 'suppressed')

    def test_the_window_reopens_after_the_base_interval(self):
        fire(self.limiter, 'stall-watch', 'work piling up')
        self.clock.advance(899)
        self.assertFalse(self.limiter.decide('stall-watch', 'work piling up').deliver)
        self.clock.advance(1)
        self.assertTrue(self.limiter.decide('stall-watch', 'work piling up').deliver)

    def test_each_notice_doubles_the_wait(self):
        self.assertEqual(self.limiter.interval_for(1), 900)
        self.assertEqual(self.limiter.interval_for(2), 1800)
        self.assertEqual(self.limiter.interval_for(3), 3600)
        self.assertEqual(self.limiter.interval_for(4), 7200)

    def test_the_backoff_is_capped(self):
        self.assertEqual(self.limiter.interval_for(5), 14400)
        self.assertEqual(self.limiter.interval_for(50), 14400,
                         'a long incident must still check in')

    def test_a_still_firing_condition_is_never_silenced_forever(self):
        """The cap is what keeps 'decaying' from becoming 'gone'."""
        fire(self.limiter, 'ack-watch', 'FLEET BLACKOUT — 90 fires')
        notices = 0
        for _ in range(24 * 60 // 5):          # a day of firings, every 5 min
            self.clock.advance(300)
            d = self.limiter.decide('ack-watch', 'FLEET BLACKOUT — 90 fires')
            self.limiter.commit(d, sender='ack-watch', subject='FLEET BLACKOUT')
            notices += bool(d.deliver)
        self.assertGreaterEqual(notices, 5, 'the condition went dark')

    def test_a_backwards_clock_delivers_rather_than_silences(self):
        """An NTP step must not park a condition in permanent suppression —
        that failure mode is the harm this module exists to prevent, wearing
        the fix's clothes."""
        fire(self.limiter, 'stall-watch', 'work piling up')
        self.clock.advance(-7200)
        self.assertTrue(self.limiter.decide('stall-watch', 'work piling up').deliver)


class TestSuppressionIsRecoverable(unittest.TestCase):
    def setUp(self):
        self.tmp = testtmp.mkdtemp('dedup')
        self.clock = FakeClock()
        self.limiter = make_limiter(self.tmp, clock=self.clock)

    def test_the_next_notice_carries_the_suppressed_count(self):
        fire(self.limiter, 'stall-watch', 'work piling up')
        for _ in range(11):
            fire(self.limiter, 'stall-watch', 'work piling up')
        self.clock.advance(901)
        d = self.limiter.decide('stall-watch', 'work piling up')
        self.assertTrue(d.deliver)
        self.assertEqual(d.suppressed_since, 11)
        self.assertEqual(d.delivered, 2, 'this is the second notice')

    def test_the_running_tally_survives_the_notice(self):
        for _ in range(6):
            fire(self.limiter, 'stall-watch', 'work piling up')
        self.clock.advance(901)
        fire(self.limiter, 'stall-watch', 'work piling up')
        summary = self.limiter.summary()
        self.assertEqual(summary['suppressed_total'], 5)
        self.assertEqual(summary['holding'], 0, 'the notice cleared the backlog')
        self.assertEqual(summary['top'][0]['occurrences'], 7)
        self.assertEqual(summary['top'][0]['delivered'], 2)

    def test_the_summary_names_the_condition_the_human_would_recognise(self):
        fire(self.limiter, 'stall-watch', 'stall-watch: work piling up')
        fire(self.limiter, 'stall-watch', 'stall-watch: work piling up')
        row = self.limiter.summary()['top'][0]
        self.assertEqual(row['sender'], 'stall-watch')
        self.assertEqual(row['subject'], 'stall-watch: work piling up')
        self.assertEqual(
            datetime.datetime.fromisoformat(row['first_seen']).timestamp(),
            self.clock.now,
            f'not the ISO form of the clock: {row["first_seen"]}')


class TestDeliveryIsCommittedNotAssumed(unittest.TestCase):
    """The at-least-once seam. `decide` must change nothing."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('dedup')
        self.clock = FakeClock()
        self.limiter = make_limiter(self.tmp, clock=self.clock)

    def test_deciding_alone_records_nothing(self):
        self.limiter.decide('stall-watch', 'work piling up')
        self.assertEqual(len(self.limiter), 0)
        self.assertFalse((self.tmp / 'dedup.json').exists())

    def test_an_uncommitted_delivery_is_still_a_first_occurrence(self):
        """A failed DM leaves the limiter untouched, so the watcher's retry is
        the first notice — not a repeat the limit would swallow."""
        self.limiter.decide('stall-watch', 'work piling up')   # send failed
        d = self.limiter.decide('stall-watch', 'work piling up')  # retry
        self.assertTrue(d.deliver)
        self.assertEqual(d.kind, 'first')

    def test_a_committed_suppression_is_counted(self):
        fire(self.limiter, 'stall-watch', 'work piling up')
        d = self.limiter.decide('stall-watch', 'work piling up')
        self.limiter.commit(d, sender='stall-watch', subject='work piling up')
        self.assertEqual(self.limiter.summary()['suppressed_total'], 1)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = testtmp.mkdtemp('dedup')
        self.clock = FakeClock()

    def test_a_restart_does_not_reopen_the_flood(self):
        limiter = make_limiter(self.tmp, clock=self.clock)
        fire(limiter, 'stall-watch', 'work piling up')
        reborn = make_limiter(self.tmp, clock=self.clock)
        self.assertFalse(reborn.decide('stall-watch', 'work piling up').deliver)

    def test_the_state_file_is_owner_only(self):
        limiter = make_limiter(self.tmp, clock=self.clock)
        fire(limiter, 'stall-watch', 'work piling up')
        self.assertEqual((self.tmp / 'dedup.json').stat().st_mode & 0o777, 0o600)

    def test_a_corrupt_state_file_fails_open(self):
        """Losing state costs one extra notification. Refusing to start, or
        starting with a suppression the file cannot justify, costs an alert."""
        (self.tmp / 'dedup.json').write_text('{ not json')
        limiter = make_limiter(self.tmp, clock=self.clock)
        self.assertEqual(len(limiter), 0)
        self.assertTrue(limiter.decide('stall-watch', 'work piling up').deliver)

    def test_state_growth_is_bounded(self):
        limiter = make_limiter(self.tmp, clock=self.clock, max_keys=10)
        for i in range(50):
            self.clock.advance(1)
            fire(limiter, 'noisy', f'condition {chr(97 + i % 26)}{i}')
        self.assertLessEqual(len(limiter), 10)

    def test_disabled_means_disabled(self):
        limiter = make_limiter(self.tmp, clock=self.clock, enabled=False)
        for _ in range(5):
            self.assertTrue(fire(limiter, 'stall-watch', 'work piling up').deliver)
        self.assertFalse((self.tmp / 'dedup.json').exists())

    def test_a_zero_window_disables_it_too(self):
        limiter = make_limiter(self.tmp, clock=self.clock, base_interval=0)
        self.assertFalse(limiter.enabled)
        self.assertTrue(fire(limiter, 'stall-watch', 'work piling up').deliver)


class TestTheMeasuredFlood(unittest.TestCase):
    """The number the ticket asked to move, replayed through the limiter."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('dedup')
        self.clock = FakeClock()
        self.limiter = make_limiter(self.tmp, clock=self.clock)

    def _replay(self, spacing):
        """Every measured firing, interleaved, `spacing` seconds apart."""
        rows = [[sender, subject, count] for count, sender, subject in MEASURED]
        notices = 0
        total = 0
        while any(r[2] for r in rows):
            for row in rows:
                if not row[2]:
                    continue
                row[2] -= 1
                total += 1
                self.clock.advance(spacing)
                notices += 1 if fire(self.limiter, row[0], row[1]).deliver else 0
        return total, notices

    def test_the_flood_shrinks(self):
        total, notices = self._replay(spacing=60)
        self.assertEqual(total, 114, 'replayed the measured counts')
        self.assertLess(notices, total // 3,
                        f'{notices} notices for {total} firings is not a limit')

    def test_every_condition_still_gets_through_at_least_once(self):
        self._replay(spacing=60)
        rows = self.limiter.summary(limit=50)['top']
        self.assertTrue(all(r['delivered'] >= 1 for r in rows),
                        'a condition was silenced entirely')

    def test_nothing_suppressed_is_uncounted(self):
        total, notices = self._replay(spacing=60)
        summary = self.limiter.summary(limit=50)
        accounted = notices + summary['suppressed_total']
        self.assertEqual(accounted, total,
                         'firings went missing between delivered and suppressed')


# --- the adapter half ------------------------------------------------------

class AsyncTestCase(unittest.TestCase):
    """The suite predates IsolatedAsyncioTestCase in this repo's style; the
    other adapter suites drive the loop by hand, so this one matches them."""

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name.startswith('test_') and asyncio.iscoroutinefunction(attr):
            return lambda *a, **kw: asyncio.run(attr(*a, **kw))
        return attr


class DeliverMailCase(AsyncTestCase):
    def setUp(self):
        self.b = load_threaded()
        self.channel = FakeTextChannel(555, client=self.b.client)
        self.b.client.channels[555] = self.channel
        self.user = FakeUser()
        self.clock = FakeClock()
        # Same limiter, on the bridge's own state path, but with a clock the
        # test drives — otherwise a 15-minute backoff takes 15 minutes.
        self.b.DEDUP = DuplicateLimiter(self.b.DEDUP_FILE, clock=self.clock)

    async def deliver(self, filename, subject, sender='ack-watch', **kw):
        return await self.b.deliver_mail(
            self.user, filename, mail(subject=subject, sender=sender,
                                      msg_id=filename, **kw))


class TestDeliverMailAppliesTheLimit(DeliverMailCase):
    def test_the_limiter_is_on_by_default(self):
        self.assertTrue(self.b.DEDUP.enabled)
        self.assertEqual(self.b.CONFIG['dedup_window'], 900)

    async def test_the_first_alert_reaches_both_surfaces(self):
        await self.deliver('f1', 'stall-watch: work piling up')
        self.assertEqual(len(self.channel.threads), 1)
        self.assertEqual(len(self.user.sent), 1)

    async def test_a_repeat_reaches_neither(self):
        """31 root cards in the log channel is the same flood as 31 DMs."""
        await self.deliver('f1', 'stall-watch: work piling up')
        await self.deliver('f2', 'stall-watch: work piling up')
        self.assertEqual(len(self.channel.threads), 1)
        self.assertEqual(len(self.user.sent), 1)

    async def test_a_suppressed_repeat_is_committed_not_retried(self):
        """It must return True, or the watcher re-offers it every poll."""
        await self.deliver('f1', 'stall-watch: work piling up')
        self.assertTrue(await self.deliver('f2', 'stall-watch: work piling up'))

    async def test_the_fire_count_drift_does_not_defeat_it(self):
        for i, count in enumerate((90, 91, 92, 90, 93)):
            await self.deliver(
                f'f{i}',
                f'ack-watch: FLEET BLACKOUT — {count} fires delivered in the '
                f'last 3h0m0s, NONE completed')
        self.assertEqual(len(self.user.sent), 1,
                         'literal-subject dedup would have sent five')

    async def test_the_backoff_lets_a_still_firing_condition_through(self):
        await self.deliver('f1', 'stall-watch: work piling up')
        for i in range(9):
            await self.deliver(f'r{i}', 'stall-watch: work piling up')
        self.clock.advance(901)
        await self.deliver('f2', 'stall-watch: work piling up')
        self.assertEqual(len(self.user.sent), 2)
        self.assertIn('still happening', self.user.sent[1])
        self.assertIn('9 repeats suppressed', self.user.sent[1])

    async def test_the_notice_folds_into_the_thread_it_already_opened(self):
        await self.deliver('f1', 'stall-watch: work piling up')
        self.clock.advance(901)
        await self.deliver('f2', 'stall-watch: work piling up')
        self.assertEqual(len(self.channel.threads), 1,
                         'one condition should own one thread')
        self.assertIn('still happening', self.channel.threads[0].sent[-1])

    async def test_distinct_conditions_are_untouched(self):
        await self.deliver('f1', 'stall-watch: work piling up')
        await self.deliver('f2', 'URGENT: watchdog flapping on com.pogo.gh-issues',
                           sender='watchdog')
        self.assertEqual(len(self.user.sent), 2)

    async def test_two_approvals_for_two_ids_both_arrive(self):
        """Digit folding must not collapse mg-ids: the second approval is a
        different decision waiting on the human, not a duplicate."""
        await self.deliver('f1', 'approval needed mg-4fc0', sender='architect')
        await self.deliver('f2', 'approval needed mg-9a13', sender='architect')
        self.assertEqual(len(self.user.sent), 2)

    async def test_a_thread_reply_is_never_rate_limited(self):
        """A reply normalises to the same key as the mail it answers. Rate
        limiting it would silence a live conversation, not a watcher."""
        await self.deliver('f1', 'design review')
        for i in range(4):
            await self.b.deliver_mail(
                self.user, f'r{i}',
                mail(subject='Re: design review', sender='ack-watch',
                     msg_id=f'r{i}', in_reply_to='f1', refs='f1'))
        self.assertEqual(len(self.user.sent), 5, 'a reply was swallowed')
        self.assertEqual(len(self.channel.threads), 1)

    async def test_a_failed_dm_is_retried_as_a_first_occurrence(self):
        """The defect this limit remedies, arriving by the back door: a limiter
        that counted a failed send as delivered would suppress its retry."""
        boom = self.b.discord.HTTPException('rate limited')

        async def explode(_content):
            raise boom

        self.user.send = explode
        self.assertFalse(await self.deliver('f1', 'stall-watch: work piling up'))

        self.user.send = FakeUser.send.__get__(self.user)
        self.assertTrue(await self.deliver('f1', 'stall-watch: work piling up'))
        self.assertEqual(len(self.user.sent), 1, 'the retry was suppressed')

    async def test_a_quiet_period_makes_the_condition_news_again(self):
        await self.deliver('f1', 'stall-watch: work piling up')
        self.clock.advance(86401)
        await self.deliver('f2', 'stall-watch: work piling up')
        self.assertEqual(len(self.user.sent), 2)
        self.assertNotIn('still happening', self.user.sent[1],
                         'a new episode is a first occurrence, not a repeat')

    async def test_the_measured_flood_shrinks_end_to_end(self):
        n = 0
        for count, sender, subject in MEASURED:
            for _ in range(count):
                n += 1
                self.clock.advance(60)
                await self.deliver(f'm{n}', subject, sender=sender)
        self.assertEqual(n, 114)
        self.assertLess(len(self.user.sent), 30,
                        f'{len(self.user.sent)} DMs for {n} alerts')
        self.assertLessEqual(len(self.channel.threads), 7,
                             'one thread per condition, not per firing')


class TestSuppressionLeavesATrace(DeliverMailCase):
    async def test_the_log_names_the_suppressed_mail(self):
        from contextlib import redirect_stdout
        import io

        await self.deliver('f1', 'stall-watch: work piling up')
        buf = io.StringIO()
        with redirect_stdout(buf):
            await self.deliver('f2', 'stall-watch: work piling up')
        line = buf.getvalue()
        self.assertIn('dedup: suppressed repeat', line)
        self.assertIn('work piling up', line)
        self.assertIn('f2', line, 'the maildir filename must be greppable')

    async def test_dupes_reports_the_standing_tally(self):
        await self.deliver('f1', 'stall-watch: work piling up')
        for i in range(4):
            await self.deliver(f'r{i}', 'stall-watch: work piling up')
        out = self.b.handle_command('dupes')
        self.assertIn('work piling up', out)
        self.assertIn('suppressed 4', out)

    async def test_status_surfaces_the_count(self):
        await self.deliver('f1', 'stall-watch: work piling up')
        await self.deliver('f2', 'stall-watch: work piling up')
        self.assertIn('Duplicate alerts suppressed', self.b.get_status_summary())

    async def test_settings_says_the_limit_is_on(self):
        self.assertIn('Duplicate limit: `on`', self.b.handle_command('settings'))

    def test_dupes_is_in_the_help(self):
        self.assertIn('`dupes`', self.b.COMMAND_LIST)


class TestConfig(unittest.TestCase):
    def test_a_zero_window_switches_the_limit_off(self):
        b = load_threaded(BRIDGET_DEDUP_WINDOW='0')
        self.assertFalse(b.DEDUP.enabled)
        self.assertIn('Duplicate limit: `off`', b.handle_command('settings'))

    def test_a_non_integer_window_is_refused(self):
        with self.assertRaises(SystemExit):
            load_threaded(BRIDGET_DEDUP_WINDOW='fifteen-ish')

    def test_a_negative_window_is_refused(self):
        with self.assertRaises(SystemExit):
            load_threaded(BRIDGET_DEDUP_WINDOW='-1')

    def test_a_cap_below_the_window_is_refused(self):
        with self.assertRaises(SystemExit):
            load_threaded(BRIDGET_DEDUP_WINDOW='900', BRIDGET_DEDUP_MAX_WINDOW='60')

    def test_a_ttl_below_the_window_is_refused(self):
        """It would expire every condition before its next notice was due, so
        the limit would silently do nothing."""
        with self.assertRaises(SystemExit):
            load_threaded(BRIDGET_DEDUP_WINDOW='900', BRIDGET_DEDUP_TTL='60')

    def test_the_env_file_can_carry_the_keys(self):
        b = load_threaded()
        self.assertEqual(b.CONFIG['dedup_max_window'], 14400)
        self.assertEqual(b.CONFIG['dedup_ttl'], 86400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
