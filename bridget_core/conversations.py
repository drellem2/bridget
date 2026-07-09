# bridget_core.conversations — conversation <-> thread map. GPL-3.0-or-later.
"""The conversation <-> thread map, persisted across restarts.

A *conversation* is a reply chain, keyed by the root message id (see
`bridget_core.mail.conversation_key`). A *thread* is whatever the presentation
adapter uses to render one — a Discord thread id, a Slack thread timestamp. The
core neither knows nor cares which; it stores an opaque integer or string.

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

SCHEMA_VERSION = 1

#: Conversations kept in the map. Oldest (by `updated_at`) are pruned first.
#: Generous — an entry is a few hundred bytes, and forgetting a conversation
#: means its next message roots a duplicate thread.
DEFAULT_MAX_CONVERSATIONS = 2000


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
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Read the map from disk. A missing or malformed file yields an empty
        map — the bridge still runs, it just re-roots threads it has forgotten.
        """
        self._conversations = {}
        self._by_thread = {}
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
                updated_at=value.get('updated_at', ''),
            )
            self._conversations[key] = conv
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
        """
        conv = self._conversations.get(key)
        if conv is None:
            conv = Conversation(key=key, subject=subject, agent=agent)
            self._conversations[key] = conv

        if message_id:
            conv.last_message_id = message_id
            if message_id not in conv.message_ids:
                conv.message_ids.append(message_id)
                # Bound per-conversation growth; only the tail is ever read.
                if len(conv.message_ids) > 50:
                    conv.message_ids = conv.message_ids[-50:]

        conv.updated_at = self._clock()
        self._prune()
        self.save()
        return conv

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
            if conv.thread_id is not None:
                self._by_thread.pop(conv.thread_id, None)
