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

        self.seen_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.seen_file.parent / (self.seen_file.name + '.tmp')
        tmp.write_text('\n'.join(sorted(self.seen)))
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
        present = self._filenames()
        self.seen |= present
        self.save_seen(present=present)
        return len(self.seen)

    def poll(self) -> list[tuple[str, dict]]:
        """Return `(filename, mail)` for each message not yet seen, oldest first.

        Marks each returned message seen *before* returning it, and persists the
        set. A message that fails to parse is marked seen and skipped rather
        than retried forever.

        Note the ordering contract with the caller: because we mark seen here, a
        delivery that fails after `poll()` returns is not retried unless the
        caller says so. Callers that want at-least-once delivery — every caller
        in this repo does — must call `unsee()` when a send fails.
        """
        present = self._filenames()
        fresh = sorted(present - self.seen)
        out: list[tuple[str, dict]] = []
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
                continue
            except Exception as e:
                print(f'maildir parse error ({name}): {e}', file=sys.stderr)
                self.seen.add(name)
                continue
            self.seen.add(name)
            out.append((name, mail))
        if fresh:
            self.save_seen(present=present)
        return out

    def unsee(self, filename: str) -> None:
        """Forget a filename so the next `poll()` returns it again.

        For callers that want at-least-once delivery: call this when the send
        failed for a retryable reason.
        """
        if filename in self.seen:
            self.seen.discard(filename)
            self.save_seen()
