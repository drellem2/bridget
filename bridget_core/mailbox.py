# bridget_core.mailbox — observe-only maildir watching. GPL-3.0-or-later.
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
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .mail import parse_mail


class MaildirWatcher:
    """Observe-only scanner over one maildir `new/` directory.

    Usage:

        w = MaildirWatcher(mail_dir, seen_file)
        if not w.primed:
            w.prime()          # first run: adopt the backlog, don't replay it
        for filename, mail in w.poll():
            deliver(mail)
    """

    def __init__(self, mail_dir: Path, seen_file: Path, *, max_seen: int = 5000):
        self.mail_dir = Path(mail_dir)
        self.seen_file = Path(seen_file)
        self.max_seen = max_seen
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

    def save_seen(self) -> None:
        """Persist the seen-set atomically.

        Bounded by `max_seen`: maildir filenames are monotonically increasing
        (macguffin stamps them with nanosecond timestamps), so keeping the
        lexicographically largest names keeps the newest. Dropping an old name
        cannot cause a re-delivery, because the mail it refers to is older than
        everything retained.
        """
        seen = self.seen
        if len(seen) > self.max_seen:
            seen = set(sorted(seen)[-self.max_seen:])
            self.seen = seen
        self.seen_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.seen_file.parent / (self.seen_file.name + '.tmp')
        tmp.write_text('\n'.join(sorted(seen)))
        os.replace(tmp, self.seen_file)

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
        self.seen |= self._filenames()
        self.save_seen()
        return len(self.seen)

    def poll(self) -> list[tuple[str, dict]]:
        """Return `(filename, mail)` for each message not yet seen, oldest first.

        Marks each returned message seen *before* returning it, and persists the
        set. A message that fails to parse is marked seen and skipped rather
        than retried forever.

        Note the ordering contract with the caller: because we mark seen here, a
        delivery that fails after `poll()` returns is not retried. That is the
        deliberate trade — at-most-once delivery, never a hot loop re-sending a
        message chat keeps rejecting. Callers that want at-least-once should
        call `unsee()` on failure.
        """
        fresh = sorted(self._filenames() - self.seen)
        out: list[tuple[str, dict]] = []
        for name in fresh:
            path = self.mail_dir / name
            try:
                mail = parse_mail(path.read_text())
            except OSError as e:
                # The file vanished between listing and reading (macguffin moved
                # it to cur/ under us). Nothing to deliver; don't mark it seen —
                # it is gone from new/ and will not be listed again.
                print(f'maildir read error ({name}): {e}', file=sys.stderr)
                continue
            except Exception as e:
                print(f'maildir parse error ({name}): {e}', file=sys.stderr)
                self.seen.add(name)
                continue
            self.seen.add(name)
            out.append((name, mail))
        if out or fresh:
            self.save_seen()
        return out

    def unsee(self, filename: str) -> None:
        """Forget a filename so the next `poll()` returns it again.

        For callers that want at-least-once delivery: call this when the send
        failed for a retryable reason.
        """
        if filename in self.seen:
            self.seen.discard(filename)
            self.save_seen()
