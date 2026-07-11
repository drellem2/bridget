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

"""The three things that can happen when the human replies, made explicit.

Silence after typing a reply is the worst outcome a bridge can produce: the
human cannot tell "sent" from "dropped on the floor". Every inbound reply
resolves to exactly one `Ack`, and the adapter always renders it.

    delivered      — the mail went out, and we know to whom.
    ambiguous      — we could not tell which conversation the reply belongs to.
                     The human is shown the candidates and asked to pick.
    undeliverable  — there is nowhere to send it, or `mg` refused. The reason
                     is surfaced verbatim rather than swallowed into a log.

An `Ack` is *data*, not a message: `kind` plus the facts behind it. It carries
no emoji, no `**bold**`, and no character budget, because all three of those
are Discord's opinions and this module is the part of the bridge that would be
identical under Slack. The adapter renders an `Ack` two ways: for a routed
thread reply it becomes a *reaction* on the human's own message — ✅ / ❌ via
`ack_reaction`, so the confirmation doesn't clutter the thread (mg-aefb); for
the ambiguous case (which has to list candidates) it becomes text via
`render_ack`. Both live in the `bridget` script. Tests assert on `kind` and on
the fields, never on how a given surface chooses to draw it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

DELIVERED = 'delivered'
AMBIGUOUS = 'ambiguous'
UNDELIVERABLE = 'undeliverable'


@dataclass
class Ack:
    """The outcome of one inbound reply, in machine-readable form."""

    kind: str
    #: DELIVERED / UNDELIVERABLE: the agent on the other end. May be '' when
    #: there was no agent to name.
    agent: str = ''
    #: DELIVERED: the subject the reply went out under.
    subject: str = ''
    #: DELIVERED: the id this reply threads onto, or '' if it went out
    #: untethered (an mg too old for correlation IDs, or no known parent).
    in_reply_to: str = ''
    #: UNDELIVERABLE: why, verbatim from mg or from us.
    reason: str = ''
    #: AMBIGUOUS: the conversations the reply might have belonged to, as
    #: (label, key) pairs.
    candidates: list = field(default_factory=list)
    #: AMBIGUOUS: an extra line of guidance for the human, if we have one.
    hint: str = ''

    @property
    def ok(self) -> bool:
        return self.kind == DELIVERED


def delivered(agent: str, subject: str = '', *, in_reply_to: str = '') -> Ack:
    """The reply was mailed to `agent`."""
    return Ack(DELIVERED, agent=agent, subject=subject, in_reply_to=in_reply_to)


def ambiguous(candidates: list, *, hint: str = '') -> Ack:
    """The reply could belong to more than one conversation (or to none we can
    name). `candidates` are (label, key) pairs the human can choose between."""
    return Ack(AMBIGUOUS, candidates=list(candidates), hint=hint)


def undeliverable(reason: str, *, agent: str = '') -> Ack:
    """There was nowhere to send the reply, or the send failed."""
    return Ack(UNDELIVERABLE, agent=agent, reason=reason.strip())
