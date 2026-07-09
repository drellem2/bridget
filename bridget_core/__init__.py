# bridget_core — transport-agnostic bridge core. GPL-3.0-or-later. See LICENSE.
"""Transport-agnostic core for the pogo chat bridge.

Nothing in this package imports Discord (or any other chat platform). It holds
the parts of the bridge that would be identical if the presentation layer were
Slack, Matrix, or a terminal:

    mail          — maildir parsing, including the correlation-ID headers
                    (Message-Id / In-Reply-To / References) that let replies
                    thread; plus conversation-key derivation.
    mailbox       — observe-only maildir scanning with seen-set de-duplication.
    conversations — the conversation <-> thread map, persisted across restarts.
    settings      — live-tunable mute/settings state, persisted.
    acks          — the delivery / ambiguity / undeliverable outcome model.

The Discord presentation adapter lives in the top-level `bridget` script: DM
cards, guild threads, and the slash/keyword command surface. Keeping the split
means porting the bridge to another platform is a new adapter, not a rewrite.
"""

from .acks import Ack, ambiguous, delivered, undeliverable
from .conversations import Conversation, ConversationStore
from .mail import conversation_key, parse_mail
from .mailbox import MaildirWatcher
from .settings import SettingsStore

__all__ = [
    'Ack',
    'Conversation',
    'ConversationStore',
    'MaildirWatcher',
    'SettingsStore',
    'ambiguous',
    'conversation_key',
    'delivered',
    'parse_mail',
    'undeliverable',
]
