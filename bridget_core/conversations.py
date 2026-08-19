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

# bridget_core.conversations — conversation <-> thread map.
"""The conversation <-> thread map, persisted across restarts.

A *conversation* is a reply chain. A *thread* is whatever the presentation
adapter uses to render one — a Discord thread id, a Slack thread timestamp. The
core neither knows nor cares which; it stores an opaque integer or string.

The conversation's key is the id of the message that first rooted it, but a
later message in the chain cannot be counted on to *name* that root: `mg mail
send --in-reply-to X` seeds `References: [X]` and nothing else, so from the
second hop onward the chain carries only the parent. The store therefore keeps
a message-id -> key index over every message it has folded in, and `resolve()`
walks a message's ancestry against it. Without that index, threading would
survive exactly one round-trip before every reply rooted a fresh thread.

For the index to span the round-trip, the bridge must fold in the ids of the
replies *it* sends as well as the mail it receives — an agent replying to our
reply names our message id, which we would otherwise never have seen.

Persistence is the point. Discord threads outlive the bridge process, so if the
map lived only in memory a restart would orphan every open thread and root a
duplicate for the next message in each conversation. The store is written
atomically (temp file + `os.replace`) so a crash mid-write cannot leave a
truncated JSON file behind.

The store also records, per conversation, whether its thread is believed *live*
on the presentation side — see `thread_live`. Conversations are cheap and the
map holds thousands; live threads are not, because a chat client has to render
every one of them in the channel they hang off. 966 of them in one Discord
channel stopped the client rendering that channel at all (mg-27e0). The core
does not archive anything — that is the adapter's call, against its own API —
but it owns the *bookkeeping* the adapter's bound is enforced against, because a
bound whose count lives only in memory is re-inflated by the next restart.
"""
from __future__ import annotations

import datetime
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .statefile import write_state

#: v2 added `posted_ids`. A v1 file loads cleanly — the field defaults to empty,
#: which costs at most one duplicate post for a mail that was in flight across
#: the upgrade.
#:
#: v3 added `thread_live`. An older file loads cleanly and every entry in it
#: reads as NOT live, which is deliberate: the 966 threads already open when
#: this shipped are a backlog to be archived separately and with the human's
#: say-so (mg-27e0), not something an upgrade should mass-archive on its own.
#: The bound therefore starts from zero and counts only what this build admits
#: — and a pre-existing thread re-enters the count the moment it is next used,
#: so the live set converges on the bound through use rather than through a
#: sweep.
SCHEMA_VERSION = 3

#: Conversations kept in the map. Oldest (by `updated_at`) are pruned first.
#: Generous — an entry is a few hundred bytes, and forgetting a conversation
#: means its next message roots a duplicate thread.
DEFAULT_MAX_CONVERSATIONS = 2000

#: Message ids remembered per conversation, newest last. These are what
#: `resolve()` matches an incoming reply against, so the cap is also the depth
#: of ancestry a straggler can name and still find its way home. A reply names
#: its parent, which is the newest id here, so the tail is what matters.
MAX_MESSAGE_IDS = 50


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


@dataclass
class Conversation:
    """One reply chain and the thread rendering it."""

    key: str
    #: Adapter-owned thread handle. None until the adapter opens a thread.
    thread_id: int | str | None = None
    #: Display subject, taken from the message that rooted the conversation.
    subject: str = ''
    #: The agent on the other end — who a reply in this thread gets mailed to.
    agent: str = ''
    #: Id of the most recent message seen. A reply threads onto *this*, so the
    #: ancestry stays linear rather than always branching off the root.
    last_message_id: str = ''
    #: Maildir filenames folded into this conversation, newest last. Bounded.
    message_ids: list[str] = field(default_factory=list)
    #: Ids the adapter has already rendered into the thread. Delivery is
    #: at-least-once, so a mail can arrive here twice; this is what stops the
    #: second arrival from posting a duplicate. A subset of `message_ids`.
    posted_ids: list[str] = field(default_factory=list)
    #: Whether the adapter's thread for this conversation is believed to be
    #: OPEN — rendered by the chat client, counting against the live-thread
    #: bound. False means archived, never opened, or (on an upgrade from a v2
    #: file) not yet accounted for. The adapter reconciles this against what it
    #: observes: a thread it finds archived is marked False before anything else
    #: happens to it, so the count tracks the platform rather than drifting.
    thread_live: bool = False
    updated_at: str = ''

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop('key')  # the key is the dict key; don't duplicate it
        return d


class ConversationStore:
    """A persisted map of conversation-key -> Conversation.

    Call sites mutate through `record()` / `bind_thread()`, each of which
    flushes to disk. Reads are served from memory.
    """

    def __init__(self, path: Path, max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
                 clock=_utcnow):
        self.path = Path(path)
        self.max_conversations = max_conversations
        self._clock = clock
        self._conversations: dict[str, Conversation] = {}
        self._by_thread: dict[object, str] = {}
        self._by_message: dict[str, str] = {}
        #: Keys whose thread is believed open, in TOUCH ORDER — least recently
        #: used first. A dict, not a set, for that ordering: `updated_at` is
        #: only second-granular, so ordering the eviction queue by it collapses
        #: to alphabetical within any one second — and a burst is exactly when
        #: the queue is consulted and exactly when every entry shares a second.
        #: An LRU policy that is really alphabetical-order would evict the
        #: conversation the human is reading as readily as a dead one.
        self._live: dict[str, None] = {}
        #: Threads carried by a file written before liveness was tracked, and so
        #: open on the platform but charged to nobody. Non-zero for exactly one
        #: run — the upgrade — because the first save rewrites the file at the
        #: current schema. It exists to be SAID: a bound that silently reports
        #: "0 open" while ~1000 threads it did not open are crowding the channel
        #: is the same unreadable instrument this all came from.
        self.legacy_thread_count = 0
        self.load()

    # -- the message-id index ---------------------------------------------

    def _index(self, conv: Conversation) -> None:
        """Point every id this conversation owns at its key."""
        self._by_message[conv.key] = conv.key
        for mid in conv.message_ids:
            self._by_message[mid] = conv.key

    def _deindex(self, conv: Conversation) -> None:
        """Drop this conversation's ids. An id another conversation has since
        claimed stays put — the newer owner is the right answer."""
        for mid in [conv.key, *conv.message_ids]:
            if self._by_message.get(mid) == conv.key:
                self._by_message.pop(mid, None)

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Read the map from disk. A missing or malformed file yields an empty
        map — the bridge still runs, it just re-roots threads it has forgotten.
        """
        self._conversations = {}
        self._by_thread = {}
        self._by_message = {}
        self._live = {}
        self.legacy_thread_count = 0
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError('not a JSON object')
            entries = raw.get('conversations', {})
            if not isinstance(entries, dict):
                raise ValueError('conversations is not an object')
            try:
                file_version = int(raw.get('version', 1))
            except (TypeError, ValueError):
                file_version = 1
        except Exception as e:
            print(f'conversation store parse error ({self.path}): {e}', file=sys.stderr)
            return

        # Touch order is in-memory only; `updated_at` is the coarse record of it
        # that survives a restart, so the restored queue is seeded from that and
        # refines itself from the first message onward.
        restored_live: list[Conversation] = []
        for key, value in entries.items():
            if not isinstance(value, dict):
                continue
            conv = Conversation(
                key=key,
                thread_id=value.get('thread_id'),
                subject=value.get('subject', ''),
                agent=value.get('agent', ''),
                last_message_id=value.get('last_message_id', ''),
                message_ids=list(value.get('message_ids', []) or []),
                posted_ids=list(value.get('posted_ids', []) or []),
                thread_live=bool(value.get('thread_live', False)),
                updated_at=value.get('updated_at', ''),
            )
            self._conversations[key] = conv
            self._index(conv)
            if conv.thread_id is not None:
                self._by_thread[conv.thread_id] = key
                if conv.thread_live:
                    restored_live.append(conv)
                    continue
            # A live flag without a thread is nonsense — a deleted thread that
            # left its flag behind. Drop the flag rather than counting a slot
            # nothing occupies.
            conv.thread_live = False

        restored_live.sort(key=lambda c: (c.updated_at, c.key))
        for conv in restored_live:
            self._live[conv.key] = None

        if file_version < 3:
            # Every thread this file names is unaccounted for. Whether it is
            # still open is not knowable from here — the platform owns that —
            # so this is an upper bound, and the caller says so when it prints
            # it. Each one is charged for its slot the moment it is next used.
            self.legacy_thread_count = sum(
                1 for c in self._conversations.values() if c.thread_id is not None)

    def save(self) -> None:
        payload = {
            'version': SCHEMA_VERSION,
            'conversations': {k: c.to_json() for k, c in self._conversations.items()},
        }
        write_state(self.path, json.dumps(payload, indent=2, sort_keys=True) + '\n')

    # -- reads ------------------------------------------------------------

    def get(self, key: str) -> Conversation | None:
        return self._conversations.get(key)

    def resolve(self, candidates) -> str | None:
        """The key of the conversation owning the first of `candidates` we know.

        `candidates` is a message's ancestry, nearest first — see
        `bridget_core.mail.correlation_candidates`. Returns None when we have
        seen none of them, which means the message roots a new conversation.
        """
        for candidate in candidates:
            key = self._by_message.get(candidate)
            if key is not None and key in self._conversations:
                return key
        return None

    def by_thread(self, thread_id) -> Conversation | None:
        """Resolve the conversation a thread renders. This is the inbound path:
        the human types in a thread, and the adapter needs to know who to mail.
        """
        key = self._by_thread.get(thread_id)
        return self._conversations.get(key) if key is not None else None

    def is_thread_live(self, key: str) -> bool:
        """True if `key`'s thread is believed open, and so already occupies a
        slot under the adapter's live-thread bound."""
        return key in self._live

    def live_count(self) -> int:
        """How many threads this store believes are open."""
        return len(self._live)

    def lru_live(self, *, exclude: str = '', limit: int | None = None) -> list[Conversation]:
        """Live conversations, least recently used first.

        This is the eviction order for the adapter's bound. Every `record()`
        and `bind_thread()` moves a conversation to the back of the queue —
        including the ones the bridge makes when the human's OWN reply is sent
        — so a conversation the human is actually in is the last thing this
        offers up.
        """
        out = []
        for key in self._live:
            if key == exclude:
                continue
            conv = self._conversations.get(key)
            if conv is None:
                continue
            out.append(conv)
            if limit is not None and len(out) >= limit:
                break
        return out

    def __len__(self) -> int:
        return len(self._conversations)

    def __contains__(self, key: str) -> bool:
        return key in self._conversations

    def keys(self):
        return self._conversations.keys()

    def values(self):
        return self._conversations.values()

    # -- writes -----------------------------------------------------------

    def record(self, key: str, *, subject: str = '', agent: str = '',
               message_id: str = '') -> Conversation:
        """Fold a message into its conversation, creating the entry if new.

        `subject` and `agent` are set once, when the conversation is created —
        a later message in the same chain does not rename the thread out from
        under the human, and a reply from a different sender does not silently
        redirect where the human's replies go.

        Call this for the replies the bridge *sends*, too, passing the id mg
        assigned them. The agent's next reply names that id and nothing older,
        so a conversation that never records its own outbound ids goes dark to
        `resolve()` after one round-trip.
        """
        conv = self._conversations.get(key)
        if conv is None:
            conv = Conversation(key=key, subject=subject, agent=agent)
            self._conversations[key] = conv
        self._by_message[key] = key

        if message_id:
            conv.last_message_id = message_id
            if message_id not in conv.message_ids:
                conv.message_ids.append(message_id)
                # Bound per-conversation growth; only the tail is ever read.
                if len(conv.message_ids) > MAX_MESSAGE_IDS:
                    dropped = conv.message_ids[:-MAX_MESSAGE_IDS]
                    conv.message_ids = conv.message_ids[-MAX_MESSAGE_IDS:]
                    for mid in dropped:
                        if mid != key and self._by_message.get(mid) == key:
                            self._by_message.pop(mid, None)
            self._by_message[message_id] = key

        conv.updated_at = self._clock()
        # Folding a message in is a use, so it moves the conversation to the
        # back of the eviction queue. This is what protects the thread the
        # human is actually reading: their own reply is recorded here too.
        if key in self._live:
            self._touch_live(key)
        self._prune()
        self.save()
        return conv

    def was_posted(self, key: str, message_id: str) -> bool:
        """True if the adapter has already rendered `message_id` into `key`'s
        thread. The guard against a redelivery posting the same mail twice."""
        conv = self._conversations.get(key)
        return bool(conv and message_id and message_id in conv.posted_ids)

    def mark_posted(self, key: str, message_id: str) -> None:
        """Record that `message_id` is now in `key`'s thread.

        Call this *after* the post succeeds, never before: a post that failed
        must be retried on the next poll, and a post recorded optimistically
        would be skipped instead — trading a duplicate for a drop, which is the
        wrong way round.
        """
        conv = self._conversations.get(key)
        if conv is None or not message_id or message_id in conv.posted_ids:
            return
        conv.posted_ids.append(message_id)
        if len(conv.posted_ids) > MAX_MESSAGE_IDS:
            # Bounded like `message_ids`. Eviction can only ever cost a duplicate
            # post — never a lost mail — which is the safe direction, and it
            # takes more than MAX_MESSAGE_IDS undelivered mails in one
            # conversation to reach.
            conv.posted_ids = conv.posted_ids[-MAX_MESSAGE_IDS:]
        self.save()

    def _touch_live(self, key: str) -> None:
        """Move `key` to the back of the eviction queue — most recently used.

        A plain re-assignment would NOT do this: a dict keeps a key's original
        insertion position when its value is overwritten, so the entry would
        stay wherever it first landed and the queue would be creation order
        wearing an LRU label.
        """
        self._live.pop(key, None)
        self._live[key] = None

    def mark_thread_live(self, key: str) -> bool:
        """Record that `key`'s thread is open. Returns True if this changed
        anything — i.e. if the conversation has just taken a slot.

        Call this only once the adapter has actually made room; the store does
        not enforce the bound, it just remembers who is holding a slot so the
        next process can enforce it against the same numbers.
        """
        conv = self._conversations.get(key)
        if conv is None or conv.thread_id is None or conv.thread_live:
            return False
        conv.thread_live = True
        self._touch_live(key)
        self.save()
        return True

    def mark_threads_archived(self, keys) -> int:
        """Record that these threads are closed. Returns how many changed.

        Plural and one write: eviction retires a batch, and a per-key save would
        rewrite a half-megabyte file once per victim.
        """
        changed = 0
        for key in keys:
            conv = self._conversations.get(key)
            if conv is None or not conv.thread_live:
                self._live.pop(key, None)
                continue
            conv.thread_live = False
            self._live.pop(key, None)
            changed += 1
        if changed:
            self.save()
        return changed

    def mark_thread_archived(self, key: str) -> bool:
        """Record that one thread is closed. Returns True if it was open."""
        return bool(self.mark_threads_archived([key]))

    def bind_thread(self, key: str, thread_id) -> Conversation | None:
        """Attach an adapter thread handle to a conversation."""
        conv = self._conversations.get(key)
        if conv is None:
            return None
        if conv.thread_id is not None and conv.thread_id != thread_id:
            self._by_thread.pop(conv.thread_id, None)
            # Re-rooting onto a different thread — the old one was deleted. The
            # new thread holds nothing, so everything posted into the old one is
            # gone with it and a redelivery must be free to post again. Without
            # this, `was_posted` would suppress the repost on the strength of a
            # record about a thread that no longer exists.
            conv.posted_ids.clear()
        conv.thread_id = thread_id
        # A thread the adapter has just opened is by definition open, and takes
        # a slot under the live-thread bound from this moment. The adapter is
        # required to have made room BEFORE calling this — see
        # `make_room_for_thread` in the Discord adapter.
        conv.thread_live = True
        self._touch_live(key)
        conv.updated_at = self._clock()
        self._by_thread[thread_id] = key
        self.save()
        return conv

    def forget(self, key: str) -> bool:
        conv = self._conversations.pop(key, None)
        if conv is None:
            return False
        self._deindex(conv)
        self._live.pop(key, None)
        if conv.thread_id is not None:
            self._by_thread.pop(conv.thread_id, None)
        self.save()
        return True

    def _prune(self) -> None:
        """Drop the least-recently-updated conversations past the cap.

        Forgetting a conversation also forgets that its thread was open, which
        would leak a slot under the adapter's live-thread bound — the thread
        stays on the platform, uncounted. It does not happen in practice
        because this prunes the least-recently-updated of thousands while the
        live set is the few hundred most recent (admission always follows a
        `record()`, which bumps `updated_at`), but the arithmetic is worth
        stating rather than relying on.
        """
        excess = len(self._conversations) - self.max_conversations
        if excess <= 0:
            return
        stale = sorted(self._conversations.values(), key=lambda c: c.updated_at)[:excess]
        for conv in stale:
            self._conversations.pop(conv.key, None)
            self._deindex(conv)
            self._live.pop(conv.key, None)
            if conv.thread_id is not None:
                self._by_thread.pop(conv.thread_id, None)
