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

# bridget_core.mailbox — observe-only maildir watching.
"""Watch a macguffin maildir without disturbing it.

**Observe-only is a hard invariant.** The bridge reads `<mailbox>/new/` and
never moves, renames, or deletes anything in it. macguffin owns that directory:
`mg mail read` is what promotes a message from `new/` to `cur/`, and if the
bridge did it as a side effect of *displaying* a message, every mail the human
glanced at in chat would silently vanish from their real inbox. The only calls
this module makes against the maildir are `iterdir()` and `read_text()`.

De-duplication therefore cannot rely on the directory: a message stays in
`new/` after we deliver it. Instead each watcher keeps a seen-set of maildir
filenames, persisted so a restart does not re-deliver the entire backlog.

The seen-set is written *after* the caller confirms delivery, never before —
see `MaildirWatcher.poll`. Delivery is at-least-once by construction, and the
adapter is responsible for making a redelivery idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .mail import parse_mail
from .statefile import write_state


class MaildirWatcher:
    """Observe-only scanner over one maildir `new/` directory.

    Usage:

        w = MaildirWatcher(mail_dir, seen_file)
        if not w.primed:
            w.prime()          # first run: adopt the backlog, don't replay it
        for filename, mail in w.poll():
            if deliver(mail):
                w.commit(filename)   # only now is it safe to forget
            else:
                w.unsee(filename)
    """

    def __init__(self, mail_dir: Path, seen_file: Path, *, gc_threshold: int = 5000):
        self.mail_dir = Path(mail_dir)
        self.seen_file = Path(seen_file)
        self.gc_threshold = gc_threshold
        self.seen: set[str] = self._load_seen()

    # -- seen-set persistence ---------------------------------------------

    @property
    def primed(self) -> bool:
        """False on a genuinely fresh install — no seen file has ever been
        written. Distinct from "seen file exists but is empty", which means the
        mailbox was empty when we primed and every later mail is new."""
        return self.seen_file.exists()

    def _load_seen(self) -> set[str]:
        if not self.seen_file.exists():
            return set()
        try:
            return {ln for ln in self.seen_file.read_text().splitlines() if ln}
        except OSError as e:
            print(f'seen-set read error ({self.seen_file}): {e}', file=sys.stderr)
            return set()

    def save_seen(self, present: set[str] | None = None) -> None:
        """Persist the seen-set atomically, garbage-collecting dead entries.

        The seen-set may only forget a filename once that file has left `new/`
        (i.e. `mg mail read` moved it to `cur/`). It can never be trimmed by
        age: because we are observe-only, a delivered message *stays* in `new/`
        forever until the human reads it, so dropping its name re-surfaces it as
        new on the very next poll — and again every poll after that.

        So the collection is by presence, not recency: `seen &= present`. That
        bounds the set to the size of `new/`, which is the smallest it can
        safely be. If `new/` itself holds more than `gc_threshold` unread
        messages the set stays large, and correctly so.
        """
        if len(self.seen) > self.gc_threshold:
            if present is None:
                present = self._filenames()
            # Intersect, never truncate. A name whose file is still in new/ must
            # be remembered or its mail gets delivered twice.
            self.seen &= present

        write_state(self.seen_file, '\n'.join(sorted(self.seen)))

    # -- scanning ----------------------------------------------------------

    def _filenames(self) -> set[str]:
        if not self.mail_dir.exists():
            return set()
        try:
            return {p.name for p in self.mail_dir.iterdir() if not p.name.startswith('.')}
        except OSError as e:
            print(f'maildir scan error ({self.mail_dir}): {e}', file=sys.stderr)
            return set()

    def prime(self) -> int:
        """Adopt everything currently in `new/` as already-seen.

        Called once on a fresh install so the bridge does not open a thread for
        every mail the human accumulated before installing it. Returns the count
        adopted.
        """
        present = self._filenames()
        self.seen |= present
        self.save_seen(present=present)
        return len(self.seen)

    def poll(self) -> list[tuple[str, dict]]:
        """Return `(filename, mail)` for each message not yet seen, oldest first.

        **A returned message is not yet seen.** `poll` hands the caller a
        message and makes no record of having done so; the caller marks it seen
        by calling `commit()` once delivery has actually succeeded.

        That ordering is the whole at-least-once guarantee. Marking a message
        seen before the caller delivers it opens a window — between the seen-set
        hitting the disk and the mail hitting the chat surface — in which a hard
        crash (SIGKILL, OOM, power loss) loses the mail forever: it is seen on
        restart, so it is never re-read, and it is still sitting unread in
        `new/`, so nothing else will ever surface it. `unsee()` cannot close that
        window, because a killed process does not get to run `unsee()`. On a tool
        whose invariant is "never drop mail on the floor", a window that small is
        still a window.

        Crashing *after* delivery but before `commit()` redelivers the message
        instead. That is the trade this method makes deliberately: a duplicate is
        recoverable by a human reading it twice, a drop is not. The adapter takes
        the other half of the deal — `ConversationStore.was_posted` keeps a
        redelivery from posting into the conversation thread twice.

        A message that cannot be parsed or read *is* marked seen here, and the
        seen-set persisted: it is never going to be delivered, so nothing will
        ever commit it, and without this it would be re-read and re-logged on
        every poll forever.

        Callers must not poll again with a batch still in flight — the in-flight
        messages are not yet in the seen-set and would be handed out twice.
        """
        present = self._filenames()
        fresh = sorted(present - self.seen)
        out: list[tuple[str, dict]] = []
        undeliverable = False
        for name in fresh:
            path = self.mail_dir / name
            try:
                mail = parse_mail(path.read_text())
            except FileNotFoundError:
                # The file vanished between listing and reading (macguffin moved
                # it to cur/ under us). Nothing to deliver; don't mark it seen —
                # it is gone from new/ and will not be listed again.
                continue
            except OSError as e:
                # A real IO problem — EACCES, EIO — and the file is still there.
                # Marking it seen is the only way to avoid re-reading, and
                # re-logging, the same broken file on every single poll forever.
                print(f'maildir read error ({name}), skipping: {e}', file=sys.stderr)
                self.seen.add(name)
                undeliverable = True
                continue
            except Exception as e:
                print(f'maildir parse error ({name}): {e}', file=sys.stderr)
                self.seen.add(name)
                undeliverable = True
                continue
            out.append((name, mail))
        if undeliverable:
            self.save_seen(present=present)
        return out

    def commit(self, filename: str) -> None:
        """Mark a delivered message seen, and persist that.

        Call this only once the mail has reached the human. Everything before
        this point is replayable; nothing after it is.
        """
        if filename not in self.seen:
            self.seen.add(filename)
            self.save_seen()

    def unsee(self, filename: str) -> None:
        """Forget a filename so the next `poll()` returns it again.

        Since `poll()` no longer marks a message seen, this is only load-bearing
        for a message already committed — a delivery the caller has decided to
        retry after the fact. For the ordinary "the send failed, try next poll"
        path it is a no-op, and correct as one: the message was never seen.
        """
        if filename in self.seen:
            self.seen.discard(filename)
            self.save_seen()
