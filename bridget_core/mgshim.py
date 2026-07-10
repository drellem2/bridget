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

# bridget_core.mgshim — mg CLI capability seam.
"""The correlation-ID seam: use `mg mail send --in-reply-to` when mg has it.

macguffin gh#66 (`Message-Id` on every message, `--in-reply-to`, `References`)
is the feature that lets a reply thread instead of landing as an unrelated
top-level mail. It is not reliably present: it ships in some builds of `mg` and
not others, and the binary on a developer's PATH can be swapped underneath a
running bridge by an unrelated `go install`.

So the bridge treats it as a *capability*, not a dependency:

    auto (default) — probe `mg mail send --help` once; use the flag if listed.
    on             — assume it exists; useful if the probe is wrong.
    off            — never pass it; every reply is a fresh top-level mail.

Degradation is total and silent-to-the-user: without correlation IDs, replies
still deliver, they just don't thread. The bridge never fails a send because mg
was too old. And because a probe result can go stale — mg really does get
rebuilt mid-session — a send rejected with `unknown flag: --in-reply-to`
downgrades the capability and retries once, rather than reporting a spurious
undeliverable.

This module also owns how a human's message is split into a mail, and with it
one rule that is not negotiable:

    Truncate the label. Never the payload.

A slice is only safe on text that has a complete copy somewhere else. `mg` caps
neither subject nor body, so any cap here is bridget's own choice, and a cap on
the *only* copy of what a person said is a silent rewrite of their instruction.
See `compose_subject_body`.
"""
from __future__ import annotations

import json
import re
import sys

FLAG_IN_REPLY_TO = '--in-reply-to'
FLAG_JSON = '--json'

MODES = ('auto', 'on', 'off')
DEFAULT_MODE = 'auto'

#: Ceiling on a `--subject`. mg itself imposes none — a 500-character subject
#: round-trips through the maildir intact — but a mail header is a one-line
#: label, not a payload, and an unbounded one makes every inbox listing
#: unreadable. Bounding it means *dropping bytes*, so the rule that pays for
#: this constant is: whatever a caller puts in the subject must also be in the
#: body. `compose_subject_body` is how callers satisfy it.
MG_SUBJECT_LIMIT = 200

#: `Delivered: human → mayor/new/1783613368062470000.88512.0`, the human-readable
#: form of what `--json` reports as `msg_id`. Parsed only as a backstop.
_DELIVERED_RE = re.compile(r'/new/(\S+)')


def help_advertises_in_reply_to(help_text: str) -> bool:
    """True if `mg mail send --help` lists the flag.

    Matches the flag only where cobra prints it in the Flags block, i.e. as a
    token, so a mention in the prose description alone does not count. (An mg
    build has shipped whose long description documented the flag its flag-set
    did not actually define — the prose is not the contract.)
    """
    if not help_text:
        return False
    in_flags = False
    for line in help_text.splitlines():
        if line.startswith('Flags:') or line.startswith('Global Flags:'):
            in_flags = True
            continue
        if not in_flags:
            continue
        if line and not line[:1].isspace():
            in_flags = False  # left the Flags block
            continue
        # cobra prints either `      --flag string` or, when a shorthand exists,
        # `  -r, --flag string`. Check both leading tokens, stripping the comma.
        tokens = [t.rstrip(',') for t in line.strip().split()[:2]]
        if FLAG_IN_REPLY_TO in tokens:
            return True
    return False


def is_unknown_flag_error(text: str, flag: str = FLAG_IN_REPLY_TO) -> bool:
    """True if mg rejected `flag` as unknown — the signal that our cached
    capability is stale because the binary changed under us."""
    low = (text or '').lower()
    return 'unknown flag' in low and flag.lstrip('-').lower() in low


class MgCapabilities:
    """Lazily-probed, downgradable view of what the local `mg` supports.

    `run_help` is a zero-arg callable returning `mg mail send --help` output.
    Injected rather than called directly so the core stays free of subprocess
    and the probe is trivially testable.
    """

    def __init__(self, run_help, mode: str = DEFAULT_MODE):
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self._run_help = run_help
        self._probed: bool | None = None

    @property
    def correlation_ids(self) -> bool:
        if self.mode == 'on':
            return True
        if self.mode == 'off':
            return False
        if self._probed is None:
            try:
                self._probed = help_advertises_in_reply_to(self._run_help())
            except Exception as e:
                print(f'mg capability probe failed, assuming no correlation IDs: {e}',
                      file=sys.stderr)
                self._probed = False
        return self._probed

    def downgrade(self) -> None:
        """Record that mg rejected the flag. Sticks for the process lifetime
        unless mode is 'on', which the operator asked us not to second-guess."""
        if self.mode != 'on':
            self._probed = False

    def describe(self) -> str:
        state = 'on' if self.correlation_ids else 'off'
        how = 'forced' if self.mode in ('on', 'off') else 'detected'
        return f'{state} ({how})'


def _truncation_marker(dropped: int) -> str:
    return f'… [truncated {dropped} chars; full text in body]'


def subject_label(text: str, limit: int = MG_SUBJECT_LIMIT) -> str:
    """A one-line, bounded label for `text`, safe to sit in a mail header.

    Neutralizes control characters — each becomes a single space — because
    `Subject:` is a single line and mg rejects a header carrying a newline or
    any other control character outright. This is deliberately narrow: ordinary
    spacing is *preserved*, not collapsed, because mg accepts it and a human who
    typed two spaces in a subject they composed should get two spaces. Only the
    bytes mg would refuse are touched, and only enough to make them legal.

    When the label doesn't fit it is elided with an explicit marker naming how
    much went and where the rest is, never a bare slice. A bare `[:200]` leaves
    a grammatical sentence that merely happens to stop early, so the reader
    gets no signal at all — and truncation of an instruction is biased toward
    the dangerous reading. English puts the imperative first and the guard
    clause last ("delete it *unless* …", "go ahead, *but* not production"), so
    clipping the tail strips the condition and keeps the command. The marker is
    the signal that a condition may have been stripped.

    This function is lossy by design. Never call it on text that isn't also
    going out in the body — see `compose_subject_body`.
    """
    label = ''.join(
        ' ' if ord(c) < 0x20 or ord(c) == 0x7f else c for c in text
    ).strip()
    if len(label) <= limit:
        return label

    # The marker's length depends on the number it prints, which depends on how
    # much room the marker leaves. Two passes reach the fixed point: `dropped`
    # only shrinks as `keep` grows, and its digit count is what moves.
    keep = limit
    for _ in range(2):
        keep = limit - len(_truncation_marker(len(label) - keep))
    if keep < 1:
        # No room to explain ourselves. Don't imply the marker's promise.
        return label[:limit - 1].rstrip() + '…'

    head = label[:keep].rstrip()
    return head + _truncation_marker(len(label) - len(head))


def compose_subject_body(text: str) -> tuple[str, str]:
    """Split one human-typed message into a mail subject and body, losing nothing.

    The contract, and the whole point of this function: **no content of `text`
    is dropped — it survives into the subject or the body.** The subject may be
    elided; when it is, the body carries the message whole. (A subject cannot
    hold a control character — mg rejects the header — so any in the subject
    become spaces. That is the only liberty taken with it; ordinary spacing is
    left exactly as typed.)

    bridget used to hand the entire message to `--subject` whenever it had no
    newline in it, and leave the body as the string '(no body)'. Subjects are
    bounded and bodies are not, so a long instruction lost its tail on the way
    to the agent — no error, no marker, `mg` exiting 0. That is mg-7e0c: a
    reply authorizing a repo deletion arrived as "…you can delete and recreate
    if", stopping mid-clause, and read as a complete sentence. A truncated
    authorization can invert its own meaning; the channel must not truncate.

    A newline means the human composed a subject line deliberately, so honour
    the split — but only while that line fits, because a first line we would
    have to elide is a sentence, not a subject.
    """
    text = text.strip()
    if not text:
        return '', ''
    head, sep, rest = text.partition('\n')
    head = head.strip()
    if sep and head and len(head) <= MG_SUBJECT_LIMIT:
        return head, rest.rstrip()
    return subject_label(text), text


def build_send_args(agent: str, subject: str, body: str, *,
                    sender: str = 'human', in_reply_to: str = '',
                    want_msg_id: bool = False) -> list[str]:
    """The argv for one `mg mail send`.

    The subject is passed through `subject_label`: bounded, one line, visibly
    elided. The body is passed through *whole* — never truncate it. Callers
    that take a subject from human text must get it from
    `compose_subject_body`, which guarantees the body still holds every byte.

    `in_reply_to` is appended only when the caller has decided mg supports it.
    `want_msg_id` asks for `--json`, whose `msg_id` is the id of the message
    just delivered — the id the recipient's reply will name, and so the id the
    conversation store must remember to keep the thread alive past this hop.

    Requesting `--json` is safe wherever `--in-reply-to` is safe: `mg mail
    --json` (macguffin 08cfa39) predates the correlation-ID work (e306af3), so
    any build advertising the latter emits the former. Callers still tolerate a
    missing id rather than assuming one.
    """
    args = [
        'mail', 'send', agent,
        f'--from={sender}',
        f'--subject={subject_label(subject)}',
        f'--body={body or "(no body)"}',
    ]
    if in_reply_to:
        args.append(f'{FLAG_IN_REPLY_TO}={in_reply_to}')
    if want_msg_id:
        args.append(FLAG_JSON)
    return args


def parse_sent_message_id(output: str) -> str:
    """The id mg assigned the message it just sent, or '' if it didn't say.

    Reads `--json`'s `msg_id`, falling back to the maildir path in the
    human-readable `Delivered:` line — whose basename *is* the message id, as
    macguffin names each file after the id it stamps into `Message-Id`.

    An unparseable output is not an error. It costs the conversation one hop of
    threading, which is exactly the pre-existing behaviour; a raised exception
    would instead tell the human their delivered reply was undeliverable.
    """
    text = (output or '').strip()
    if not text:
        return ''
    try:
        payload = json.loads(text)
    except ValueError:
        pass
    else:
        if isinstance(payload, dict):
            return str(payload.get('msg_id') or '')
        return ''
    match = _DELIVERED_RE.search(text)
    return match.group(1) if match else ''
