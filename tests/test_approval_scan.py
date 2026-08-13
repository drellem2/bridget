#!/usr/bin/env python3
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

"""The approval scan's own directory (mg-18bf).

`scan_pending_approvals` used to read MAIL_DIR — the box mail is DELIVERED
from. Step 4 of the representative cutover re-points POGO_MAIL_DIR at the
representative's output box, because the DM watcher must move or bridget DMs
Daniel every raw `human` queue mail and bypasses the relay entirely. The scan
was dragged along, and the output box holds REWRITTEN subjects: the
representative's prompt forbids dropping an approval request but requires
rewriting it, and forbids internal identifiers in the subject. A rewritten
subject cannot match `^Subject: approval needed `, so the "Awaiting your
approval" section would have read zero — indistinguishable from a genuinely
empty plate, at exactly the moment Daniel started relying on the relay.

Three things are pinned here:

  * the scan follows the mail ROOT but not the RECIPIENT, so a re-point moves
    the watcher and leaves the scan on `human`;
  * a pre-fix control reproduces the silent zero, so the bug is a fact rather
    than a story; and
  * the remedy is subject to the defect it remedies — a scan pointed at a box
    whose subjects never match is still possible — so its zero now names which
    of the three worlds it is in: no directory, empty directory, or mail read
    and none matched.

Stubs `discord` so this runs under system python3 (no venv-bridget required).
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / 'bridget'


def load_bridget(env_overrides: dict | None = None):
    """Import bridget into a fresh namespace with a clean fake HOME.

    Returns (module, fake_home) — the caller needs the home to seed maildirs
    under the same root bridget derived its paths from.
    """
    fake_home = Path(tempfile.mkdtemp(prefix='bridget-approval-test-'))
    env_dir = fake_home / '.pogo'
    env_dir.mkdir(parents=True)
    (env_dir / 'bridget.env').write_text(
        'DISCORD_BOT_TOKEN=fake\n'
        'DISCORD_USER_ID=1\n'
        'DISCORD_SERVER_ID=2\n'
    )

    keys_we_set = {'HOME', 'BRIDGET_REPO_DIR'}
    if env_overrides:
        keys_we_set.update(env_overrides.keys())
    saved_env = {k: os.environ.get(k) for k in keys_we_set}
    os.environ['HOME'] = str(fake_home)
    os.environ['BRIDGET_REPO_DIR'] = str(REPO)
    for k, v in (env_overrides or {}).items():
        os.environ[k] = v

    fake_discord = mock.MagicMock()
    fake_discord.Intents.default.return_value = mock.MagicMock()
    saved_discord = sys.modules.get('discord')
    sys.modules['discord'] = fake_discord
    saved_bridget = sys.modules.pop('bridget', None)

    try:
        loader = SourceFileLoader('bridget', str(SCRIPT))
        spec = importlib.util.spec_from_loader('bridget', loader)
        bridget = importlib.util.module_from_spec(spec)
        loader.exec_module(bridget)
        return bridget, fake_home
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_discord is not None:
            sys.modules['discord'] = saved_discord
        else:
            sys.modules.pop('discord', None)
        if saved_bridget is not None:
            sys.modules['bridget'] = saved_bridget
        else:
            sys.modules.pop('bridget', None)


def write_mail(d: Path, name: str, subject: str, body: str = 'please decide') -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f'From: architect\n'
        f'To: human\n'
        f'Subject: {subject}\n'
        f'\n'
        f'{body}\n'
    )


#: What an agent writes into `human`.
RAW_SUBJECT = 'approval needed mg-deadbeef: ship the retry loop'
#: What the representative writes into its output box after rewriting — no
#: internal identifier, no `approval needed` prefix. crew/representative.md
#: forbids holding or dropping the request but requires the rewrite, so this
#: pair is the shape of the real thing, not an adversarial edge case.
REWRITTEN_SUBJECT = 'Decision needed: whether to ship the retry loop'


def home_mail(home: Path) -> Path:
    return home / '.macguffin' / 'mail'


class ScanReadsItsOwnBox(unittest.TestCase):
    """The split itself: the scan follows the root, not the recipient."""

    def test_default_scan_dir_is_the_human_box(self):
        b, home = load_bridget()
        self.assertEqual(b.APPROVAL_DIR,
                         home / '.macguffin' / 'mail' / 'human' / 'new')
        # With no representative in front, the two boxes coincide — the split
        # must be a no-op on the default install.
        self.assertEqual(b.APPROVAL_DIR, b.MAIL_DIR)

    def test_recipient_repoint_moves_delivery_but_not_the_scan(self):
        """Step 4 of the cutover, exactly as CUTOVER.md writes it."""
        b, home = load_bridget({'POGO_MAIL_DIR': '~/.macguffin/mail/daniel'})
        self.assertEqual(b.MAIL_DIR, home_mail(home) / 'daniel' / 'new')
        self.assertEqual(b.APPROVAL_DIR, home_mail(home) / 'human' / 'new')
        self.assertNotEqual(b.APPROVAL_DIR, b.MAIL_DIR)

    def test_moving_the_whole_root_carries_the_scan_along(self):
        """A root move is not a re-point, and must not strand the scan.

        This is the case a hard-coded `~/.macguffin/mail/human/new` default
        would have broken — the fix has to follow one kind of change and
        ignore the other.
        """
        b, home = load_bridget({'POGO_MAIL_DIR': '~/mail2/human'})
        self.assertEqual(b.APPROVAL_DIR, home / 'mail2' / 'human' / 'new')
        self.assertEqual(b.APPROVAL_DIR, b.MAIL_DIR)

    def test_mailbox_name_is_overridable(self):
        b, home = load_bridget({'BRIDGET_APPROVAL_MAILBOX': 'queue'})
        self.assertEqual(b.APPROVAL_DIR,
                         home_mail(home) / 'queue' / 'new')

    def test_a_path_in_the_mailbox_name_is_refused_loudly(self):
        """A bare name is the contract; a path would silently escape the root."""
        with self.assertRaises(SystemExit):
            load_bridget({'BRIDGET_APPROVAL_MAILBOX': '../elsewhere'})


class TheSilentZeroIsReproduced(unittest.TestCase):
    """Pre-fix control: without the split, the re-point empties the view."""

    def test_prefix_behaviour_scanning_the_delivery_box_finds_nothing(self):
        b, home = load_bridget({'POGO_MAIL_DIR': '~/.macguffin/mail/daniel'})
        # The agent's request sits in `human`; the representative's rewrite of
        # it sits in `daniel`. Both boxes are populated, as they would be live.
        write_mail(home_mail(home) / 'human' / 'new', '1.mail', RAW_SUBJECT)
        write_mail(home_mail(home) / 'daniel' / 'new', '1.mail', REWRITTEN_SUBJECT)

        # PRE-FIX: point the scan at the delivery box, which is what
        # `scan_pending_approvals` did before mg-18bf.
        with mock.patch.object(b, 'APPROVAL_DIR', b.MAIL_DIR):
            prefix = b.scan_pending_approvals()
        self.assertEqual(prefix.subjects, [],
                         'control did not reproduce the bug — the rewritten '
                         'subject matched the approval regex, so the premise '
                         'of mg-18bf is wrong, not the fix')

        # POST-FIX: the same install, the scan on its own box.
        after = b.scan_pending_approvals()
        self.assertEqual(after.subjects, [RAW_SUBJECT])

    def test_the_prefix_zero_was_indistinguishable_from_an_empty_plate(self):
        """Why the control is not enough on its own.

        Both worlds produced `[]`. That is the whole harm: the bug had no
        instrument, so the fix needs one — see ZeroSaysWhichWorldItIsIn.
        """
        b, home = load_bridget({'POGO_MAIL_DIR': '~/.macguffin/mail/daniel'})
        write_mail(home_mail(home) / 'daniel' / 'new', '1.mail', REWRITTEN_SUBJECT)
        with mock.patch.object(b, 'APPROVAL_DIR', b.MAIL_DIR):
            misconfigured = b.scan_pending_approvals().subjects

        b2, home2 = load_bridget()
        (home_mail(home2) / 'human' / 'new').mkdir(parents=True)
        truly_empty = b2.scan_pending_approvals().subjects

        self.assertEqual(misconfigured, truly_empty)
        self.assertEqual(misconfigured, [])


class ZeroSaysWhichWorldItIsIn(unittest.TestCase):
    """A remedy is an artifact of the same kind as the defect.

    The split fixes today's misconfiguration; it does not make
    misconfiguration impossible. `BRIDGET_APPROVAL_MAILBOX` and
    `BRIDGET_APPROVAL_RE` between them can still point the scan at a box whose
    subjects it will never match. So the zero has to say which zero it is.
    """

    def test_missing_directory_is_named_not_reported_as_none_pending(self):
        b, home = load_bridget()
        scan = b.scan_pending_approvals()
        self.assertFalse(scan.exists)
        rendered = '\n'.join(b.render_approvals(scan, 'Awaiting your approval'))
        self.assertIn('does not exist', rendered)
        self.assertIn(str(b.APPROVAL_DIR), rendered)

    def test_empty_directory_reads_as_a_trustworthy_none(self):
        b, home = load_bridget()
        (home_mail(home) / 'human' / 'new').mkdir(parents=True)
        scan = b.scan_pending_approvals()
        self.assertTrue(scan.exists)
        self.assertEqual(scan.scanned, 0)
        rendered = '\n'.join(b.render_approvals(scan, 'Awaiting your approval'))
        self.assertIn('none', rendered)
        self.assertNotIn('⚠️', rendered)

    def test_mail_present_but_nothing_matched_is_loud(self):
        """The mg-f04b shape, caught in the act."""
        b, home = load_bridget()
        write_mail(home_mail(home) / 'human' / 'new', '1.mail', REWRITTEN_SUBJECT)
        write_mail(home_mail(home) / 'human' / 'new', '2.mail', 'weekly digest')
        scan = b.scan_pending_approvals()
        self.assertEqual(scan.subjects, [])
        self.assertEqual(scan.scanned, 2)
        rendered = '\n'.join(b.render_approvals(scan, 'Awaiting your approval'))
        self.assertIn('⚠️', rendered)
        self.assertIn('2 unread', rendered)
        # Names the two knobs that could have caused it.
        self.assertIn('BRIDGET_APPROVAL_RE', rendered)
        self.assertIn('BRIDGET_APPROVAL_MAILBOX', rendered)

    def test_the_three_zeroes_render_differently(self):
        """The property the pre-fix control shows was absent."""
        b, home = load_bridget()
        mailnew = home_mail(home) / 'human' / 'new'

        def rendered():
            return '\n'.join(b.render_approvals(b.scan_pending_approvals(), 'X'))

        absent = rendered()
        mailnew.mkdir(parents=True)
        empty = rendered()
        write_mail(mailnew, '1.mail', REWRITTEN_SUBJECT)
        unmatched = rendered()

        self.assertEqual(len({absent, empty, unmatched}), 3,
                         'two of the three zero-approval worlds still read '
                         'identically')

    def test_a_real_approval_still_renders_as_a_list(self):
        b, home = load_bridget()
        write_mail(home_mail(home) / 'human' / 'new', '1.mail', RAW_SUBJECT)
        write_mail(home_mail(home) / 'human' / 'new', '2.mail', 'weekly digest')
        rendered = '\n'.join(
            b.render_approvals(b.scan_pending_approvals(), 'Awaiting your approval'))
        self.assertIn('Awaiting your approval (1):', rendered)
        self.assertIn(RAW_SUBJECT, rendered)
        self.assertNotIn('⚠️', rendered)


class DivergenceIsStatedWhereItCanBeSeen(unittest.TestCase):
    """The split creates a divergence nothing else in bridget would show."""

    def test_status_line_names_the_box_and_the_divergence(self):
        b, home = load_bridget({'POGO_MAIL_DIR': '~/.macguffin/mail/daniel'})
        line = b.approvals_status_line()
        self.assertIn(str(b.APPROVAL_DIR), line)
        self.assertIn(str(b.MAIL_DIR), line)
        self.assertIn('separate', line)
        self.assertIn('BRIDGET_APPROVAL_MAILBOX', line)

    def test_status_line_says_so_when_the_boxes_coincide(self):
        b, _ = load_bridget()
        line = b.approvals_status_line()
        self.assertIn('same box', line)

    def test_settings_command_carries_the_line(self):
        b, home = load_bridget({'POGO_MAIL_DIR': '~/.macguffin/mail/daniel'})
        out = b.render_settings(b.SETTINGS.summary())
        self.assertIn('Approval scan:', out)
        self.assertIn(str(b.APPROVAL_DIR), out)


class TheScanStaysAScan(unittest.TestCase):
    """Leaving the scan on `human` is only safe because it mutates nothing.

    `human` is the representative's work queue, watched by a fail-open deadman
    that fires when mail sits there unprocessed. A reader that moved files out
    of `new/` would satisfy that deadman and a dead representative would mean
    silence — which is the *other* reason POGO_MAIL_DIR had to move (README,
    "Running behind a representative"). So this reader must never write.
    """

    def test_scanning_leaves_every_file_where_it_was(self):
        b, home = load_bridget({'POGO_MAIL_DIR': '~/.macguffin/mail/daniel'})
        newdir = home_mail(home) / 'human' / 'new'
        write_mail(newdir, '1.mail', RAW_SUBJECT)
        write_mail(newdir, '2.mail', 'weekly digest')
        before = sorted(p.name for p in newdir.iterdir())

        b.scan_pending_approvals()

        self.assertEqual(sorted(p.name for p in newdir.iterdir()), before)
        self.assertFalse((home_mail(home) / 'human' / 'cur').exists(),
                         'the scan created a cur/ — it is filing mail, which '
                         'is exactly what the deadman must not see')

    def test_mark_mail_read_still_operates_on_the_delivery_box(self):
        """`read <mg-id>` marks what the human was DM'd, so it follows the
        watcher — the split moves the scan and nothing else."""
        b, home = load_bridget({'POGO_MAIL_DIR': '~/.macguffin/mail/daniel'})
        write_mail(home_mail(home) / 'human' / 'new', '1.mail', RAW_SUBJECT)
        write_mail(home_mail(home) / 'daniel' / 'new', '1.mail',
                   REWRITTEN_SUBJECT, body='ref mg-deadbeef')

        self.assertEqual(b.mark_mail_read(mg_id='mg-deadbeef'), 1)
        self.assertTrue((home_mail(home) / 'daniel' / 'cur' / '1.mail').exists())
        self.assertTrue((home_mail(home) / 'human' / 'new' / '1.mail').exists())


class ScanSurvivesUnreadableEntries(unittest.TestCase):
    """`scanned` is the denominator of the warning, so it must count what was
    actually read — a file the scan could not open would otherwise inflate it
    and produce a "none matched" warning about mail nobody looked at."""

    def test_a_subdirectory_in_new_is_skipped_not_counted(self):
        b, home = load_bridget()
        newdir = home_mail(home) / 'human' / 'new'
        write_mail(newdir, '1.mail', RAW_SUBJECT)
        (newdir / 'tmpdir').mkdir()
        scan = b.scan_pending_approvals()
        self.assertEqual(scan.scanned, 1)
        self.assertEqual(scan.subjects, [RAW_SUBJECT])


if __name__ == '__main__':
    unittest.main(verbosity=2)
