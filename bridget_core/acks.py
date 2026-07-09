# bridget_core.acks — delivery outcome model. GPL-3.0-or-later.
"""The three things that can happen when the human replies, made explicit.

Silence after typing a reply is the worst outcome a bridge can produce: the
human cannot tell "sent" from "dropped on the floor". Every inbound reply
resolves to exactly one `Ack`, and the adapter always renders it.

    delivered      — the mail went out, and we know to whom.
    ambiguous      — we could not tell which conversation the reply belongs to.
                     The human is shown the candidates and asked to pick.
    undeliverable  — there is nowhere to send it, or `mg` refused. The reason
                     is surfaced verbatim rather than swallowed into a log.

`kind` is the machine-readable outcome; `text` is what the human reads. The
split keeps tests asserting on `kind` rather than on emoji.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DELIVERED = 'delivered'
AMBIGUOUS = 'ambiguous'
UNDELIVERABLE = 'undeliverable'


@dataclass
class Ack:
    kind: str
    text: str
    #: For AMBIGUOUS: the conversations the reply might have belonged to.
    candidates: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind == DELIVERED

    def __str__(self) -> str:
        return self.text


def delivered(agent: str, subject: str = '', *, in_reply_to: str = '') -> Ack:
    """The reply was mailed to `agent`."""
    text = f'✅ delivered to `{agent}`'
    if subject:
        trimmed = subject if len(subject) <= 60 else subject[:59] + '…'
        text += f' — "{trimmed}"'
    if in_reply_to:
        # Surfacing the threading tells the human their reply will land *in*
        # the conversation, not as a fresh top-level mail.
        text += '\n↳ threaded as a reply'
    return Ack(DELIVERED, text)


def ambiguous(candidates: list, *, hint: str = '') -> Ack:
    """The reply could belong to more than one conversation (or to none we can
    name). `candidates` are (label, key) pairs the human can choose between."""
    n = len(candidates)
    if n == 0:
        text = (
            "⚠️ I can't tell which conversation this replies to.\n"
            'Reply inside a conversation thread, or use `mail <subject>` to '
            'start a new one.'
        )
    else:
        text = f'⚠️ ambiguous — this could reply to **{n}** conversations:\n'
        text += '\n'.join(f'• {label}' for label, _ in candidates[:5])
        if n > 5:
            text += f'\n… and {n - 5} more'
        text += '\nReply inside the thread you mean.'
    if hint:
        text += f'\n{hint}'
    return Ack(AMBIGUOUS, text, candidates=list(candidates))


def undeliverable(reason: str, *, agent: str = '') -> Ack:
    """There was nowhere to send the reply, or the send failed."""
    where = f' to `{agent}`' if agent else ''
    detail = reason.strip()
    if len(detail) > 300:
        detail = detail[:299] + '…'
    return Ack(UNDELIVERABLE, f'❌ undeliverable{where}: {detail or "unknown error"}')
