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

# bridget_core.relaylog — the delivery path's positive record.
"""When the delivery path should say "I am here", and what it should say.

Until mg-7c1b, `~/.pogo/bridget.log` recorded delivery only when it went
wrong. That is an instrument with the same defect mg-35b1 fixed on the other
axis: a log that records only exceptions cannot be used to confirm the normal
path ran. Its silence is consistent with perfect health AND with total death,
and the reader cannot tell which without leaving the file.

Two readers were caught by that zero on the same night, in different hands:

    grep -c blackout ~/.pogo/bridget.log   ->  0   # read as "Discord carried
                                                   # NONE" — false; bridget had
                                                   # relayed all 33 within
                                                   # seconds
    grep -c dedup    ~/.pogo/bridget.log   ->  0   # read as "the dedup held
                                                   # zero repeats" — true, and
                                                   # reported for ~5 cycles with
                                                   # nothing establishing that
                                                   # the path was even running

**Why a heartbeat rather than one line per relayed mail.** The ticket offers
both. A per-mail line distinguishes "delivered something" from "delivered
nothing" — but an idle-and-healthy bridget still writes nothing, so silence
stays ambiguous, which is the defect. Only a beat that fires with NOTHING to
report makes silence mean death. The count rides along on it, so a per-mail
line's information is not lost: `delivered 3 in the last 61s` is the three
lines, folded.

**Why it is not a bare "still alive".** bridget already has one of those, and
the way it failed is the reason this module exists at all. `bridget.heartbeat`
kept ticking through the ~70h wedge of mg-e5b8, because it is a LOOP heartbeat:
it proved the poller was iterating while outbound delivery was dead. So the
caller must gate this beat on a delivery-healthy cycle — the same gate as
`bridget.delivery.heartbeat` — and this module's job is only to say *when* a
beat is due. A record that ticks while delivery is broken is worse than no
record: it reads as a positive.

**Volume.** Daniel merged a duplication limit into this repo the same night
(mg-5521) because he was drowning in repetition, so a record that buries the
exception lines it sits among would have traded one unreadable file for
another. The cadence is therefore two-rate:

    idle      — one line per `interval` (default 3600s: 24 lines/day)
    active    — one line per `min(60, interval)`, carrying the count of
                everything relayed since the previous line

so a burst of 33 alarms arriving within seconds is one line, not 33, and a
quiet day costs 24. A log file is not a Discord DM and does not reach Daniel;
this is volume in the file people grep, not volume in his notifications.

**Why silence was not enough (mg-879c).** The gate above is right about the
beat: a positive printed on a cycle whose sends were failing would be
`bridget.heartbeat` all over again. But gating it left the outage itself
unwritten, and that was tested for and shipped on the reasoning "the stop IS
the signal". It was then measured, and the reasoning did not hold:

    bridget.log  2026-08-16T07:26:41Z  <last line>
    bridget.log  2026-08-19T06:54:30Z  logged in as pogo-bridge#9730

71.6 hours, 164 mail stuck, ~8,600 failed sends — and the reader who found
this hole read it as "a 3-DAY hole nobody has explained", not as "delivery was
down", because a stop looks the same as a quiet fleet, a slept host, a rotated
file or a killed process. The stop is a signal only to someone who already
knows the beat is unconditional, and the file does not say that at the point of
absence. Worse, the beat that finally landed on recovery read

    relay: 171 delivered in the last 262762s (408 total since ...)

which is true and reads as a steady trickle over three days; it was a backlog
flushed into one DM burst in under five minutes.

So the stall gets its own line — `relay-stall:`, a DIFFERENT grep token, so it
can never be counted as a delivery — on the same cadence as the idle beat. An
outage now costs ~24 lines a day and is dated at both ends. `RelayLedger.stall`
is the negative; `RelayLedger.due` is the positive; a healthy cycle ends a
stall, so the two can never both be live.

Nothing here renders. `RelayBeat` and `RelayStall` are the facts; the adapter
formats them, the way `Decision` and `format_repeat_notice` divide the same
work.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass

#: Ceiling on how fast the beat may fire when mail IS flowing. A cycle that
#: delivered wants to be recorded near the delivery rather than up to an hour
#: later — but one line per poll cycle under sustained load is exactly the
#: repetition this must not become, so deliveries inside this window fold into
#: the next line's count.
ACTIVE_GAP_CAP = 60


def isostamp(timestamp: float) -> str:
    """Epoch seconds as `2026-08-11T20:29:03Z` — the fleet's log stamp format.

    Deliberately the `Z` form used by `bridget_core.logstamp`, not the
    `+00:00` form `ratelimit.isoformat` emits, because this value is read
    *inside* a log line whose own prefix is the `Z` form: two spellings of the
    same instant on one line is a thing to explain rather than a thing to read.
    """
    return datetime.datetime.fromtimestamp(
        timestamp, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


@dataclass(frozen=True)
class RelayBeat:
    """One due beat. Facts only — the adapter turns this into a line.

    `delivered` counts mail relayed since the PREVIOUS beat, `window` is how
    long that took, and `total`/`since` carry the run's whole story so a single
    line lifted out of the log still answers "has this process ever delivered
    anything".
    """
    delivered: int
    window: float
    total: int
    since: float


@dataclass(frozen=True)
class RelayStall:
    """One due stall line — what the gated beat could not say.

    `stalled_for` is 0 on the onset line by design: that line's job is to stamp
    WHEN delivery broke, and a duration is not yet a fact about it. The lines
    after it carry the growing number, so the outage can be scoped from any one
    of them without reading back to the onset.
    """
    stalled_for: float
    since: float
    total: int
    started: float


class RelayLedger:
    """Counts relayed mail and decides when the delivery path should speak.

    Per process, not persisted: `since` is this run's start, and that is the
    honest scope — a count restored across a restart would let a beat report
    deliveries the current process never made.

    Usage, from a cycle that is known delivery-healthy:

        for mail in poll():
            if deliver(mail):
                ledger.record()
        if delivery_ok:
            beat = ledger.due(now)
            if beat:
                print(format_relay_beat(beat), flush=True)

    `due()` mutates: it resets the per-window count and stamps the beat. Call
    it once per cycle, and only on a healthy one — see the module docstring for
    why gating matters more than cadence.
    """

    def __init__(self, started: float | None = None, *, interval: int = 3600,
                 enabled: bool = True, clock=None):
        self._clock = clock or _now
        self.started = self._clock() if started is None else started
        self.interval = interval
        #: interval <= 0 switches the record off entirely. It is a knob rather
        #: than a constant because the right cadence depends on how noisy this
        #: file is for its reader, and that is not ours to guess.
        self.enabled = enabled and interval > 0
        self.total = 0
        self._pending = 0
        #: None until the first beat: the delivery loop's FIRST healthy cycle
        #: emits one, so the log carries a positive within a poll interval of
        #: startup instead of an hour of silence indistinguishable from a
        #: watcher that never spawned.
        self._last_beat: float | None = None
        #: When the current run of unhealthy cycles began, and when the last
        #: stall line was emitted. Both cleared by `due()`, which is only ever
        #: called from a healthy cycle — so a stall cannot outlive the outage
        #: that opened it, and a second outage is announced at its own onset
        #: rather than folded into the first.
        self._stalled_since: float | None = None
        self._last_stall: float | None = None

    @property
    def active_gap(self) -> int:
        """Minimum seconds between beats when there is something to report."""
        return min(ACTIVE_GAP_CAP, self.interval)

    def record(self, count: int = 1) -> None:
        """Count `count` mail as relayed. Cheap; call it per delivered mail."""
        self.total += count
        self._pending += count

    def due(self, now: float | None = None) -> RelayBeat | None:
        """The beat to emit at `now`, or None. Mutates when it returns a beat.

        Three ways a beat comes due, in the order they matter:

        1. No beat yet this run — the loop has completed its first healthy
           cycle and the file should say so.
        2. Mail was relayed and `active_gap` has passed — record it near the
           event, folding a burst into one line.
        3. `interval` has passed with nothing to report — the idle beat, and
           the only one that makes silence mean death.
        """
        if not self.enabled:
            return None
        if now is None:
            now = self._clock()
        # This cycle was healthy, so any stall is over. Clearing here rather
        # than in a separate call keeps the two records from ever both being
        # live: there is exactly one place a healthy cycle is reported.
        self._stalled_since = None
        self._last_stall = None
        if self._last_beat is None:
            return self._beat(now, self.started)
        elapsed = now - self._last_beat
        if self._pending and elapsed >= self.active_gap:
            return self._beat(now, self._last_beat)
        if elapsed >= self.interval:
            return self._beat(now, self._last_beat)
        return None

    def stall(self, now: float | None = None) -> RelayStall | None:
        """The stall line to emit at `now`, or None. Call only from an
        UNHEALTHY cycle — the mirror of `due()`, and mutating in the same way.

        Fires immediately on the first unhealthy cycle, so the log stamps the
        onset rather than the first hour mark, then at most once per
        `interval`. The cadence is the idle beat's on purpose: an outage should
        cost the reader what a quiet day costs them, and no more.
        """
        if not self.enabled:
            return None
        if now is None:
            now = self._clock()
        if self._stalled_since is None:
            self._stalled_since = now
            self._last_stall = None
        if self._last_stall is not None and now - self._last_stall < self.interval:
            return None
        self._last_stall = now
        return RelayStall(stalled_for=max(0.0, now - self._stalled_since),
                          since=self._stalled_since,
                          total=self.total,
                          started=self.started)

    def _beat(self, now: float, window_start: float) -> RelayBeat:
        beat = RelayBeat(delivered=self._pending,
                         window=max(0.0, now - window_start),
                         total=self.total,
                         since=self.started)
        self._pending = 0
        self._last_beat = now
        return beat


def _now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()
