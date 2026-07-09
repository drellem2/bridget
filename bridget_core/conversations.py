# bridget_core.conversations — conversation <-> thread map. GPL-3.0-or-later.
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
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: v2 added `posted_ids`. A v1 file loads cleanly — the field defaults to empty,
#: which costs at most one duplicate post for a mail that was in flight across
#: the upgrade.
SCHEMA_VERSION = 2

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
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError('not a JSON object')
            entries = raw.get('conversations', {})
            if not isinstance(entries, dict):
                raise ValueError('conversations is not an object')
        except Exception as e:
            print(f'conversation store parse error ({self.path}): {e}', file=sys.stderr)
            return

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
                updated_at=value.get('updated_at', ''),
            )
            self._conversations[key] = conv
            self._index(conv)
            if conv.thread_id is not None:
                self._by_thread[conv.thread_id] = key

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'version': SCHEMA_VERSION,
            'conversations': {k: c.to_json() for k, c in self._conversations.items()},
        }
        tmp = self.path.parent / (self.path.name + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
        os.replace(tmp, self.path)

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
            conv.posted_ids = conv.posted_ids[-MAX_MESSAGE_IDS:]
        self.save()

    def bind_thread(self, key: str, thread_id) -> Conversation | None:
        """Attach an adapter thread handle to a conversation."""
        conv = self._conversations.get(key)
        if conv is None:
            return None
        if conv.thread_id is not None and conv.thread_id != thread_id:
            self._by_thread.pop(conv.thread_id, None)
        conv.thread_id = thread_id
        conv.updated_at = self._clock()
        self._by_thread[thread_id] = key
        self.save()
        return conv

    def forget(self, key: str) -> bool:
        conv = self._conversations.pop(key, None)
        if conv is None:
            return False
        self._deindex(conv)
        if conv.thread_id is not None:
            self._by_thread.pop(conv.thread_id, None)
        self.save()
        return True

    def _prune(self) -> None:
        """Drop the least-recently-updated conversations past the cap."""
        excess = len(self._conversations) - self.max_conversations
        if excess <= 0:
            return
        stale = sorted(self._conversations.values(), key=lambda c: c.updated_at)[:excess]
        for conv in stale:
            self._conversations.pop(conv.key, None)
            self._deindex(conv)
            if conv.thread_id is not None:
                self._by_thread.pop(conv.thread_id, None)
