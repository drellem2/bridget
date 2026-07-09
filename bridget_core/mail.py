# bridget_core.mail — maildir parsing + correlation IDs. GPL-3.0-or-later.
"""Parse macguffin maildir messages, including the correlation-ID headers.

macguffin stamps every delivered message with a `Message-Id` equal to its
MSG-ID, which is also its maildir filename (macguffin PR #23 / gh#66). Replies
sent via `mg mail send --in-reply-to MSG-ID` or `mg mail reply` additionally
carry `In-Reply-To` and a `References` chain.

Those three headers are what let the bridge group messages into conversations
and root each conversation in its own chat thread. They are *optional*: mail
written before gh#66 landed has none of them, and macguffin deliberately does
not auto-populate In-Reply-To. Every function here degrades cleanly when they
are absent — a message with no correlation headers is simply a conversation of
one, keyed on its own id.
"""
from __future__ import annotations

# Headers whose value is a whitespace-separated list of message ids.
_LIST_HEADERS = ('references',)


def _split_headers(content: str) -> tuple[dict, str]:
    """Split a raw maildir message into (headers, body).

    Header keys are lower-cased. RFC-5322 folded continuation lines (a line
    beginning with space or tab) are appended to the preceding header, which
    matters for `References` — it can carry up to 20 ids and may be wrapped.
    The header block ends at the first empty line, as macguffin writes it.
    """
    lines = content.splitlines()
    headers: dict[str, str] = {}
    body_start = len(lines)
    last_key: str | None = None

    for i, line in enumerate(lines):
        if line == '':
            body_start = i + 1
            break
        if line[:1] in (' ', '\t') and last_key is not None:
            headers[last_key] += ' ' + line.strip()
            continue
        if ': ' in line:
            k, v = line.split(': ', 1)
            last_key = k.lower()
            headers[last_key] = v
        else:
            # A non-folded line with no "key: value" shape. macguffin never
            # writes one; treat it as the start of the body rather than
            # silently swallowing it.
            body_start = i
            break

    return headers, '\n'.join(lines[body_start:])


def parse_mail(content: str) -> dict:
    """Parse a maildir message into the dict the bridge passes around.

    The `from` / `subject` / `body` keys predate correlation IDs and keep their
    historical shape, including the '?' sentinel for a missing sender/subject.
    The correlation keys are added on top:

        message_id   — this message's id, or '' if the header is absent
        in_reply_to  — the id this message replies to, or ''
        references   — the ancestry chain, oldest first; [] if absent
        date         — the Date header verbatim, or ''
    """
    headers, body = _split_headers(content)

    refs = headers.get('references', '')

    return {
        'from': headers.get('from', '?'),
        'subject': headers.get('subject', '?'),
        'body': body,
        'message_id': headers.get('message-id', '').strip(),
        'in_reply_to': headers.get('in-reply-to', '').strip(),
        'references': refs.split() if refs.strip() else [],
        'date': headers.get('date', '').strip(),
    }


def conversation_key(mail: dict, fallback: str = '') -> str:
    """Return the stable id of the conversation this message belongs to.

    The key is the *root* of the reply chain, so every message in a thread
    resolves to the same value regardless of where in the chain it sits:

        References[0]  — the root, when macguffin seeded a chain
        In-Reply-To    — the parent, when this is a direct reply with no chain
        Message-Id     — this message is itself a root
        fallback       — pre-gh#66 mail with no headers at all; callers pass
                         the maildir filename, which is what macguffin uses as
                         the message id anyway

    Caveat: macguffin caps `References` at the last 20 ids. A conversation that
    runs past 20 messages loses its true root, and messages beyond the cap key
    on the oldest id still in the chain — they start a second thread rather
    than joining the first. That is a bounded, visible degradation (a fresh
    thread appears) and not a correctness bug in the map.
    """
    refs = mail.get('references') or []
    if refs:
        return refs[0]
    if mail.get('in_reply_to'):
        return mail['in_reply_to']
    if mail.get('message_id'):
        return mail['message_id']
    return fallback


def reply_target(mail: dict, fallback: str = '') -> str:
    """Return the id a reply to `mail` should set as In-Reply-To.

    That is the message's own id — you reply *to* this message, not to its
    root. Falls back to the maildir filename for headerless mail, which is the
    same value macguffin would have used as the Message-Id.
    """
    return mail.get('message_id') or fallback


def thread_title(mail: dict, limit: int = 90) -> str:
    """A human-readable thread name for the conversation this mail roots.

    Strips a leading "Re: " so a thread opened from a reply reads the same as
    one opened from the original, and trims to `limit` characters — Discord
    caps thread names at 100.
    """
    subject = (mail.get('subject') or '').strip()
    while subject[:4].lower() == 're: ':
        subject = subject[4:].lstrip()
    if not subject or subject == '?':
        sender = mail.get('from') or 'unknown'
        subject = f'mail from {sender}'
    if len(subject) > limit:
        subject = subject[: limit - 1].rstrip() + '…'
    return subject
