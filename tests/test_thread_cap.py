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

"""The live-thread bound (mg-27e0).

bridget opened one Discord thread per conversation and closed none. 966 of them
accumulated in a single channel — every one with `auto_archive_duration=1440`,
every one still active, because this fleet touches enough conversations often
enough that a large population never goes quiet for a whole day. An unbounded
generator pointed at one channel is worth capping on its own merits.

mg-27e0 additionally blamed that population for Daniel's client failing to
render #log on 2026-08-19. mg-2ab2 refuted it: the count was 971 and RISING when
the channel became readable again, with nothing archived. The bound is hygiene,
not the remedy for that incident — see docs/thread-render-forensics.md. These
tests pin the bound's behaviour and make no claim about the render failure.

The generator is what is fixed here, not the backlog. These tests pin:

  * the cap holds at CREATION and at WAKE — the two ways a thread enters the
    open set. Bounding only creation leaves the wake path unbounded, and a
    long-running bridge wakes far more threads than it opens.
  * eviction is LEAST-RECENTLY-USED, so the conversation the human is actually
    in is the last one closed.
  * the bound is a BOUND, not a sweep — it runs before the thread that would
    breach the cap, so a burst cannot outrun it, and concurrent deliveries
    cannot each take the same last slot.
  * a restart does not re-inflate the population: the open set is persisted, and
    restore admits nothing.
  * archiving is not deleting — an evicted conversation's next mail reopens its
    own thread, in its own place, and nothing is lost.
  * the count is stated against the cap, everywhere it is stated. The old line
    printed a bare total that rose 1120 -> 1139 -> 1147 across three restarts in
    one morning and read as a statistic to everyone who saw it.

It also carries a PRE-FIX CONTROL: with the cap switched off, the same traffic
reproduces the unbounded population.
"""
import asyncio
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'tests'))

from test_threading import (  # noqa: E402
    FakeTextChannel,
    FakeUser,
    load_threaded,
    mail,
)


def threaded(cap='3', **kw):
    """A bridget with threading on and the live-thread cap set."""
    b = load_threaded(BRIDGET_MAX_LIVE_THREADS=cap, **kw)
    channel = FakeTextChannel(555, client=b.client)
    b.client.channels[555] = channel
    return b, channel, FakeUser()


def subject_for(n: int) -> str:
    """A distinct subject per mail, spelled in letters rather than digits.

    Deliberately not "s1", "s2", ...: the duplication limit (mg-5521)
    normalises digits, so numbered subjects are ONE condition to it and get
    folded into a single conversation — which would leave every test here
    measuring a cap against a population of one.
    """
    return 'topic ' + ''.join(chr(ord('a') + int(d)) for d in str(n))


async def deliver(b, user, n, subject=None):
    """One unrelated mail, rooting its own conversation."""
    await b.deliver_mail(user, f'f{n}', mail(subject=subject or subject_for(n),
                                             msg_id=f'id-{n}'))


def open_threads(channel):
    return [t for t in channel.threads if not t.archived]


# --- the bound holds at creation -------------------------------------------

class TestCapAtCreation(unittest.IsolatedAsyncioTestCase):

    async def test_the_open_population_never_exceeds_the_cap(self):
        b, channel, user = threaded(cap='3')
        for n in range(1, 21):
            await deliver(b, user, n)
        self.assertEqual(len(channel.threads), 20,
                         'every conversation should still get its own thread')
        self.assertEqual(len(open_threads(channel)), 3,
                         'the open population broke the cap')
        self.assertEqual(b.CONVERSATIONS.live_count(), 3)

    async def test_conversations_stay_unbounded_while_threads_do_not(self):
        """The cap is on what the client has to RENDER, not on what bridget
        remembers. Forgetting a conversation would re-root its thread on the
        next reply; forgetting to close a thread is what broke the channel."""
        b, channel, user = threaded(cap='2')
        for n in range(1, 11):
            await deliver(b, user, n)
        self.assertEqual(len(b.CONVERSATIONS), 10)
        self.assertEqual(b.CONVERSATIONS.live_count(), 2)

    async def test_eviction_is_least_recently_used(self):
        """Also the regression test for a defect in this fix's first cut. The
        eviction queue was ordered by `updated_at`, which is second-granular —
        so inside any one second it collapsed to alphabetical, and a burst is
        both when the queue is consulted and when every entry shares a second.
        These three conversations are created in well under a second, and an
        LRU that is really alphabetical evicts the wrong one."""
        b, channel, user = threaded(cap='2')
        await deliver(b, user, 1)
        await deliver(b, user, 2)
        # Touch conversation 1 again — now 2 is the least recently used.
        await b.deliver_mail(user, 'f1b', mail(subject=subject_for(1), msg_id='id-1b',
                                               in_reply_to='id-1', refs='id-1'))
        await deliver(b, user, 3)
        by_key = {c.key: c for c in b.CONVERSATIONS.values()}
        self.assertFalse(by_key['id-2'].thread_live, 'the LRU thread was not evicted')
        self.assertTrue(by_key['id-1'].thread_live, 'the recently-used thread was evicted')
        self.assertTrue(by_key['id-3'].thread_live)

    async def test_the_new_thread_is_never_its_own_victim(self):
        """`make_room_for_thread` excludes the conversation being admitted. If
        it did not, a cap of 1 would archive the thread it had just opened and
        the mail would land somewhere the human cannot see."""
        b, channel, user = threaded(cap='1')
        await deliver(b, user, 1)
        await deliver(b, user, 2)
        self.assertEqual(len(open_threads(channel)), 1)
        self.assertEqual(open_threads(channel)[0].id, channel.threads[-1].id)
        self.assertEqual(len(channel.threads[-1].sent), 1,
                         'the mail did not reach the thread it opened')

    async def test_room_is_made_before_the_thread_is_opened(self):
        """Order matters: evict, then create. The other order opens the thread
        that breaches the cap and only afterwards goes looking for space, which
        is a cap that is briefly always wrong — and under a burst, wrong at
        exactly the moment the channel is being flooded."""
        b, channel, user = threaded(cap='2')
        seen = []
        real_create = b.make_room_for_thread

        async def spy(key):
            seen.append(len(channel.threads))
            await real_create(key)
        b.make_room_for_thread = spy
        await deliver(b, user, 1)
        await deliver(b, user, 2)
        await deliver(b, user, 3)
        # Each call saw the thread count from BEFORE its own create.
        self.assertEqual(seen, [0, 1, 2])


# --- ...and at wake, which is the path a bound at creation alone misses -----

class TestCapAtWake(unittest.IsolatedAsyncioTestCase):

    async def test_waking_an_archived_thread_goes_through_the_bound(self):
        """Discord archives a thread on its own idle timer. The next mail in
        that conversation must reopen it — and that reopening is an entry into
        the open set, so it has to make room like any other."""
        b, channel, user = threaded(cap='2')
        await deliver(b, user, 1)
        await deliver(b, user, 2)
        first = channel.threads[0]
        # Discord retires it behind our back.
        first.archived = True
        b.CONVERSATIONS.mark_thread_archived('id-1')
        await deliver(b, user, 3)
        self.assertEqual(len(open_threads(channel)), 2)
        # Now conversation 1 speaks again: it wakes, and something else closes.
        await b.deliver_mail(user, 'f1b', mail(subject=subject_for(1), msg_id='id-1b',
                                               in_reply_to='id-1', refs='id-1'))
        self.assertFalse(first.archived, 'the woken thread was not reopened')
        self.assertEqual(len(open_threads(channel)), 2,
                         'a wake broke the cap the creation path holds')

    async def test_a_thread_open_but_unaccounted_for_is_charged_a_slot(self):
        """The upgrade path. 966 threads were already open when this shipped;
        the store that describes them has no live flags, so they load as not
        open. Each one is charged for its slot the moment it is next used —
        which is how the pre-existing population is drawn into the bound
        through use, instead of by a mass archive nobody asked for."""
        b, channel, user = threaded(cap='2')
        await deliver(b, user, 1)
        await deliver(b, user, 2)
        # Simulate the v2 file: threads open on Discord, no flag here.
        b.CONVERSATIONS.mark_threads_archived(['id-1', 'id-2'])
        self.assertEqual(b.CONVERSATIONS.live_count(), 0)
        self.assertEqual(len(open_threads(channel)), 2)
        # Conversation 1 is used again. It is open, so it takes a slot — and at
        # a cap of 2 that is fine; the second use is what forces an eviction.
        await b.deliver_mail(user, 'f1b', mail(subject=subject_for(1), msg_id='id-1b',
                                               in_reply_to='id-1', refs='id-1'))
        self.assertEqual(b.CONVERSATIONS.live_count(), 1)
        await deliver(b, user, 3)
        await deliver(b, user, 4)
        self.assertEqual(b.CONVERSATIONS.live_count(), 2)
        self.assertLessEqual(len(open_threads(channel)), 3,
                             'the grandfathered population never drained')


# --- a bound, not a sweep --------------------------------------------------

class TestItIsABoundNotASweep(unittest.IsolatedAsyncioTestCase):

    async def test_a_concurrent_burst_cannot_take_the_same_slot_twice(self):
        """The check-then-open race. Several delivery watchers run at once and
        every one of them awaits inside the check; without THREAD_ADMISSION_LOCK
        N coroutines each read "one slot free" and all N take it — which is the
        same unbounded-under-burst shape as the defect being fixed. That is
        exactly why the remedy gets checked for it."""
        b, channel, user = threaded(cap='3')
        await asyncio.gather(*[deliver(b, user, n) for n in range(1, 31)])
        self.assertEqual(len(channel.threads), 30)
        self.assertEqual(len(open_threads(channel)), 3,
                         'a concurrent burst outran the bound')

    async def test_an_over_cap_start_drains_without_a_mass_archive(self):
        """Lowering the cap (or meeting a backlog) must not fire hundreds of
        archive calls in one burst — mg-27e0 puts mass-archiving explicitly out
        of scope. The batch is bounded, the set still drains, and the log says
        how far over it still is."""
        b, channel, user = threaded(cap='20', BRIDGET_THREAD_EVICTION_BATCH='3')
        for n in range(1, 21):
            await deliver(b, user, n)
        self.assertEqual(b.CONVERSATIONS.live_count(), 20)
        b.MAX_LIVE_THREADS = 2
        before = sum(t.archive_calls for t in channel.threads)
        await deliver(b, user, 21)
        after = sum(t.archive_calls for t in channel.threads)
        self.assertEqual(after - before, 3, 'the eviction batch was not bounded')
        # ...and it keeps draining, 2 net per admission, rather than stalling.
        for n in range(22, 32):
            await deliver(b, user, n)
        self.assertLessEqual(b.CONVERSATIONS.live_count(), 2,
                             'the over-cap population never drained to the cap')

    async def test_a_refused_archive_costs_a_slot_and_not_the_mail(self):
        """A thread that will not archive is one slot over the cap. Refusing to
        open the new one instead would cost the human the message, which is the
        wrong way round — and the refusal is printed, not swallowed."""
        b, channel, user = threaded(cap='1')
        await deliver(b, user, 1)
        channel.threads[0].refuse_archive = True
        await deliver(b, user, 2)
        self.assertEqual(len(channel.threads), 2)
        self.assertEqual(len(channel.threads[1].sent), 1,
                         'the mail was dropped because an eviction failed')
        self.assertTrue(b.CONVERSATIONS.get('id-1').thread_live,
                        'a thread that would not archive was recorded as closed')

    async def test_a_deleted_thread_does_not_wedge_the_bound(self):
        """A thread the human deleted holds no slot. If eviction could not free
        it, the bound would jam on a thread that does not exist."""
        b, channel, user = threaded(cap='1')
        await deliver(b, user, 1)
        gone = channel.threads[0]
        b.client.channels.pop(gone.id, None)
        await deliver(b, user, 2)
        self.assertFalse(b.CONVERSATIONS.get('id-1').thread_live)
        self.assertEqual(b.CONVERSATIONS.live_count(), 1)


# --- the restart question the ticket asked ---------------------------------

class TestRestartCannotReInflate(unittest.IsolatedAsyncioTestCase):
    """"A bound only at creation lets a restore re-inflate past it" — mg-27e0.

    It does not, because restore ADMITS nothing. The open set is persisted, so
    the next process starts from the same count the last one enforced, and the
    only paths that add to it are the two that go through the bound.
    """

    async def test_the_open_set_survives_a_restart(self):
        b, channel, user = threaded(cap='3')
        for n in range(1, 11):
            await deliver(b, user, n)
        self.assertEqual(b.CONVERSATIONS.live_count(), 3)
        reborn = b.ConversationStore(b.CONVERSATIONS_FILE)
        self.assertEqual(reborn.live_count(), 3,
                         'a restart forgot which threads were open')
        self.assertEqual(len(reborn), 10)

    async def test_restore_does_not_open_anything(self):
        b, channel, user = threaded(cap='3')
        for n in range(1, 11):
            await deliver(b, user, n)
        opened = len(channel.threads)
        b.ConversationStore(b.CONVERSATIONS_FILE)
        self.assertEqual(len(channel.threads), opened)

    async def test_an_older_store_loads_with_nothing_charged(self):
        """A v2 file has no live flags. Every entry reads as closed, so the
        bound starts from zero and the 966 already on the server are left for
        the separate, reversible cleanup the ticket reserves to Daniel."""
        b, channel, user = threaded(cap='3')
        for n in range(1, 5):
            await deliver(b, user, n)
        import json
        raw = json.loads(Path(b.CONVERSATIONS_FILE).read_text())
        for entry in raw['conversations'].values():
            entry.pop('thread_live', None)
        raw['version'] = 2
        Path(b.CONVERSATIONS_FILE).write_text(json.dumps(raw))
        reborn = b.ConversationStore(b.CONVERSATIONS_FILE)
        self.assertEqual(reborn.live_count(), 0)
        self.assertEqual(len(reborn), 4)

    async def test_a_live_flag_without_a_thread_is_not_a_slot(self):
        """A flag left behind by a deleted thread would hold a slot nothing
        occupies, and the bound would shrink by one on every restart."""
        b, channel, user = threaded(cap='3')
        await deliver(b, user, 1)
        import json
        path = Path(b.CONVERSATIONS_FILE)
        raw = json.loads(path.read_text())
        raw['conversations']['id-1']['thread_id'] = None
        path.write_text(json.dumps(raw))
        reborn = b.ConversationStore(path)
        self.assertEqual(reborn.live_count(), 0)


# --- archiving is not deleting ---------------------------------------------

class TestEvictionIsReversible(unittest.IsolatedAsyncioTestCase):

    async def test_an_evicted_conversation_reopens_its_own_thread(self):
        b, channel, user = threaded(cap='1')
        await deliver(b, user, 1)
        first = channel.threads[0]
        await deliver(b, user, 2)
        self.assertTrue(first.archived)
        await b.deliver_mail(user, 'f1b', mail(subject=subject_for(1), msg_id='id-1b',
                                               in_reply_to='id-1', refs='id-1'))
        self.assertEqual(len(channel.threads), 2,
                         'an evicted conversation rooted a duplicate thread')
        self.assertFalse(first.archived)
        self.assertEqual(len(first.sent), 2,
                         'the reply did not land in the original thread')

    async def test_eviction_archives_and_never_deletes(self):
        b, channel, user = threaded(cap='1')
        await deliver(b, user, 1)
        await deliver(b, user, 2)
        self.assertEqual(channel.threads[0].archive_calls, 1)
        self.assertEqual(len(channel.threads[0].sent), 1,
                         'eviction lost what was in the thread')


# --- the idle timer: shorter, not longer -----------------------------------

class TestIdleArchiveDuration(unittest.IsolatedAsyncioTestCase):

    async def test_new_threads_retire_after_an_hour_by_default(self):
        """Daniel's instinct on seeing the flood was "threads should expire
        after 2 days of inactivity". Every one of the 966 already carried 1440
        minutes; 2 days (Discord's nearest option is 4320) keeps each thread
        open LONGER and grows the population. 60 is the direction that shrinks
        it. Still only a mitigation — creation outruns archival at any
        duration, which is what the cap is for."""
        b, channel, user = threaded()
        await deliver(b, user, 1)
        self.assertEqual(b.THREAD_ARCHIVE_MINUTES, 60)
        self.assertEqual(channel.threads[0].auto_archive_duration, 60)

    async def test_the_duration_is_configurable_within_discords_choices(self):
        b, channel, user = threaded(BRIDGET_THREAD_ARCHIVE_MINUTES='1440')
        await deliver(b, user, 1)
        self.assertEqual(channel.threads[0].auto_archive_duration, 1440)

    def test_a_value_discord_will_not_accept_is_refused_at_startup(self):
        """2 days is not one of Discord's four options, so it would be
        rejected by the API at thread-creation time — one failed thread per
        mail, at delivery time, in a log. Refuse it where it is typed."""
        with self.assertRaises(SystemExit):
            load_threaded(BRIDGET_THREAD_ARCHIVE_MINUTES='2880')


# --- say the count out loud ------------------------------------------------

class TestTheCountIsStatedAgainstItsBound(unittest.IsolatedAsyncioTestCase):
    """mg-27e0's fourth scope item. bridget printed "N conversation(s)
    restored" at every startup — 1120, then 1139, then 1147 across three
    restarts in one morning — and nobody read it as an alarm, because a total
    with nothing to measure it against is a statistic."""

    async def test_the_status_names_the_cap_and_the_proximity(self):
        b, channel, user = threaded(cap='4')
        await deliver(b, user, 1)
        line = b.thread_cap_status()
        self.assertIn('1/4', line)
        self.assertIn('%', line)

    async def test_nearing_the_cap_reads_as_a_warning(self):
        b, channel, user = threaded(cap='4')
        for n in range(1, 5):
            await deliver(b, user, n)
        self.assertIn('near cap', b.thread_cap_status())

    async def test_over_the_cap_says_so_and_says_how_it_drains(self):
        b, channel, user = threaded(cap='4')
        for n in range(1, 5):
            await deliver(b, user, n)
        b.MAX_LIVE_THREADS = 2
        line = b.thread_cap_status()
        self.assertIn('OVER CAP', line)
        self.assertIn('draining', line)

    async def test_the_upgrade_run_names_the_threads_it_is_not_counting(self):
        """A v2 file's threads are open on Discord and charged to nobody. A
        status line that said "0/50 open" while ~1000 threads it did not open
        crowded the channel would be the same unreadable instrument this ticket
        is about, so the upgrade run states the number it cannot account for."""
        b, channel, user = threaded(cap='50')
        for n in range(1, 5):
            await deliver(b, user, n)
        import json
        path = Path(b.CONVERSATIONS_FILE)
        raw = json.loads(path.read_text())
        for entry in raw['conversations'].values():
            entry.pop('thread_live', None)
        raw['version'] = 2
        path.write_text(json.dumps(raw))
        b.CONVERSATIONS = b.ConversationStore(path)
        self.assertEqual(b.CONVERSATIONS.legacy_thread_count, 4)
        line = b.thread_cap_status()
        self.assertIn('0/50', line)
        self.assertIn('up to 4 more', line)

    async def test_a_current_store_names_no_uncounted_threads(self):
        """The upgrade note must appear on exactly one run. A permanent
        footnote about a backlog that has since drained is noise, and noise is
        what stopped the old startup line being read."""
        b, channel, user = threaded(cap='50')
        for n in range(1, 5):
            await deliver(b, user, n)
        reborn = b.ConversationStore(b.CONVERSATIONS_FILE)
        self.assertEqual(reborn.legacy_thread_count, 0)
        self.assertNotIn('opened before the bound', b.thread_cap_status())

    async def test_an_unbounded_build_says_it_is_the_fault_condition(self):
        b, channel, user = threaded(cap='0')
        for n in range(1, 11):
            await deliver(b, user, n)
        line = b.thread_cap_status()
        self.assertIn('UNBOUNDED', line)
        self.assertIn('mg-27e0', line)

    async def test_the_unbounded_warning_does_not_re_assert_the_refuted_cause(self):
        """The warning may say the population grows without limit. It may NOT
        say a client cannot render it.

        mg-27e0 shipped "a channel cannot render ~1000 open threads" in this
        very line. mg-2ab2 measured 971 rendering fine, so the line was stating
        a refuted claim to every operator who ran with the bound off. Pinned
        here because a warning string is exactly the kind of prose that gets
        re-embellished later by someone reaching for urgency, and the number it
        reached for last time was wrong.
        """
        b, channel, user = threaded(cap='0')
        for n in range(1, 11):
            await deliver(b, user, n)
        line = b.thread_cap_status().lower()
        self.assertNotIn('cannot render', line)
        self.assertNotIn('will not render', line)
        self.assertNotIn('unrenderable', line)

    async def test_the_settings_command_shows_it_too(self):
        b, channel, user = threaded(cap='4')
        await deliver(b, user, 1)
        out = b.handle_command('settings')
        self.assertIn('Open threads', out)
        self.assertIn('1/4', out)
        self.assertIn('Idle archive', out)


# --- the control -----------------------------------------------------------

class TestPreFixControl(unittest.IsolatedAsyncioTestCase):
    """With the bound switched off, the same traffic reproduces the fault.

    Without this, every assertion above could pass against a workload that was
    never going to breach a cap in the first place.
    """

    async def test_without_the_cap_the_population_is_unbounded(self):
        b, channel, user = threaded(cap='0')
        for n in range(1, 41):
            await deliver(b, user, n)
        self.assertEqual(len(open_threads(channel)), 40,
                         'the control did not reproduce the unbounded population')
        self.assertEqual(sum(t.archive_calls for t in channel.threads), 0)

    async def test_with_the_cap_the_same_traffic_is_bounded(self):
        b, channel, user = threaded(cap='5')
        for n in range(1, 41):
            await deliver(b, user, n)
        self.assertEqual(len(open_threads(channel)), 5)


if __name__ == '__main__':
    unittest.main(verbosity=2)
