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

# bridget_core.inbound — the receiving half's instrument.
"""What bridget writes down when a message comes IN, and what it does about a
message that never arrived.

mg-879c instrumented the OUTBOUND path: an outage writes `relay-stall:`, an
unwired agent writes `agent-mail:`. The inbound half got nothing, because there
was nothing there to repair. `grep -n "print(" bridget`, restricted to
`on_message` / `handle_command` / `reply_in_conversation` /
`handle_channel_message`, returned **zero**. Not one line was written when a
message from Daniel arrived, was handled, was refused, or was ignored. The only
surviving receipt was the 👀/✅/❌ reaction on his own Discord message, and that
exists solely on the thread path: the DM path answers with text, and the
unmapped-guild-channel path `return`s in silence.

What that cost, measured by a read-only REST sweep of every guild channel and
the human DM on 2026-08-19:

    08-15T15:55:06Z  DM  "mail have pm-onethird mail me an update please"  -> mayor/  OK
    08-19T07:37:05Z  DM  "Mail pause one third once the executive ..."     -> NOWHERE
    08-19T07:39:59Z  DM  "Mail pause one third once the executive ..."     -> NOWHERE

Both 08-19 messages arrived inside the 07:30:00–07:38:10Z resolver wedge
(mg-3f08). The verb parse is case-insensitive and `mail_recipient` defaults to
`mayor`, so had `on_message` fired, mail would exist. None does, in any of the
seven watched boxes, and no error was logged anywhere. The gateway was down; the
process was SIGTERMed at 07:43:06 and re-IDENTIFYed at 07:43:11, and **Discord
does not replay across a fresh IDENTIFY**. Daniel retried once, three minutes
apart, got no answer either time, and reported the bridge as "flaky".

Three separate things are missing, and each is a different kind of missing.

**1. A receipt, because a handled message left no trace.** A `recv` line and a
`done` line under the `inbound:` grep token — deliberately distinct from
`relay:`, `relay-stall:` and `agent-mail:`, so `grep -c` over any one of them
counts that one thing and only that thing.

*Why two lines and not one.* The ticket asks for "a single stdout line per
inbound message" and one line is enough to answer "did it arrive?" — but only if
the handler returns. A line emitted after routing cannot record a message whose
handler raised, blocked on a hung `mg`, or was killed mid-flight, and that is
precisely the failure class this ticket exists to make visible. So `recv` is
written *before* any routing decision and `done` after it, and a `recv` with no
matching `done` is itself the finding. At Daniel's measured volume — three
messages in five days — the second line costs nothing worth counting.

*What is NOT receipted, said out loud.* Messages from bots and from authors
other than the configured `DISCORD_USER_ID` are dropped before the receipt.
bridget is a single-human bridge; receipting every bot post in a shared guild
would bury the three lines a day that matter under its own outbound traffic. The
consequence is real and is the price: if someone other than Daniel types at
bridget, the file still says nothing.

**2. Gateway state, because "no inbound" had two meanings.** The last line of
`bridget` is `client.run(TOKEN, log_handler=None)`, which suppresses discord.py's
own connect/disconnect/resume logging outright. Nothing in either log file said
the gateway dropped on 08-16 or came back on 08-19; the only evidence was a bare
`logged in as` banner appearing mid-run with no supervise line above it. A reader
could not tell "no inbound because nobody typed" from "no inbound because we were
not connected" — the same ambiguity mg-879c removed on the outbound side.

`GatewayJournal` records the transitions under a `gateway:` token. The
load-bearing distinction is not up/down, it is **RESUME versus IDENTIFY**:

    disconnect -> RESUME    the gateway replays what was missed. Nothing is lost.
    disconnect -> IDENTIFY  a fresh session. Discord replays NOTHING. Every
                            message sent while the socket was down is gone
                            unless something goes and fetches it.

so the re-IDENTIFY line names that consequence in the log rather than leaving
it as folklore, and it is what makes the sweep below legible when it runs.

*Why not just pass a `log_handler`.* Re-enabling discord.py's logging would fix
the connect/disconnect gap and, in the same motion, pour every library warning
into the one file Daniel greps. Four explicit handlers say the four things that
matter, in the fleet's own stamped format.

**3. Gap recovery, because a log cannot record what never arrived.** This is the
part a log line genuinely cannot do. The two lost messages were fetchable by REST
the whole time — `GET /channels/{id}/messages?after=<last_seen_id>` is exactly how
they were found — so a bounded catch-up sweep on `on_ready` would have recovered
both. `SeenStore` is the state that makes it possible: the last message id
bridget has actually finished routing, per surface, persisted.

Four decisions inside that, all of which could have gone the other way:

- **At-least-once, not at-most-once.** The id is marked *after* routing
  completes, and NOT AT ALL when routing raised. A crash between handling and
  marking therefore replays the message rather than dropping it. Duplicate mail
  is visible to its recipient and can be ignored; a lost message is invisible to
  everyone, which is the entire subject of this ticket. When in doubt the sweep
  does the thing that can be seen. A message that always raises does not become
  a poison pill, because `mark` is monotonic: any later message on the same
  surface advances the resume point past it, so it is retried only while it is
  still the newest thing there.
- **First run adopts the head and replays nothing.** With no persisted entry for
  a surface, bridget records the current newest message id and sweeps zero. The
  alternative — treating "no state" as "replay everything" — turns the first
  start after an upgrade into a re-run of Daniel's entire DM history, mailing
  agents from months-old commands.
- **The caps are logged when they bite.** A sweep truncated by
  `limit` or by `max_age` says so and names the knob. A bounded sweep that
  reports nothing about its own bound reads as "we covered everything", which is
  the defect wearing a fix's clothes.
- **Threads are not swept.** The ticket scopes the sweep to the DM channel and
  each inbound-mapped channel, and that is what this does. Sweeping conversation
  threads instead would be up to `BRIDGET_MAX_LIVE_THREADS` (default 50) extra
  REST calls on every reconnect. So a thread reply typed while the gateway is
  down is still lost — this is a residual gap, the adapter's catch-up status
  line states it on every start, and it is not silent.

If the sweep is switched off, the adapter's re-IDENTIFY notice still tells the
reader that a fresh IDENTIFY may have missed messages, which is the ticket's
fallback ask: bridget should say it might be incomplete rather than print
`logged in as` and appear whole.

**Nothing here renders**, per the package rule. This module holds the facts —
`GatewayEvent`, `CatchupPlan`, `CatchupResult`, the `SeenStore` — and the
`format_inbound_*` / `format_gateway` / `format_catchup*` adapters in `bridget`
turn them into lines, the way `RelayLedger` and `format_relay_beat` divide the
same work. Nothing here touches discord or the filesystem beyond one small JSON
file; the routing, the fetching and the posting all live in `bridget`.

**The remedy is an artifact of the same kind as the defect.** Three ways this
fix could itself lose or double a message were enumerated and closed, and they
are listed here because the enumeration is the part nothing verifies:

1. Two sweeps overlapping. `on_ready` is dispatched as a task and fires on every
   reconnect, so a reconnect during a slow sweep runs two over one resume point
   — a check-then-act race, the shape of the loss being repaired. Serialised by
   a lock in the adapter.
2. The live path and the sweep both routing one message. The gateway can deliver
   while the sweep is paging, so the sweep re-reads the mark per message rather
   than trusting the plan it took before the first fetch.
3. The bookkeeping dropping what the record just made visible. See the
   at-least-once note above: an errored message is logged and left unmarked.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from .statefile import write_state

#: The grep token for everything on the receiving path. Distinct from `relay:`
#: (a delivery beat), `relay-stall:` (a delivery outage) and `agent-mail:` (an
#: unwired channel) so that counting one of them never counts another.
INBOUND_TOKEN = 'inbound:'

#: The grep token for gateway transitions. Separate from INBOUND_TOKEN because
#: "the socket dropped" and "a message arrived" are different questions, and a
#: reader asking one should not have to filter out the other.
GATEWAY_TOKEN = 'gateway:'

#: The catch-up sweep's own token, for the same reason.
CATCHUP_TOKEN = 'inbound-catchup:'

SCHEMA_VERSION = 1

#: Per-surface ceiling on how many missed messages one sweep will replay. Sized
#: well above the observed rate (3 messages in 5 days) and well below "a day of
#: someone else's busy channel", so it is a runaway guard rather than a policy.
DEFAULT_CATCHUP_LIMIT = 50

#: How stale a missed message may be and still be acted on, in seconds. A
#: `mail` command from last week is not a request any more, and replaying it
#: would mail an agent on Daniel's behalf about something he has long since
#: handled by other means.
DEFAULT_CATCHUP_MAX_AGE = 86400


def _now() -> float:
    return datetime.datetime.now(datetime.timezone.utc).timestamp()


def isostamp(timestamp: float) -> str:
    """Epoch seconds as `2026-08-19T07:37:05Z` — the fleet's log stamp format.

    The `Z` form of `bridget_core.logstamp`, not the `+00:00` form, because this
    value is read inside a line whose own prefix is the `Z` form.
    """
    return datetime.datetime.fromtimestamp(
        timestamp, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def humanize(seconds: float) -> str:
    """`93s`, `4m52s`, `71h36m` — compact enough to sit inside a log line."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    return f'{seconds // 3600}h{(seconds % 3600) // 60:02d}m'


# --------------------------------------------------------------------------
# 1. the receipt
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# 2. gateway state
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GatewayEvent:
    """One transition, as facts. The adapter below turns it into a line."""
    kind: str            # 'connected' | 'disconnected' | 'resumed' | 'identified'
    at: float
    down_for: float = 0.0
    up_for: float = 0.0
    identifies: int = 0
    lossy: bool = False


class GatewayJournal:
    """Tracks the socket so the log can distinguish quiet from disconnected.

    Per process, not persisted: a gap that spans a restart is the supervisor's
    to describe, and `bridget-supervise` already stamps its own lines around the
    exec. What this owns is what happens *within* one run, which is exactly what
    was invisible — `on_ready` fires again on every gateway reconnect and printed
    nothing but `logged in as`.

        journal = GatewayJournal()
        print(format_gateway(journal.connected()), flush=True)
        print(format_gateway(journal.disconnected()), flush=True)
        print(format_gateway(journal.resumed()), flush=True)     # replayed
        print(format_gateway(journal.identified()), flush=True)  # NOT replayed
    """

    def __init__(self, clock=None):
        self._clock = clock or _now
        self.started = self._clock()
        self.identifies = 0
        self.resumes = 0
        self.disconnects = 0
        self._up_since: float | None = None
        self._down_since: float | None = None

    def connected(self) -> GatewayEvent:
        now = self._clock()
        down = 0.0 if self._down_since is None else max(0.0, now - self._down_since)
        self._up_since = now
        return GatewayEvent(kind='connected', at=now, down_for=down)

    def disconnected(self) -> GatewayEvent:
        now = self._clock()
        up = 0.0 if self._up_since is None else max(0.0, now - self._up_since)
        self._down_since = now
        self._up_since = None
        self.disconnects += 1
        return GatewayEvent(kind='disconnected', at=now, up_for=up)

    def resumed(self) -> GatewayEvent:
        now = self._clock()
        down = 0.0 if self._down_since is None else max(0.0, now - self._down_since)
        self._down_since = None
        self._up_since = now
        self.resumes += 1
        # A RESUME replays the events buffered while the socket was down. This
        # is the non-lossy reconnect, and saying so is what stops a reader
        # treating every blip as a possible message loss.
        return GatewayEvent(kind='resumed', at=now, down_for=down, lossy=False)

    def identified(self) -> GatewayEvent:
        now = self._clock()
        down = 0.0 if self._down_since is None else max(0.0, now - self._down_since)
        self._down_since = None
        self._up_since = now
        self.identifies += 1
        # A fresh IDENTIFY starts a new session. Discord replays nothing across
        # it — which is how Daniel's two 08-19 messages died. `lossy` is False
        # only for the very first IDENTIFY of a run, where there is no earlier
        # session whose messages could have gone missing.
        return GatewayEvent(kind='identified', at=now, down_for=down,
                            identifies=self.identifies,
                            lossy=self.identifies > 1)

    @property
    def reconnected(self) -> bool:
        """True once this run has re-established the session at least once."""
        return self.identifies > 1


# --------------------------------------------------------------------------
# 3. gap recovery: what we have already routed, per surface
# --------------------------------------------------------------------------

def dm_surface(user_id: int) -> str:
    """Stable key for the human's DM. Keyed on the USER, not on the DM channel
    snowflake, because the channel id is discovered lazily (`create_dm`) and a
    key that only exists after a round trip cannot be looked up before one."""
    return f'dm:{user_id}'


def channel_surface(snowflake: int) -> str:
    """Stable key for a guild text channel."""
    return f'channel:{snowflake}'


class SeenStore:
    """The last message id bridget has finished routing, per swept surface.

    Small on purpose. Only surfaces the sweep actually reads get an entry —
    the human DM and inbound-mapped channels — so the file does not grow one
    row per conversation thread over a year of use, and every key in it means
    the same thing: *this is where a catch-up sweep should resume*.

    A missing or corrupt file is not fatal and not silent: it degrades to
    "bootstrap every surface from its current head", which replays nothing, and
    the caller says so in the log.
    """

    def __init__(self, path: Path, clock=None):
        self.path = Path(path)
        self._clock = clock or _now
        self._seen: dict[str, int] = {}
        self.load_error: str = ''
        self.write_error: str = ''

    def load(self) -> 'SeenStore':
        if not self.path.exists():
            return self
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            self.load_error = str(e)
            return self
        if not isinstance(data, dict):
            self.load_error = 'not a JSON object'
            return self
        for key, value in (data.get('seen') or {}).items():
            try:
                self._seen[str(key)] = int(value)
            except (TypeError, ValueError):
                # One unreadable row must not discard the others: the rows are
                # independent, and dropping the lot would re-bootstrap every
                # surface and hide a real gap behind a clean start.
                continue
        return self

    def last_seen(self, surface: str) -> int | None:
        return self._seen.get(surface)

    def known(self, surface: str) -> bool:
        return surface in self._seen

    def mark(self, surface: str, message_id: int) -> bool:
        """Record `message_id` as routed on `surface`. Persists immediately.

        Monotonic per surface: a lower id never overwrites a higher one, so
        replaying an old message during a sweep cannot rewind the resume point
        and make the next sweep re-fetch everything after it. Returns whether
        anything changed.
        """
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            return False
        current = self._seen.get(surface)
        if current is not None and message_id <= current:
            return False
        self._seen[surface] = message_id
        self._flush()
        return True

    def forget(self, surface: str) -> None:
        """Drop a surface, so the next sweep bootstraps it from its head.

        For a channel that has been unmapped: keeping its row would leave the
        file claiming a resume point for a surface nothing reads.
        """
        if self._seen.pop(surface, None) is not None:
            self._flush()

    def surfaces(self) -> list[str]:
        return sorted(self._seen)

    def _flush(self) -> None:
        payload = {
            'version': SCHEMA_VERSION,
            'updated_at': isostamp(self._clock()),
            'seen': {k: str(v) for k, v in sorted(self._seen.items())},
        }
        try:
            write_state(self.path, json.dumps(payload, indent=2) + '\n')
            self.write_error = ''
        except OSError as e:
            # An unwritable state file costs replays, never drops: the surface
            # simply resumes from its previous mark. Recorded rather than
            # raised, because failing the inbound path to protect its own
            # bookkeeping would be worse than the bookkeeping being stale.
            self.write_error = str(e)

    #: Message ids are Discord snowflakes: the top 42 bits are a millisecond
    #: timestamp since 2015-01-01, so `id > other` is `sent after` and an id
    #: can be constructed from a time to bound a sweep.
    DISCORD_EPOCH_MS = 1420070400000

    @classmethod
    def snowflake_for(cls, timestamp: float) -> int:
        """The smallest snowflake that could have been minted at `timestamp`."""
        ms = max(0, int(timestamp * 1000) - cls.DISCORD_EPOCH_MS)
        return ms << 22

    @classmethod
    def snowflake_time(cls, message_id: int) -> float:
        """Epoch seconds a snowflake was minted at."""
        return ((int(message_id) >> 22) + cls.DISCORD_EPOCH_MS) / 1000.0


@dataclass(frozen=True)
class CatchupPlan:
    """What one surface's sweep should ask Discord for, decided before asking.

    `after` is the resume snowflake, `limit` the page bound. `bootstrap` means
    there is no resume point yet, so the sweep adopts the head and replays
    nothing. `floor_reason` records which bound produced `after` when it was
    not simply the last-seen id — the sweep logs it, so a truncation is never
    inferred from a count that looks small.
    """
    surface: str
    after: int | None
    limit: int
    bootstrap: bool
    floor_reason: str = ''


def plan_catchup(store: SeenStore, surface: str, *, limit: int,
                 max_age: float, now: float | None = None) -> CatchupPlan:
    """Decide the sweep for one surface without performing it.

    Pure, so the awkward cases can be tested without a Discord: no state at all
    (bootstrap), a resume point older than `max_age` (clamped forward, and the
    clamp is named), and the ordinary case.
    """
    now = _now() if now is None else now
    if not store.known(surface):
        return CatchupPlan(surface=surface, after=None, limit=limit,
                           bootstrap=True)
    after = store.last_seen(surface)
    age_floor = SeenStore.snowflake_for(now - max_age)
    if after is not None and after < age_floor:
        # The resume point predates the age bound. Sweeping from it would fetch
        # a day-plus of history and act on commands that have long since
        # expired, so we move the floor forward — and say which bound did it,
        # because a sweep that returns 0 after silently skipping a week reads
        # exactly like a sweep that found nothing to do.
        return CatchupPlan(surface=surface, after=age_floor, limit=limit,
                           bootstrap=False,
                           floor_reason=f'clamped to max_age={int(max_age)}s')
    return CatchupPlan(surface=surface, after=after, limit=limit,
                       bootstrap=False)


@dataclass(frozen=True)
class CatchupResult:
    """What one surface's sweep actually did."""
    surface: str
    fetched: int = 0
    replayed: int = 0
    skipped_other_author: int = 0
    skipped_too_old: int = 0
    capped: bool = False
    bootstrap: bool = False
    head: int | None = None
    error: str = ''
