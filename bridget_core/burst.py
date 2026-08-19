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

# bridget_core.burst — a rate limit on thread CREATION, and backlog coalescing.
"""A ceiling on how fast conversations may open threads, and what to do instead.

`ConversationStore` and the adapter's `make_room_for_thread` bound the number
of threads held OPEN at once (mg-27e0). This module bounds a different
quantity: how many are opened per unit time. **They are different quantities
and a bound on one is not a bound on the other** — mg-2ab2 measured the
standing population at 966 -> 968 -> 967 -> 971, rising monotonically, across
the window in which Daniel's client went from unreadable to readable, so the
population was not the variable. What the same window did contain was a rate:

    06:49Z   23      thread creations per minute, from
    06:50Z   27      thread_metadata.create_timestamp
    06:56Z   27      (mg-2ab2, docs/thread-render-forensics.md)
    06:59Z   22
    07:02Z    2      <- decayed
    ...
    122 threads in the 06:00Z hour; 36 in the whole rest of the day.

A 71h28m DNS outage had queued the fleet's mail, and connectivity returning
flushed it. mg-879c measured the same flush on the DM side: 171 messages into
one DM in under five minutes.

**This module is hygiene, not a diagnosis.** It is NOT established that the
burst is what stopped the client rendering — mg-2ab2 is explicit that a ~2h45m
gap between the burst decaying (~07:00Z) and Daniel reporting the channel
readable (09:45Z) is unclosed, and that server-side data cannot see a client
reload. An unbounded thread-creation rate is worth bounding whatever broke the
client that morning, in the same way and for the same reason the standing bound
was worth having. Nothing here should be read as the fix for that incident.

Daniel's own words on seeing the flood were *"another instance of needing
better deduplication in the bridge"*, and this is the deduplication he meant.
The duplication limit that already exists (`bridget_core.ratelimit`) folds
REPEATS of one condition — same sender, same subject once digits are
normalised. It was running that morning and suppressing repeats; the flood went
through anyway, because a drained backlog is 122 DIFFERENT subjects. So the
axis this module folds on is the correspondent, not the condition.

## The three states

The limiter counts creations in a trailing window and puts each new
conversation into one of three states:

    create      the rate is under `coalesce_above`. Nothing changes: the
                conversation opens its own thread, exactly as before. This is
                the state essentially all traffic is in — 36 threads across the
                whole rest of that day is a rate of ~0.03/min.

    coalesce    the rate is at or over `coalesce_above` and this correspondent
                already has a recent thread. The mail is filed into THAT
                conversation instead of opening a second one. Same
                correspondent, same episode, one thread — which is what turns
                122 threads from ~8 senders into ~8.

    over        the rate is at or over `ceiling` and there is nothing to fold
                into. No thread is opened at all; the caller delivers the mail
                by its other surface and says why. This is the branch that
                makes the ceiling a ceiling rather than a hope: coalescing
                reduces the creation rate but does not bound it, because N
                first-time correspondents in one window are N threads.

Nothing is ever dropped and nothing is ever delayed. That is the same contract
`ratelimit` keeps and for the same reason: a limiter whose failure mode is a
lost alert has become the defect it was built to remove.

The cost of `coalesce`, stated rather than left to be found: a thread that has
absorbed more than `conversations.MAX_MESSAGE_IDS` mails no longer indexes the
oldest of them, so a straggler reply naming one of those roots a fresh
conversation instead of landing in the thread. That is the store's existing
bound and its existing trade — a duplicate thread, never a lost mail — reached
sooner by folding. The alternative reaches it too, one thread at a time.

## Why this state is NOT persisted

`ConversationStore` persists its live set, and mg-27e0 argued at length that a
bound whose count lives only in memory is re-inflated by the next restart. That
argument is about a STANDING quantity — the population is whatever it was a
second before the process died, and a restart that forgets it forgets
everything.

A rate over a 60-second window is not that. Its whole state is worth less than
one window, it rebuilds itself from the next 60 seconds of traffic, and a
restart mid-burst re-enters the burst state within a window of resuming. The
anchors are the only part with any reach, and losing one costs a single extra
thread for that correspondent. So this is per-process, like `RelayLedger`, and
the honest scope is this run.

## What it says out loud

`report()` is the other half. A burst that is silently absorbed is a burst
nobody can tell happened, and the whole family of tickets this sits in
(mg-35b1, mg-7c1b, mg-879c, mg-8961) is about instruments that could not
distinguish a quiet day from a dead one. The episode gets an onset line, a
running line while it lasts, and a closing line with the totals — under its own
grep token, `thread-burst:`, which shares no prefix with `relay:` or `dedup:`.
"""
from __future__ import annotations

import datetime
from collections import deque
from dataclasses import dataclass

#: The trailing window creations are counted over. A minute, because that is
#: the resolution the incident was measured at and the one a human reads a
#: burst in ("peak 27 in the 06:56Z minute").
DEFAULT_WINDOW = 60

#: Creations per window at or above which a burst is declared and new
#: conversations start folding onto their correspondent's thread. The measured
#: burst ran 22-27/min and the rest of that day ran ~0.03/min, so anything in
#: the low tens separates them cleanly. 0 switches coalescing off.
DEFAULT_COALESCE_ABOVE = 12

#: Hard ceiling on creations per window. Past this a new conversation gets no
#: thread at all until the rate falls back. Above `coalesce_above` on purpose:
#: coalescing is the cheap remedy and gets first refusal, and the ceiling is
#: only reached when the burst is wide (many distinct correspondents) rather
#: than merely deep. 0 removes the ceiling and leaves only coalescing.
DEFAULT_CEILING = 30

#: How long a correspondent's thread stays eligible to absorb their next mail.
#: Long enough to span a flush (the measured one ran ~06:49Z-07:00Z with a tail
#: to 07:14Z), short enough that an unrelated burst tomorrow does not fold into
#: a thread from today. Only ever consulted while a burst is live, so the risk
#: this bounds is small to begin with.
DEFAULT_ANCHOR_TTL = 900

#: Correspondents tracked. Least recently used are dropped first; forgetting
#: one costs at most one extra thread.
DEFAULT_MAX_ANCHORS = 200

#: Ceiling on the creation timestamps retained. The window is what matters and
#: it is pruned on every read, so this is a backstop against a pathological
#: clock, not a working limit.
_MAX_TIMESTAMPS = 10000

#: The grep token for everything this module makes the adapter say. Distinct
#: from `relay:` and `dedup:` and sharing no prefix with either, so
#: `grep -c thread-burst:` counts bursts and nothing else — the same discipline
#: `relay-stall:` keeps against `relay:`.
BURST_TOKEN = 'thread-burst:'


@dataclass(frozen=True)
class Admission:
    """What should happen to one conversation that wants a thread.

    `kind` is one of:
        off       — the limiter is disabled; nothing is counted or folded
        create    — open a thread, as normal
        coalesce  — file this mail into `conversation` instead; it is the
                    thread this correspondent already has open in this episode
        over      — the ceiling is reached and there is nothing to fold into.
                    Open no thread; deliver by the other surface and say why.
    """

    kind: str
    #: The conversation the mail should be filed under. Equal to the key the
    #: caller asked about for `create`/`over`; the anchor's key for `coalesce`.
    conversation: str
    correspondent: str = ''
    #: Creations in the trailing window at the moment of this admission,
    #: INCLUDING this one when it creates. This is the number that goes in the
    #: log line, so it has to be the number the decision was made on.
    rate: int = 0
    window: int = DEFAULT_WINDOW
    #: How many mails this anchor has now absorbed, this one included. 0 unless
    #: `kind == 'coalesce'`.
    folded: int = 0
    #: When this admission was made. Carried so `rollback` can withdraw THIS
    #: creation rather than whichever one happens to be last: deliveries
    #: interleave across watchers at every `await`, so "the most recent" is not
    #: reliably the one that failed.
    at: float = 0.0

    @property
    def creates(self) -> bool:
        return self.kind in ('off', 'create')

    @property
    def coalesced(self) -> bool:
        return self.kind == 'coalesce'

    @property
    def over_ceiling(self) -> bool:
        return self.kind == 'over'


@dataclass(frozen=True)
class BurstReport:
    """One thing the adapter should say about the current episode.

    `phase` is 'onset', 'continuing' or 'over'. The facts are the same three
    numbers throughout, so a line lifted out of the log on its own still scopes
    the episode: how fast it is going, how much it folded, and how long it has
    been running.
    """

    phase: str
    rate: int
    peak: int
    window: int
    created: int
    coalesced: int
    over_ceiling: int
    anchors: int
    started: float
    elapsed: float


class ThreadBurstLimiter:
    """A trailing-window rate limit on thread creation, with coalescing.

    Usage, from the delivery path:

        admission = BURST.admit(mail['from'], key)
        if admission.coalesced:
            key = admission.conversation
        elif admission.over_ceiling:
            ...deliver without a thread, and say so...
        ...
        if thread creation failed:
            BURST.rollback(admission)

    and once per delivery cycle:

        report = BURST.report()
        if report is not None:
            print(format_thread_burst(report))

    **`admit()` records as it decides, unlike `DuplicateLimiter.decide()`.**
    That split exists there because a delivery recorded before it landed would
    let the limiter suppress the retry — trading a duplicate for a DROP. Here
    the two error directions are not symmetric in that way. Counting a creation
    that then fails makes the limiter briefly more conservative for at most one
    window, and costs nothing but a thread opening a little later. NOT counting
    it until the thread exists opens a check-then-act window across an `await`,
    and every concurrent delivery inside that window reads the same
    under-the-ceiling answer and creates — which is the unbounded burst this
    module exists to bound, arriving by the back door. So the accounting is
    synchronous with the decision, and `rollback()` exists for the one caller
    that knows its creation definitely did not happen.
    """

    def __init__(self, *, window: int = DEFAULT_WINDOW,
                 coalesce_above: int = DEFAULT_COALESCE_ABOVE,
                 ceiling: int = DEFAULT_CEILING,
                 anchor_ttl: int = DEFAULT_ANCHOR_TTL,
                 max_anchors: int = DEFAULT_MAX_ANCHORS,
                 enabled: bool = True, clock=None):
        self._clock = clock or _now
        self.window = max(1, int(window))
        self.coalesce_above = max(0, int(coalesce_above))
        self.ceiling = max(0, int(ceiling))
        self.anchor_ttl = max(0, int(anchor_ttl))
        self.max_anchors = max(1, int(max_anchors))
        #: With neither threshold set there is nothing to enforce, and the
        #: limiter says so rather than pretending to be on. An operator who
        #: turns it off gets that stated at startup, for the reason
        #: `relay_status_line` states its own off case: a control that can be
        #: silently disabled has the defect it was built to remove.
        self.enabled = bool(enabled) and (self.coalesce_above > 0 or self.ceiling > 0)
        #: Creation timestamps, oldest first. Pruned to `window` on every read.
        self._creations: deque[float] = deque()
        #: correspondent -> (conversation key, last touched). Insertion-ordered
        #: for LRU pruning; re-inserted on every touch, like the store's live
        #: queue, because a dict keeps a key's original position when its value
        #: is merely overwritten.
        self._anchors: dict[str, tuple[str, float]] = {}
        # Episode state. `_started` is None between episodes.
        self._started: float | None = None
        self._last_report: float | None = None
        self._reported_phase = ''
        self._peak = 0
        self._created = 0
        self._coalesced = 0
        self._over = 0
        #: anchor key -> how many mails it has absorbed this episode. Reset by
        #: each episode, so the number in a log line scopes to that burst.
        self._fold_counts: dict[str, int] = {}

    # -- the rate ---------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._creations and self._creations[0] <= cutoff:
            self._creations.popleft()
        # A clock that stepped backwards leaves timestamps in the future, which
        # `cutoff` never reaches. Deliver rather than wedge: drop the lot and
        # start the window over, the same direction `DuplicateLimiter.decide`
        # takes on a negative elapsed.
        if len(self._creations) > _MAX_TIMESTAMPS:
            self._creations.clear()

    def rate(self, now: float | None = None) -> int:
        """Creations in the trailing window. Cheap; prunes as it reads."""
        at = self._clock() if now is None else now
        self._prune(at)
        return len(self._creations)

    def bursting(self, now: float | None = None) -> bool:
        """True if the creation rate is at or over the coalescing threshold."""
        if not self.enabled or not self.coalesce_above:
            return False
        return self.rate(now) >= self.coalesce_above

    # -- anchors ----------------------------------------------------------

    @staticmethod
    def anchor_key(correspondent: str) -> str:
        return (correspondent or '?').strip().casefold()

    def anchor_for(self, correspondent: str, now: float | None = None) -> str:
        """The conversation this correspondent's next mail may fold into, or ''.

        Expired anchors are dropped as they are read, so a stale one can never
        capture a mail from a later, unrelated episode.
        """
        at = self._clock() if now is None else now
        who = self.anchor_key(correspondent)
        entry = self._anchors.get(who)
        if entry is None:
            return ''
        key, touched = entry
        if self.anchor_ttl and (at - touched > self.anchor_ttl or at < touched):
            self._anchors.pop(who, None)
            return ''
        return key

    def _touch_anchor(self, correspondent: str, key: str, now: float) -> None:
        who = self.anchor_key(correspondent)
        self._anchors.pop(who, None)
        self._anchors[who] = (key, now)
        while len(self._anchors) > self.max_anchors:
            self._anchors.pop(next(iter(self._anchors)))

    def forget_anchor(self, correspondent: str) -> None:
        """Drop a correspondent's anchor — the conversation behind it is gone."""
        self._anchors.pop(self.anchor_key(correspondent), None)

    # -- the limit --------------------------------------------------------

    def admit(self, correspondent: str, key: str, *,
              now: float | None = None) -> Admission:
        """Decide — and record — what this new conversation gets.

        Call once per conversation that is about to open a thread, and only for
        one that does not have one already: waking an archived thread is the
        standing bound's business, not this one's. A thread that is woken costs
        the channel a slot but costs the client nothing to create, and charging
        it here would make a long-running bridge look like it was bursting
        every time an old conversation got a reply.
        """
        at = self._clock() if now is None else now
        if not self.enabled:
            return Admission(kind='off', conversation=key,
                             correspondent=correspondent, window=self.window,
                             at=at)

        self._prune(at)
        rate = len(self._creations)
        over_coalesce = bool(self.coalesce_above) and rate >= self.coalesce_above
        over_ceiling = bool(self.ceiling) and rate >= self.ceiling

        if over_coalesce or over_ceiling:
            self._open_episode(at, rate)
            anchor = self.anchor_for(correspondent, now=at)
            if anchor and anchor != key:
                self._coalesced += 1
                self._touch_anchor(correspondent, anchor, at)
                folded = self._fold_counts.get(anchor, 0) + 1
                self._fold_counts[anchor] = folded
                return Admission(kind='coalesce', conversation=anchor,
                                 correspondent=correspondent, rate=rate,
                                 window=self.window, folded=folded, at=at)
            if over_ceiling:
                # Nothing to fold into and no budget to create with. The caller
                # delivers by its other surface; this conversation opens its own
                # thread on a later mail, once the rate has fallen back.
                self._over += 1
                return Admission(kind='over', conversation=key,
                                 correspondent=correspondent, rate=rate,
                                 window=self.window, at=at)

        # Charged before the thread exists, on purpose — see the class
        # docstring. `rollback` un-charges it if the create demonstrably failed.
        self._creations.append(at)
        rate += 1
        if self._started is not None:
            self._created += 1
            self._peak = max(self._peak, rate)
        self._touch_anchor(correspondent, key, at)
        return Admission(kind='create', conversation=key,
                         correspondent=correspondent, rate=rate,
                         window=self.window, at=at)

    def rollback(self, admission: Admission) -> None:
        """Un-charge a creation that did not happen.

        Only for a caller that KNOWS the thread was not opened — a create that
        raised, or a log channel that could not be reached. A creation left
        charged costs at most one window of conservatism; one un-charged that
        did happen is a hole in the ceiling, so this is deliberately not
        automatic.
        """
        if not self.enabled or admission.kind != 'create':
            return
        try:
            self._creations.remove(admission.at)
        except ValueError:
            # Already aged out of the window. Nothing to withdraw.
            pass
        who = self.anchor_key(admission.correspondent)
        entry = self._anchors.get(who)
        if entry is not None and entry[0] == admission.conversation:
            # Only if it still points at the conversation THIS admission
            # opened. A later mail from the same correspondent may have moved
            # it on, and that anchor is real.
            self._anchors.pop(who, None)
        if self._started is not None and self._created:
            self._created -= 1

    # -- episodes and reporting -------------------------------------------

    def _open_episode(self, at: float, rate: int) -> None:
        if self._started is None:
            self._started = at
            self._last_report = None
            self._reported_phase = ''
            self._peak = rate
            self._created = 0
            self._coalesced = 0
            self._over = 0
            self._fold_counts = {}
        else:
            self._peak = max(self._peak, rate)

    def report(self, now: float | None = None) -> BurstReport | None:
        """The line the adapter should print, or None. Mutates when it returns.

        Call once per delivery cycle. Three phases, at most one per call:

        * `onset` — the first cycle in which an episode is live. This is the
          line that says a burst is happening WHILE it is happening, which is
          the thing the `relay:` beat could not say: `171 delivered in the last
          262762s` is true and reads as a trickle.
        * `continuing` — at most one per window while the episode lasts, so a
          long flush costs the log a line a minute rather than a line a mail.
        * `over` — the rate has fallen back. Carries the episode's totals, so
          the burst is scoped in the file without reading back to the onset.
        """
        if not self.enabled:
            return None
        at = self._clock() if now is None else now
        rate = self.rate(at)

        if self._started is None:
            return None

        if rate < max(1, self.coalesce_above or self.ceiling):
            report = BurstReport(
                phase='over', rate=rate, peak=self._peak, window=self.window,
                created=self._created, coalesced=self._coalesced,
                over_ceiling=self._over, anchors=len(self._anchors),
                started=self._started, elapsed=max(0.0, at - self._started))
            self._started = None
            self._last_report = None
            self._reported_phase = ''
            self._fold_counts = {}
            return report

        self._peak = max(self._peak, rate)
        if self._reported_phase == '':
            phase = 'onset'
        elif self._last_report is not None and at - self._last_report < self.window:
            return None
        else:
            phase = 'continuing'
        self._last_report = at
        self._reported_phase = phase
        return BurstReport(
            phase=phase, rate=rate, peak=self._peak, window=self.window,
            created=self._created, coalesced=self._coalesced,
            over_ceiling=self._over, anchors=len(self._anchors),
            started=self._started, elapsed=max(0.0, at - self._started))

    def summary(self) -> dict:
        """Facts for the adapter's status surfaces. Renders nothing."""
        at = self._clock()
        return {
            'enabled': self.enabled,
            'window': self.window,
            'coalesce_above': self.coalesce_above,
            'ceiling': self.ceiling,
            'anchor_ttl': self.anchor_ttl,
            'rate': self.rate(at),
            'bursting': self._started is not None,
            'peak': self._peak,
            'created': self._created,
            'coalesced': self._coalesced,
            'over_ceiling': self._over,
            'anchors': len(self._anchors),
        }


def _now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()
