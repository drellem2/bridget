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

"""One way to write a state file: atomically, and readable only by its owner.

Every file the bridge persists sits in `~/.pogo/` next to the env file that
holds the bot token. None of them hold the token, but they do hold mail
subjects, agent names, and which conversations the human muted — and under the
default umask of 022 a plain `write_text` lands them at 0644, readable by every
account on the host.

The mode is set on the temp file *before* it holds anything, so the content is
never momentarily world-readable. `os.replace` then carries the mode across with
the rename, which is also what makes the write atomic: a crash mid-write leaves
either the old file or the new one, never a truncated one.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Owner read/write. These files describe who the human talks to.
STATE_FILE_MODE = 0o600

#: Owner-only traversal, for a directory we create ourselves.
STATE_DIR_MODE = 0o700


def secure_parent(path: Path) -> None:
    """Ensure `path`'s directory exists, creating it owner-only.

    An existing directory is left exactly as it is: `~/.pogo` is pogo's, not
    bridget's, and silently re-permissioning a directory we did not create is
    not ours to do. `install.sh` tightens it at install time, where the user can
    see it happen.
    """
    parent = Path(path).parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True, mode=STATE_DIR_MODE)


def write_state(path: Path, text: str) -> None:
    """Atomically write `text` to `path` with mode 0600."""
    path = Path(path)
    secure_parent(path)
    tmp = path.parent / (path.name + '.tmp')
    # Create the temp file owner-only before writing: opening with the mode is
    # the only way to avoid a window where the content exists at 0644. The
    # explicit umask dance would be racy across threads.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, STATE_FILE_MODE)
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # O_CREAT honours the mode only when the file is new; an inherited temp file
    # from an older, laxer version of this code would keep its old mode.
    os.chmod(tmp, STATE_FILE_MODE)
    os.replace(tmp, path)
