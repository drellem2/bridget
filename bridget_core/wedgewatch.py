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

# bridget_core.wedgewatch — when a delivery outage stops being bridget's problem.
"""An outage long enough to be an incident escalates OFF this transport, then
restarts the process that cannot deliver.

`relaylog` made an outage legible to a reader of `~/.pogo/bridget.log`
(mg-879c). This module is about the reader who is not reading it. On
2026-08-19 delivery failed for eight minutes, every 35s, identically —

    deliver failed for <msg-id>, will retry: Cannot connect to host
    discord.com:443 ssl:default [nodename nor servname provided, or not known]

— while the host resolved `discord.com` 5/5 from a shell. The wedge was
resolver state *inside* the process, it had never once self-cleared, and the
message stuck in the retry loop was pogod's own fleet-health alarm:

    "AGENTS ARE FAILING EVERY TURN — pec0d (server_error)"

So the alarm and the only transport the alarm has failed together, and the
alarm's only recipient is a human who found out by noticing that nothing had
reacted. Eight minutes of 100% delivery failure produced no alert, no mail, no
event, and no change in any health surface; `supervise` later recorded the
process's exit as `rc=143 after 639957s (healthy run)`.

**Why this is not the resolver's bug.** mg-879c dated every failure line in
`bridget.err.log` across 2026-08-04..19 and found the same aiohttp
`ClientConnectorDNSError` on `discord.com:443` FOUR times — 51 messages stuck
over 08-04..10, 10 over 08-14, 164 over a 71.6-hour outage on 08-16..19, and
the one this ticket caught. Every occurrence ended in a process restart and
none ended any other way; nothing in either log records a self-recovery. A
fifth is a matter of time, and the part that has to change is not the
resolver — it is that nobody hears about it and nothing acts.

**Two decisions, and they are separate on purpose.**

`escalate_after` is the one that must never be off. It says an outage this long
is an incident, and it is reported to surfaces that do not depend on the
transport that is broken — a `pogo events` record and a mail to the mayor's
maildir, both local files. Reporting it *through* bridget is the circularity
this exists to break.

`selfheal_after` is the one that acts: exit non-zero and let
`bridget-supervise` respawn. That is cheap here and it is the only remedy with
a measured success rate — the supervisor restarts in 5s, the maildir is
uncommitted until a mail actually lands, so nothing is lost, and the four
occurrences above were each cleared by exactly this.

**The remedy is an artifact of the same kind as the defect, so:**

1. *A silent restart is the defect again.* A process that vanishes and returns
   with fresh counters leaves the same nothing-happened trace as the wedge. So
   a self-heal cannot be taken until this outage has been escalated: if the
   thresholds are configured such that the restart would come first, the
   escalation is forced out ahead of it (`Verdict.escalate` is populated
   whenever `selfheal` is, and `escalations` says which round it is).

2. *A restart budget that lives in the restarted process is not a budget.* The
   counter resets to zero on every respawn, so a genuinely-down network would
   flap forever — a self-heal loop wearing a rate limiter's clothes. The budget
   is therefore a FILE (`RestartBudget`), and the accounting has to survive the
   thing it is accounting for.

3. *A budget that fails open is not a budget either.* If the file cannot be
   written, every restart re-reads an empty ledger and the cap is unlimited. So
   a spend that cannot be recorded is REFUSED, not granted — the escalation
   still goes out, and bridget stays up saying so, which is the safe half.

Nothing here renders and nothing here does IO except `RestartBudget`, which
owns one small file. The lines, the mail body and the event payload are the
adapter's, the way `RelayStall` is a fact and `format_relay_stall` is a line.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .statefile import write_state

#: Exit status bridget uses for a self-heal, distinct from a crash (1), from a
#: signal (128+n, e.g. the 143 a `kill` produces) and from a config refusal.
#: `bridget-supervise` names it in its log, because "restarted itself because
#: delivery was wedged" and "died" are the same line otherwise — and that line
#: reading `(healthy run)` over an 8-minute total outage is part of what this
#: module is for. 75 is sysexits.h's EX_TEMPFAIL: the condition is real, and
#: retrying is the correct response to it.
EXIT_SELFHEAL = 75

#: Seconds of UNBROKEN delivery failure before an outage is an incident. The
#: threshold is a duration and not a cycle count on purpose: `BRIDGET_POLL_INTERVAL`
#: is configurable, so N cycles means a different outage at every deployment,
#: and what makes this an incident is wall-clock time with mail undelivered.
#: 120s is ~24 poll cycles at the default 5s interval — long enough that a
#: rate-limit or a gateway reconnect has had its chance, short enough that the
#: eight-minute wedge is caught at 07:32 rather than at 07:43 by a human.
DEFAULT_ESCALATE_AFTER = 120

#: Seconds before the process restarts itself. Deliberately later than the
#: escalation so the record is written by a process that is still running: an
#: escalation racing its own exit is a report that may not exist.
DEFAULT_SELFHEAL_AFTER = 300

#: How often a still-unresolved outage re-escalates. The same cadence as the
#: `relay-stall:` line, for the same reason — an outage should cost its reader
#: what a quiet day costs them, and the 71.6h one would otherwise be a single
#: mail three days stale.
DEFAULT_REPEAT_INTERVAL = 3600

#: Restarts allowed per `DEFAULT_BUDGET_WINDOW`. Three is enough for the
#: measured failure — one restart cleared it every time — and small enough that
#: a real network outage costs three respawns and then stops, leaving a live
#: bridget escalating rather than a flapping one that is down more than it is up.
DEFAULT_RESTART_BUDGET = 3
DEFAULT_BUDGET_WINDOW = 3600


@dataclass(frozen=True)
class Escalation:
    """One due escalation. Facts only; the adapter writes the mail and event.

    `escalations` counts THIS outage's escalations including this one, so a
    reader can tell a first alarm from an hourly repeat without diffing
    timestamps — 1 is "delivery just broke", 72 is "and it has been three days".
    """
    stalled_for: float
    cycles: int
    since: float
    escalations: int


@dataclass(frozen=True)
class SelfHeal:
    """One granted restart. Only ever produced when the budget had room.

    `spent`/`budget` ride along because they are the part a reader needs to
    predict what happens next: `3/3` means this is the last restart the window
    allows and a fifth failure will be reported and endured rather than acted on.
    """
    stalled_for: float
    cycles: int
    since: float
    spent: int
    budget: int


@dataclass(frozen=True)
class Verdict:
    """What a failing cycle should cause. Both halves may be None."""
    escalate: Escalation | None = None
    selfheal: SelfHeal | None = None


class RestartBudget:
    """How many self-heals have been spent lately, persisted across restarts.

    The file holds the epoch stamps of recent grants, pruned to `window` on
    every read. It is small and it is rewritten whole; there is no growth path.

    It lives outside the process for the reason in the module docstring: an
    in-memory cap on restarts is reset by the restart, which is not a cap. That
    makes this the one piece of state here whose *absence* is dangerous rather
    than merely lossy — so a read that fails is treated as an empty ledger (a
    first restart is still the right move on a fresh install), while a WRITE
    that fails denies the grant. Fail-open on the read, fail-closed on the
    spend: the asymmetry is the whole design.
    """

    def __init__(self, path: Path | None, *, limit: int = DEFAULT_RESTART_BUDGET,
                 window: int = DEFAULT_BUDGET_WINDOW, clock=None):
        self.path = Path(path) if path is not None else None
        self.limit = limit
        self.window = window
        self._clock = clock or _now
        #: Why a spend was refused, for the escalation to carry. A restart that
        #: did not happen must say which of the two reasons it was: the budget
        #: is exhausted (the outage is bigger than a restart can fix) or the
        #: ledger could not be written (bridget cannot account for restarts, so
        #: it takes none). Those call for different human actions.
        self.last_refusal = ''

    def stamps(self, now: float | None = None) -> list[float]:
        """Grants inside the window, oldest first. Never raises."""
        if now is None:
            now = self._clock()
        if self.path is None:
            return []
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if not isinstance(item, (int, float)):
                continue
            # A stamp in the future is a clock that stepped backwards (sleep or
            # NTP), not a grant. Keeping it would silently freeze the budget
            # until wall-clock caught up; dropping it costs at most an extra
            # restart, which is the direction this should err in.
            if 0 <= now - item < self.window:
                out.append(float(item))
        return sorted(out)

    def spent(self, now: float | None = None) -> int:
        return len(self.stamps(now))

    def spend(self, now: float | None = None) -> tuple[bool, int]:
        """Take one restart if the window allows. Returns (granted, spent).

        `spent` is the count AFTER a grant, so it reads as "this was the Nth of
        `limit`".
        """
        if now is None:
            now = self._clock()
        self.last_refusal = ''
        if self.limit <= 0:
            self.last_refusal = 'budget is zero'
            return False, 0
        stamps = self.stamps(now)
        if len(stamps) >= self.limit:
            self.last_refusal = (
                f'{len(stamps)} restarts already in the last {self.window}s')
            return False, len(stamps)
        stamps.append(now)
        if self.path is None:
            # No file means no accounting, and unaccounted restarts are the
            # flap this class exists to bound. Same rule as an unwritable file.
            self.last_refusal = 'no restart-budget file configured'
            return False, len(stamps) - 1
        try:
            write_state(self.path, json.dumps(stamps))
        except OSError as e:
            self.last_refusal = f'could not record the restart: {e}'
            return False, len(stamps) - 1
        return True, len(stamps)


class WedgeWatch:
    """Tracks an unbroken run of delivery-failing cycles and rules on it.

    Usage mirrors `RelayLedger`, from the same place in the same loop:

        if delivery_ok:
            watch.healthy()
        else:
            verdict = watch.failing()
            if verdict.escalate:
                escalate(verdict.escalate)      # out of band, never via Discord
            if verdict.selfheal:
                restart(verdict.selfheal)       # after the escalation, always

    Per process except for the budget. `healthy()` ends a run, so a wedge that
    clears and returns is a second incident with its own first-alarm — folding
    it into the first would report a recurrence as a continuation, and a
    recurrence is the more interesting of the two.
    """

    def __init__(self, *, escalate_after: int = DEFAULT_ESCALATE_AFTER,
                 selfheal_after: int = DEFAULT_SELFHEAL_AFTER,
                 repeat_interval: int = DEFAULT_REPEAT_INTERVAL,
                 budget: RestartBudget | None = None, clock=None):
        self._clock = clock or _now
        self.escalate_after = escalate_after
        self.selfheal_after = selfheal_after
        self.repeat_interval = max(1, repeat_interval)
        self.budget = budget
        self.reset()

    @property
    def escalation_enabled(self) -> bool:
        return self.escalate_after > 0

    @property
    def selfheal_enabled(self) -> bool:
        # A zero-limit budget is a switched-off self-heal, not a budgeted one:
        # every spend would be refused, so reporting the feature as on would
        # make the escalation promise a restart that can never come.
        return (self.selfheal_after > 0 and self.budget is not None
                and self.budget.limit > 0)

    @property
    def since(self) -> float | None:
        """When the current run of failing cycles began, or None if healthy."""
        return self._since

    @property
    def cycles(self) -> int:
        return self._cycles

    def reset(self) -> None:
        self._since: float | None = None
        self._cycles = 0
        self._last_escalation: float | None = None
        self._escalations = 0
        self._healed = False
        self._refusal = ''

    def healthy(self, now: float | None = None) -> None:
        """Delivery worked this cycle: the run, and any incident, is over."""
        self.reset()

    def failing(self, now: float | None = None) -> Verdict:
        """Record one delivery-failing cycle and say what it should cause."""
        if now is None:
            now = self._clock()
        if self._since is None or now < self._since:
            # A backwards clock step restarts the run rather than reporting a
            # negative duration. The alternative — clamping — would report an
            # outage as shorter than it is, and this record errs the other way.
            self._since = now
            self._cycles = 0
        self._cycles += 1
        stalled_for = max(0.0, now - self._since)

        heal, refused = self._due_selfheal(now, stalled_for)
        # A REFUSED self-heal forces the alarm exactly as a granted one does,
        # and that is not symmetry for its own sake. Without it, an outage past
        # `selfheal_after` with an exhausted budget escalated once at the
        # threshold saying "will restart after 300s", then did not restart, and
        # said nothing further until the hourly repeat — a promise quietly
        # broken, which is the shape of the defect this module is for. The
        # moment bridget decides it will NOT act is the moment worth reporting.
        esc = self._due_escalation(now, stalled_for,
                                   forced=heal is not None or refused)
        return Verdict(escalate=esc, selfheal=heal)

    def _due_escalation(self, now: float, stalled_for: float,
                        *, forced: bool) -> Escalation | None:
        """The escalation for this cycle, or None.

        `forced` means a self-heal was granted on this same cycle, and it
        overrides BOTH gates — the threshold and the repeat interval, and even
        `escalate_after=0`. A restart is a new fact about the outage and the
        one the reader most needs, so it is never folded into an hourly
        interval and never suppressed by a knob: guarantee 1 in the module
        docstring is that no self-heal is silent.
        """
        if not forced:
            if not self.escalation_enabled:
                return None
            if stalled_for < self.escalate_after:
                return None
            if (self._last_escalation is not None
                    and now - self._last_escalation < self.repeat_interval):
                return None
        self._last_escalation = now
        self._escalations += 1
        return Escalation(stalled_for=stalled_for, cycles=self._cycles,
                          since=self._since, escalations=self._escalations)

    def _due_selfheal(self, now: float,
                      stalled_for: float) -> tuple[SelfHeal | None, bool]:
        """(the granted restart or None, whether one was asked for and refused)."""
        if self._healed:
            return None, False
        if not self.selfheal_enabled:
            # Switched off is not a refusal: nothing was asked for, so there is
            # nothing new to report, and the threshold escalation already says
            # the self-heal is off.
            return None, False
        if stalled_for < self.selfheal_after:
            return None, False
        # Asked once per outage whatever the answer. A refused spend that
        # re-asked every five seconds would rewrite the budget file forever and
        # bury the refusal in its own repetition (mg-5521's lesson, one
        # subsystem over).
        self._healed = True
        granted, spent = self.budget.spend(now)
        if not granted:
            self._refusal = self.budget.last_refusal
            return None, True
        return SelfHeal(stalled_for=stalled_for, cycles=self._cycles,
                        since=self._since, spent=spent,
                        budget=self.budget.limit), False

    @property
    def refusal(self) -> str:
        """Why THIS outage's self-heal did not happen, or '' if none was refused.

        Scoped to the outage and not to the budget on purpose. `RestartBudget`
        keeps its last refusal indefinitely, so reading it directly would let a
        stale one from a previous incident describe a new one — reporting "3
        restarts already in the last 3600s" for an outage whose window has since
        rolled, which is a false explanation for a real alarm.
        """
        return self._refusal


def _now() -> float:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).timestamp()
