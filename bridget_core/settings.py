# bridget_core.settings — live-tunable bridge settings. GPL-3.0-or-later.
"""Bridge settings the operator can change at runtime, persisted to disk.

"Live" means a `mute` typed into chat takes effect on the next poll of every
watcher, without a restart. The watchers and the command handler share one
process, so an in-memory object would nearly suffice — but the file is also the
supported way to configure the bridge before it starts, and an operator may
edit it by hand. `reload_if_changed()` (an mtime check, cheap enough to call
every poll) makes both paths work.

The DM policy is the calm-inbox knob:

    all      — every mail arrives as a DM. The pre-threading behavior, and the
               default, so an existing install sees no change.
    curated  — only mail that wants a decision reaches the DM. Everything else
               lands in the log channel, where it threads. This is the calm
               inbox: the DM becomes a to-do list, not a firehose.
    none     — nothing DMs; the log channel is the only surface.

`curated` and `none` are only meaningful once a log channel is configured;
otherwise they would silently drop mail, so the bridge refuses that combination
at startup rather than swallowing the operator's notifications.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

SCHEMA_VERSION = 1

DM_POLICIES = ('all', 'curated', 'none')
DEFAULT_DM_POLICY = 'all'


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


class SettingsStore:
    """Persisted, hot-reloadable bridge settings."""

    def __init__(self, path: Path, default_dm_policy: str = DEFAULT_DM_POLICY,
                 clock=_utcnow):
        self.path = Path(path)
        self._clock = clock
        self._default_dm_policy = (
            default_dm_policy if default_dm_policy in DM_POLICIES else DEFAULT_DM_POLICY
        )
        self._mtime: float | None = None
        self.dm_policy: str = self._default_dm_policy
        self.mute_all: bool = False
        self.muted: set[str] = set()
        self.updated_at: str = ''
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Read settings from disk; a missing or malformed file yields defaults."""
        self.dm_policy = self._default_dm_policy
        self.mute_all = False
        self.muted = set()
        self.updated_at = ''
        self._mtime = None

        if not self.path.exists():
            return
        try:
            self._mtime = self.path.stat().st_mtime
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict):
                raise ValueError('not a JSON object')
        except Exception as e:
            print(f'settings parse error ({self.path}): {e}', file=sys.stderr)
            return

        policy = d.get('dm_policy')
        if policy in DM_POLICIES:
            self.dm_policy = policy
        elif policy is not None:
            print(f'settings: ignoring unknown dm_policy {policy!r}', file=sys.stderr)

        self.mute_all = bool(d.get('mute_all', False))
        muted = d.get('muted', [])
        self.muted = set(muted) if isinstance(muted, list) else set()
        self.updated_at = d.get('updated_at', '')

    def reload_if_changed(self) -> bool:
        """Re-read the file if it changed on disk. Returns True if it did.

        Called from the watcher poll loops, so an operator's `mute` (or a hand
        edit) takes effect within one poll interval.
        """
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        self.load()
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = self._clock()
        payload = {
            'version': SCHEMA_VERSION,
            'dm_policy': self.dm_policy,
            'mute_all': self.mute_all,
            'muted': sorted(self.muted),
            'updated_at': self.updated_at,
        }
        tmp = self.path.parent / (self.path.name + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
        os.replace(tmp, self.path)
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = None

    # -- queries ----------------------------------------------------------

    def is_muted(self, conversation_key: str = '') -> bool:
        """True if a notification for this conversation should be suppressed.

        Muting suppresses the *DM*, not the log-channel post. A muted
        conversation keeps threading; the human just stops being pinged.
        """
        if self.mute_all:
            return True
        return bool(conversation_key) and conversation_key in self.muted

    # -- mutations --------------------------------------------------------

    def set_dm_policy(self, policy: str) -> bool:
        if policy not in DM_POLICIES:
            return False
        self.dm_policy = policy
        self.save()
        return True

    def mute(self, conversation_key: str) -> bool:
        """Mute one conversation. Returns False if it was already muted."""
        if conversation_key in self.muted:
            return False
        self.muted.add(conversation_key)
        self.save()
        return True

    def unmute(self, conversation_key: str) -> bool:
        if conversation_key not in self.muted:
            return False
        self.muted.discard(conversation_key)
        self.save()
        return True

    def set_mute_all(self, on: bool) -> bool:
        """Mute or unmute every DM. Returns False if already in that state."""
        if self.mute_all == on:
            return False
        self.mute_all = on
        self.save()
        return True

    def describe(self, conversation_subjects: dict | None = None) -> str:
        """A human-readable settings dump for the `settings` command."""
        lines = [
            '⚙️ **bridget settings**',
            f'• DM policy: `{self.dm_policy}`',
            f'• Mute all DMs: `{str(self.mute_all).lower()}`',
        ]
        if self.muted:
            lines.append(f'• Muted conversations: **{len(self.muted)}**')
            subjects = conversation_subjects or {}
            for key in sorted(self.muted)[:10]:
                label = subjects.get(key) or key
                lines.append(f'    – {label}')
            if len(self.muted) > 10:
                lines.append(f'    … and {len(self.muted) - 10} more')
        else:
            lines.append('• Muted conversations: none')
        if self.updated_at:
            lines.append(f'• Updated: {self.updated_at}')
        return '\n'.join(lines)
