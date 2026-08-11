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

# bridget_core.ratelimit — duplicate-alert suppression.
"""A duplication limit for repeated alerts, keyed on the *condition*.

Daniel, 2026-08-11: "stall-watch etc send me emails which becomes crazy
annoying on discord ... bc they duplicate a lot, if there was some sort of
duplication limit that would be fine." A snapshot of his maildir that day held
1404 unread, of which the top eight conditions accounted for 109 messages —
one of them repeated 31 times.

Two properties do the work.

**The key normalises digits.** The three highest-volume rows in that snapshot
were one condition whose fire count drifted:

    ack-watch: FLEET BLACKOUT - 90 fires delivered ... NONE completed   x13
    ack-watch: FLEET BLACKOUT - 92 fires delivered ... NONE completed   x9
    ack-watch: FLEET BLACKOUT - 91 fires delivered ... NONE completed   x5

A limiter keyed on the literal subject sees three conditions there and lets
most of the volume through. `normalize_subject` folds every digit run to `N`,
so those three are one key.

Digit-folding is deliberately blunt, and it has one carve-out: an mg-id is
*identity*, not drift. `approval needed mg-4fc0` and `approval needed mg-9a13`
are two different decisions waiting on the human, and folding them together
would suppress the second one — the limiter dropping exactly the mail it has
least right to drop. `mg-[0-9a-f]+` therefore survives normalisation intact.

**A first occurrence is never delayed and never dropped.** The limiter only
ever suppresses a *repeat*, and re-notifies on a doubling backoff — 15 minutes
after the first, then 30, 60, 120, up to a cap — so a condition that is still
firing keeps saying so at a decaying rate instead of going silent. Everything
suppressed stays counted (`suppressed`, `suppressed_total`) and is reported
back on the next delivery, so the volume is recoverable rather than lost.

Deciding and recording are two calls, on purpose. `decide()` is pure; nothing
is written until `commit()`. The caller commits only once the mail has actually
reached a surface, which is the same at-least-once contract `MaildirWatcher`
keeps with its seen-set — and for the same reason. A limiter that recorded a
delivery optimistically would count a *failed* send as the first occurrence and
then suppress the retry, turning a transient Discord outage into a silently
dropped alert. That is the defect this module exists to remedy, arriving by the
back door.

Nothing here renders: `Decision` and `summary()` are facts, and the adapter
decides what a suppression notice looks like.
"""
from __future__ import annotations

import datetime
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .statefile import write_state

SCHEMA_VERSION = 1

#: Delay before a still-firing condition is allowed to notify a second time.
#: Each further notification doubles the wait, up to DEFAULT_MAX_INTERVAL.
DEFAULT_BASE_INTERVAL = 900        # 15 minutes

#: Ceiling on that doubling. A condition firing all day still checks in every
#: four hours, which is the difference between "quieter" and "silent".
DEFAULT_MAX_INTERVAL = 14400       # 4 hours

#: Quiet time after which a condition counts as new again, so its next firing
#: is a first occurrence and arrives immediately. Measured from the last time
#: the condition was *seen*, not the last time it was delivered: a condition
#: firing every five minutes for two days never resets, and correctly so.
DEFAULT_TTL = 86400                # 24 hours

#: Conditions tracked. Least-recently-seen are dropped first. Forgetting one
#: costs at most a single extra notification, so this can be small.
DEFAULT_MAX_KEYS = 500

#: Preserve mg-ids, fold every other digit run. One pass, so an id is matched
#: before its digits can be eaten by the second branch.
_TOKEN_RE = re.compile(r'(mg-[0-9a-f]+)|(\d+)', re.IGNORECASE)
_WS_RE = re.compile(r'\s+')
_REPLY_PREFIX_RE = re.compile(r'^(?:re|fwd|fw)\s*:\s*', re.IGNORECASE)

#: Separates sender from subject in a key. Neither can contain it.
_KEY_SEP = '\x1f'


def _fold(match: re.Match) -> str:
    return match.group(1) if match.group(1) else 'N'


def normalize_subject(subject: str) -> str:
    """Fold a subject to the condition it describes.

    Reply prefixes go, digit runs become `N` (mg-ids excepted), whitespace
    collapses, case is folded. What is left is what two firings of the same
    watcher have in common.
    """
    text = subject or ''
    while True:
        stripped = _REPLY_PREFIX_RE.sub('', text, count=1)
        if stripped == text:
            break
        text = stripped
    text = _TOKEN_RE.sub(_fold, text)
    return _WS_RE.sub(' ', text).strip().casefold()


def alert_key(sender: str, subject: str) -> str:
    """The identity of a condition: who reports it, and what it says.

    The sender is part of the key because two watchers saying "work piling up"
    are two conditions, and silencing one because the other already fired would
    be the limiter inventing a duplicate that isn't one.
    """
    who = (sender or '?').strip().casefold().replace(_KEY_SEP, ' ')
    return who + _KEY_SEP + normalize_subject(subject)


def claims_ancestry(mail: dict) -> bool:
    """True if this message names a parent — i.e. it is part of a reply chain.

    The limiter is for *broadcast* alerts, which root their own conversation
    every time they fire. A reply is by construction not a repeated alert, and
    it normalises to the same key as the message it answers (`Re: ` is
    stripped), so rate-limiting one would suppress a live conversation. Callers
    skip the limiter for anything that claims ancestry or resolves onto a
    conversation already on record.
    """
    return bool(mail.get('in_reply_to')) or bool(mail.get('references'))


def isoformat(timestamp: float) -> str:
    """An epoch seconds value as an ISO-8601 UTC string. '' for 0/None."""
    if not timestamp:
        return ''
    return datetime.datetime.fromtimestamp(
        timestamp, datetime.timezone.utc).isoformat(timespec='seconds')


@dataclass(frozen=True)
class Decision:
    """What the limiter thinks should happen to one message. No state changed.

    `kind` is one of:
        off        — the limiter is disabled; nothing is tracked
        first      — the first firing of this condition (or the first since the
                     TTL expired). Always delivered, never delayed.
        repeat     — a repeat whose backoff has elapsed: the "still happening"
                     notification
        suppressed — a repeat inside the backoff window
    """

    key: str
    deliver: bool
    kind: str
    at: float = 0.0
    #: How many times this condition has fired, including this message.
    occurrences: int = 1
    #: How many of those have reached a surface, including this one if it is
    #: about to. This is the "#N" in a still-happening notice.
    delivered: int = 1
    #: Repeats suppressed since the last delivery, NOT counting this message.
    suppressed_since: int = 0
    first_seen: float = 0.0
    last_delivered: float = 0.0
    #: Conversation key the first occurrence opened, so a repeat can be folded
    #: into the thread that already exists instead of rooting another one.
    conversation: str = ''

    @property
    def suppressed(self) -> bool:
        return self.kind == 'suppressed'


@dataclass
class _Condition:
    """Per-condition state. Timestamps are epoch seconds."""

    key: str
    sender: str = ''
    #: The subject as first seen this episode — the un-normalised one, so a
    #: report can show the human something he recognises.
    sample: str = ''
    first_at: float = 0.0
    last_seen_at: float = 0.0
    last_delivered_at: float = 0.0
    occurrences: int = 0
    delivered: int = 0
    #: Suppressed since the last delivery. Reset by each delivery.
    suppressed: int = 0
    #: Suppressed across this whole episode. Never reset until the TTL expires.
    suppressed_total: int = 0
    conversation: str = ''

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        d.pop('key')
        return d


class DuplicateLimiter:
    """A persisted duplication limit over alert conditions.

    Usage mirrors `MaildirWatcher`: ask, act, then record.

        decision = limiter.decide(mail['from'], mail['subject'])
        if not decision.deliver:
            limiter.commit(decision)      # count it, say so in the log
            return
        ...deliver...
        limiter.commit(decision, conversation=key)   # only once it landed

    A limiter constructed with `enabled=False` (or a non-positive base
    interval) delivers everything and touches no disk.
    """

    def __init__(self, path: Path, *, base_interval: int = DEFAULT_BASE_INTERVAL,
                 max_interval: int = DEFAULT_MAX_INTERVAL, ttl: int = DEFAULT_TTL,
                 max_keys: int = DEFAULT_MAX_KEYS, enabled: bool = True, clock=None):
        self.path = Path(path)
        self.base_interval = int(base_interval)
        self.max_interval = max(int(max_interval), int(base_interval))
        self.ttl = int(ttl)
        self.max_keys = int(max_keys)
        self.enabled = bool(enabled) and self.base_interval > 0
        self._clock = clock or _now
        self._conditions: dict[str, _Condition] = {}
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Read state from disk. A missing or malformed file yields empty state
        — which costs one extra notification per live condition, never a
        suppressed first occurrence."""
        self._conditions = {}
        if not self.enabled or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError('not a JSON object')
            entries = raw.get('conditions', {})
            if not isinstance(entries, dict):
                raise ValueError('conditions is not an object')
        except Exception as e:
            print(f'duplicate-limiter parse error ({self.path}): {e}', file=sys.stderr)
            return

        for key, value in entries.items():
            if not isinstance(value, dict):
                continue
            try:
                self._conditions[key] = _Condition(
                    key=key,
                    sender=str(value.get('sender', '')),
                    sample=str(value.get('sample', '')),
                    first_at=float(value.get('first_at', 0) or 0),
                    last_seen_at=float(value.get('last_seen_at', 0) or 0),
                    last_delivered_at=float(value.get('last_delivered_at', 0) or 0),
                    occurrences=int(value.get('occurrences', 0) or 0),
                    delivered=int(value.get('delivered', 0) or 0),
                    suppressed=int(value.get('suppressed', 0) or 0),
                    suppressed_total=int(value.get('suppressed_total', 0) or 0),
                    conversation=str(value.get('conversation', '')),
                )
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        if not self.enabled:
            return
        payload = {
            'version': SCHEMA_VERSION,
            'conditions': {k: c.to_json() for k, c in self._conditions.items()},
        }
        write_state(self.path, json.dumps(payload, indent=2, sort_keys=True) + '\n')

    # -- the limit --------------------------------------------------------

    def interval_for(self, delivered: int) -> int:
        """How long after the Nth delivery the condition may notify again.

        Doubles per delivery and then flattens at `max_interval`. Shifting
        rather than exponentiating keeps this exact for the shift counts that
        matter and cannot overflow into a float.
        """
        if delivered < 1:
            return 0
        shift = min(delivered - 1, 30)
        return min(self.base_interval << shift, self.max_interval)

    def decide(self, sender: str, subject: str, *, now: float | None = None) -> Decision:
        """Whether this message should reach the human. Changes nothing."""
        at = self._clock() if now is None else now
        key = alert_key(sender, subject)
        if not self.enabled:
            return Decision(key=key, deliver=True, kind='off', at=at, first_seen=at)

        cond = self._conditions.get(key)
        if cond is None or at - cond.last_seen_at > self.ttl:
            # Never seen, or quiet long enough to count as news again.
            return Decision(key=key, deliver=True, kind='first', at=at,
                            occurrences=1, delivered=1, first_seen=at)

        elapsed = at - cond.last_delivered_at
        # A negative elapsed means the clock moved backwards (an NTP step, a
        # state file restored from a later boot). Deliver rather than suppress:
        # the failure mode of the other branch is a condition that stays silent
        # until wall-clock catches up, which is the harm this module exists to
        # prevent, dressed up as the fix.
        due = elapsed < 0 or elapsed >= self.interval_for(cond.delivered)
        return Decision(
            key=key,
            deliver=due,
            kind='repeat' if due else 'suppressed',
            at=at,
            occurrences=cond.occurrences + 1,
            delivered=cond.delivered + 1 if due else cond.delivered,
            suppressed_since=cond.suppressed,
            first_seen=cond.first_at,
            last_delivered=cond.last_delivered_at,
            conversation=cond.conversation,
        )

    def commit(self, decision: Decision, *, sender: str = '', subject: str = '',
               conversation: str = '') -> None:
        """Record the outcome of `decision`, and persist it.

        For a delivery, call this only once the mail has reached a surface. A
        send that failed must be retried, and a retry the limiter has already
        counted as delivered would be suppressed instead — trading a duplicate
        for a drop, which is the wrong way round.
        """
        if not self.enabled or decision.kind == 'off':
            return

        cond = self._conditions.get(decision.key)
        if cond is None or decision.kind == 'first':
            # 'first' includes "seen before, but the TTL expired": that is a new
            # episode, so its counters start over rather than inheriting the
            # backoff of an incident that ended a day ago.
            cond = _Condition(key=decision.key, first_at=decision.at)
            self._conditions[decision.key] = cond

        if sender and not cond.sender:
            cond.sender = sender
        if subject and not cond.sample:
            cond.sample = subject

        cond.occurrences += 1
        cond.last_seen_at = decision.at
        if decision.deliver:
            cond.last_delivered_at = decision.at
            cond.delivered += 1
            cond.suppressed = 0
            if conversation:
                cond.conversation = conversation
        else:
            cond.suppressed += 1
            cond.suppressed_total += 1

        self._prune()
        self.save()

    def _prune(self) -> None:
        excess = len(self._conditions) - self.max_keys
        if excess <= 0:
            return
        stale = sorted(self._conditions.values(), key=lambda c: c.last_seen_at)[:excess]
        for cond in stale:
            self._conditions.pop(cond.key, None)

    # -- reporting --------------------------------------------------------

    def __len__(self) -> int:
        return len(self._conditions)

    def summary(self, limit: int = 10) -> dict:
        """What the limiter has been holding back, as facts for the adapter.

        This is the other half of the duplication limit: a suppressed alert
        that leaves no trace is a dropped alert. Every suppression is also
        logged as it happens, and reported in the next still-happening notice;
        this is the pull view of the same numbers.
        """
        loud = sorted(self._conditions.values(),
                      key=lambda c: (-c.suppressed_total, -c.occurrences))
        return {
            'enabled': self.enabled,
            'base_interval': self.base_interval,
            'max_interval': self.max_interval,
            'ttl': self.ttl,
            'conditions': len(self._conditions),
            'suppressed_total': sum(c.suppressed_total for c in self._conditions.values()),
            'holding': sum(c.suppressed for c in self._conditions.values()),
            'top': [
                {
                    'sender': c.sender,
                    'subject': c.sample,
                    'occurrences': c.occurrences,
                    'delivered': c.delivered,
                    'suppressed': c.suppressed,
                    'suppressed_total': c.suppressed_total,
                    'first_seen': isoformat(c.first_at),
                    'last_seen': isoformat(c.last_seen_at),
                    'last_delivered': isoformat(c.last_delivered_at),
                    'next_delivery_after': self.interval_for(c.delivered),
                }
                for c in loud[:limit] if c.suppressed_total or c.occurrences > 1
            ],
        }


def _now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()
