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
    mgshim        — the mg CLI seam: detect whether this build of mg supports
                    correlation IDs, and degrade cleanly when it does not.
    statefile     — atomic, owner-only writes for everything above.

The Discord presentation adapter lives in the top-level `bridget` script: DM
cards, guild threads, and the slash/keyword command surface. Keeping the split
means porting the bridge to another platform is a new adapter, not a rewrite.

**Nothing here renders.** The core returns outcomes and facts — an `Ack.kind`,
a `SettingsStore.summary()` dict, a `thread_title` trimmed to whatever length
the caller asked for. Emoji, `**bold**`, backticks and Discord's character caps
all live in the adapter, which is the only file that knows what a message looks
like. `tests/test_core.py::TestCoreCarriesNoPresentation` is the tripwire: the
drift it catches is the easy kind, where someone adds one formatted string to a
core module because that is where the data already is.
"""

from .acks import Ack, ambiguous, delivered, undeliverable
from .conversations import Conversation, ConversationStore
from .mail import conversation_key, correlation_candidates, parse_mail
from .mailbox import MaildirWatcher
from .mgshim import (
    MgCapabilities,
    build_send_args,
    is_unknown_flag_error,
    parse_sent_message_id,
)
from .settings import SettingsStore

__all__ = [
    'Ack',
    'Conversation',
    'ConversationStore',
    'MaildirWatcher',
    'MgCapabilities',
    'SettingsStore',
    'ambiguous',
    'build_send_args',
    'conversation_key',
    'correlation_candidates',
    'delivered',
    'is_unknown_flag_error',
    'parse_mail',
    'parse_sent_message_id',
    'undeliverable',
]
