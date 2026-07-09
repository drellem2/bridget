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


def _is_field_name(name: str) -> bool:
    """RFC 5322 `ftext`: one or more printable ASCII characters, no colon.

    This is what separates `Subject:` (a header with an empty value) from a
    body line that happens to contain a colon. A space disqualifies it, which
    is why `hello: world, how are you` reads as body and `In-Reply-To:` does
    not.
    """
    return bool(name) and all('!' <= c <= '~' and c != ':' for c in name)


def _split_headers(content: str) -> tuple[dict, str]:
    """Split a raw maildir message into (headers, body).

    Header keys are lower-cased. RFC-5322 folded continuation lines (a line
    beginning with space or tab) are appended to the preceding header, which
    matters for `References` — it can carry up to 20 ids and may be wrapped.
    The header block ends at the first empty line, as macguffin writes it.

    The separator is the first colon, not the first colon-space. RFC 5322 makes
    that space optional, so `Subject:` with an empty value is a legal header —
    and keying on `': '` made it a body line, silently swallowing every header
    after it. A mail whose `Subject` was empty would lose its `In-Reply-To` and
    root a fresh thread. Today's `mg` never emits an empty-value header
    (`--subject` is required non-empty), so this was latent rather than live;
    it is not a property we want to depend on.
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
        name, sep, value = line.partition(':')
        if sep and _is_field_name(name):
            last_key = name.lower()
            headers[last_key] = value.strip()
        else:
            # A non-folded line with no "field-name:" shape. macguffin never
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


def correlation_candidates(mail: dict, fallback: str = '') -> list[str]:
    """Every id that could tie `mail` to a conversation already on record.

    Ordered nearest-ancestor first, because the nearest ancestor is the one a
    live conversation is most likely to know:

        In-Reply-To    — the parent
        References     — the ancestry chain, reversed to walk parent-ward
        Message-Id     — this message itself; a hit means we have already seen
                         it (a redelivery after a failed DM), so it must land
                         back in the conversation it already belongs to
        fallback       — the maildir filename, which is the message id for mail
                         written before macguffin stamped Message-Id

    Callers probe these against `ConversationStore.resolve` *before* minting a
    new key with `conversation_key`. This is what makes threading survive past
    the first round-trip: `mg mail send --in-reply-to X` is a stateless
    primitive that seeds `References: [X]` and nothing else, so from the second
    hop onward the chain no longer carries its own root and `References[0]` is
    merely the parent. Only the store knows which conversation that parent sits
    in.
    """
    ids = []
    if mail.get('in_reply_to'):
        ids.append(mail['in_reply_to'])
    ids.extend(reversed(mail.get('references') or []))
    if mail.get('message_id'):
        ids.append(mail['message_id'])
    if fallback:
        ids.append(fallback)

    seen: set[str] = set()
    return [i for i in ids if i and not (i in seen or seen.add(i))]


def conversation_key(mail: dict, fallback: str = '') -> str:
    """Mint the key for a conversation this message is the first we've seen of.

    This is the *fallback* path. It runs only when no id in
    `correlation_candidates` matched a conversation already in the store —
    otherwise the message joins that conversation instead.

    The key is the oldest ancestor this message can name, so that two messages
    that both root a conversation the store has forgotten still land together:

        References[0]  — the oldest ancestor still in the chain
        In-Reply-To    — the parent, when this is a direct reply with no chain
        Message-Id     — this message is itself a root
        fallback       — pre-gh#66 mail with no headers at all; callers pass
                         the maildir filename, which is what macguffin uses as
                         the message id anyway

    `References[0]` is *not* reliably the true root of the chain — macguffin
    caps `References` at the last 20 ids, and `mg mail send --in-reply-to`
    seeds it with the parent alone. Neither costs us a thread any more: the
    store's message-id index resolves those messages onto their conversation
    before this function is consulted.
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
    while subject[:3].lower() == 're:':
        subject = subject[3:].lstrip()
    if not subject or subject == '?':
        sender = mail.get('from') or 'unknown'
        subject = f'mail from {sender}'
    if len(subject) > limit:
        subject = subject[: limit - 1].rstrip() + '…'
    return subject
