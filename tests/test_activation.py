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

"""bridget-supervise knows which revision it is running (mg-6ca7).

The defect: `bridget-supervise` execs the bridget file in the working checkout,
on whatever branch that checkout happens to be on. On 2026-08-11 it was on
`representative-relay-mg-65d2`, two commits behind `origin/main`, so the merged
duplication limit (mg-5521) was not running — while the merge succeeded, the
MERGED mail arrived, and the process stayed healthy. Restarting bridget re-ran
the same old code and looked fine doing it.

Two properties make it worth a suite. It is SILENT: nothing in the log named a
revision, so no reader could tell. And it SURVIVES A RESTART: "restart it" is
the standard remedy for "the fix isn't live", and here it confirms the wrong
state rather than correcting it.

So the load-bearing assertions are that the NEW code actually ran, and that the
old behaviour is reproduced as a positive control (`BRIDGET_ACTIVATION=off`) so
these tests can fail. Everything after that is the refusals: the checkout is not
moved when the tree is dirty, when the branch carries commits main does not
have, or when it is a polecat worktree someone is testing in — and every refusal
is loud, because the defect here was silence and a silent refusal is the same
defect with better manners.

No network and no launchctl: `origin/main` is a ref, and this writes it directly.
"""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'tests'))

import testtmp  # noqa: E402

SUPERVISE = REPO / 'bridget-supervise'

#: mg-35b1's anchored prefix. A multi-line alert body must not escape it.
STAMP_RE = re.compile(r'^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] ')

GIT_IDENTITY = [
    '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
    '-c', 'commit.gpgsign=false',
]


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(['git', '-C', str(repo), *GIT_IDENTITY, *args],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(f'git {" ".join(args)} failed: {r.stderr.strip()}')
    return r.stdout.strip()


def fake_bridget_source(label: str) -> str:
    """A stand-in for the bridget script that says which revision ran it.

    `$MARKER` and `$SPAWN_HOOK` come from the environment rather than being
    baked in, because the file is committed and its content is what the
    supervisor is choosing between.
    """
    return (
        '#!/bin/bash\n'
        f'echo {label} >> "$MARKER"\n'
        'if [ -n "${SPAWN_HOOK:-}" ] && [ -x "$SPAWN_HOOK" ]; then "$SPAWN_HOOK"; fi\n'
        'exit 5\n'
    )


def write_exec(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class Checkout:
    """A git checkout in the shape mg-6ca7 was found in.

    main carries a `bridget` that prints `new`; a feature branch sits one commit
    behind it on a `bridget` that prints `old`; and `origin/main` — the ref the
    supervisor compares against — points at main. The feature branch has NO
    upstream, exactly as `representative-relay-mg-65d2` had none: a check
    phrased as "behind its tracking branch" would have had nothing to say about
    it, which is why the supervisor compares against the deploy ref instead.
    """

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '-q', str(root)], check=True, timeout=60)
        git(root, 'symbolic-ref', 'HEAD', 'refs/heads/main')

        write_exec(root / 'bridget', fake_bridget_source('old'))
        git(root, 'add', 'bridget')
        git(root, 'commit', '-qm', 'old')
        self.old = git(root, 'rev-parse', 'HEAD')

        git(root, 'branch', 'feature')

        write_exec(root / 'bridget', fake_bridget_source('new'))
        git(root, 'commit', '-qam', 'new')
        self.new = git(root, 'rev-parse', 'HEAD')

        self.publish()

    def publish(self):
        """Point `origin/main` at whatever local main holds — what a fetch does."""
        git(self.root, 'update-ref', 'refs/remotes/origin/main',
            git(self.root, 'rev-parse', 'main'))

    def park_on_feature(self):
        git(self.root, 'checkout', '-q', 'feature')

    @property
    def bin(self) -> str:
        return str(self.root / 'bridget')

    def head(self) -> str:
        return git(self.root, 'rev-parse', 'HEAD')

    def branch(self) -> str:
        return git(self.root, 'rev-parse', '--abbrev-ref', 'HEAD')


class ActivationCase(unittest.TestCase):
    """One tmpdir per test: a $HOME, a checkout, an alert sink, a marker file."""

    def setUp(self):
        self.td = testtmp.TemporaryDirectory('activation')
        self.addCleanup(self.td.cleanup)
        self.tmp = Path(self.td.name)
        self.marker = self.tmp / 'ran'
        self.sink = self.tmp / 'alerts'
        # `cmd <subject> <body> <recipient>`: the recipient is the third
        # argument so a test can prove the report reached `human` and not the
        # mayor's coordination inbox.
        self.alert_cmd = write_exec(
            self.tmp / 'alert-cmd',
            '#!/bin/bash\nprintf "TO=%s\\n%s\\n%s\\n---\\n" "$3" "$1" "$2" >> '
            + str(self.sink) + '\n')

    def checkout(self, at: Path = None) -> Checkout:
        return Checkout(at or self.tmp / 'checkout')

    def supervise(self, bridget_bin: str, timeout: int = 60, **overrides):
        """Run the real supervisor for a single spawn against a real checkout."""
        env = {k: v for k, v in os.environ.items() if not k.startswith('BRIDGET_')}
        env.update(
            HOME=str(self.tmp),
            MARKER=str(self.marker),
            BRIDGET_BIN=bridget_bin,
            BRIDGET_ALERT_CMD=str(self.alert_cmd),
            BRIDGET_ALERT_STAMP=str(self.tmp / 'alert.stamp'),
            BRIDGET_REVISION_ALERT_STAMP=str(self.tmp / 'revision.stamp'),
            BRIDGET_MAX_SPAWNS='1',
            BRIDGET_MIN_BACKOFF='0',
            BRIDGET_HEALTHY_RUNTIME='9999',
        )
        env.update(overrides)
        return subprocess.run(['bash', str(SUPERVISE)], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def ran(self) -> list:
        return self.marker.read_text().split() if self.marker.exists() else []

    def alerts(self) -> str:
        return self.sink.read_text() if self.sink.exists() else ''


class ActivationTest(ActivationCase):
    def test_the_spawn_line_names_the_revision_it_started(self):
        """The log mg-6ca7 needed and did not have.

        `starting bridget (spawn #14)` is true of every revision that has ever
        existed. A reader could not have told the stale run from a current one,
        which is why nobody did.
        """
        co = self.checkout()
        r = self.supervise(co.bin)
        self.assertEqual(r.returncode, 5)
        self.assertIn(f'starting bridget (spawn #1) at {co.new[:7]} on main '
                      '(current with origin/main)', r.stdout)
        self.assertEqual(self.alerts(), '',
                         'a current checkout is not news; do not mail about it')

    def test_a_checkout_parked_on_a_stale_branch_is_moved_to_the_deploy_ref(self):
        """The mg-6ca7 repair, end to end: the NEW code is what runs."""
        co = self.checkout()
        co.park_on_feature()

        r = self.supervise(co.bin)

        self.assertEqual(self.ran(), ['new'],
                         'the supervisor started the old code anyway')
        self.assertEqual(co.head(), co.new)
        self.assertEqual(co.branch(), 'main')
        self.assertEqual(r.returncode, 5)
        self.assertIn('fast-forwarded', r.stdout)
        self.assertIn('fast-forwarded', self.alerts())

    def test_nothing_is_discarded_by_the_move(self):
        """The branch ref still points where it did; the commit is still there.

        The hard constraint on this ticket is that a supervisor which destroys a
        human's work is strictly worse than one that runs old code, so the
        lossless case has to be provably lossless rather than merely intended.
        """
        co = self.checkout()
        co.park_on_feature()
        self.supervise(co.bin)
        self.assertEqual(git(co.root, 'rev-parse', 'feature'), co.old)
        self.assertEqual(git(co.root, 'cat-file', '-t', co.old), 'commit')

    def test_the_pre_fix_behaviour_is_reproducible_and_is_the_defect(self):
        """Positive control. Without it these tests prove only that a feature is
        on; not that its absence is what was reported.

        `BRIDGET_ACTIVATION=off` is the supervisor as it was on 2026-08-11: it
        runs the stale checkout, says nothing about any revision, and looks
        exactly as healthy as a current one.
        """
        co = self.checkout()
        co.park_on_feature()

        r = self.supervise(co.bin, BRIDGET_ACTIVATION='off')

        self.assertEqual(self.ran(), ['old'])
        self.assertEqual(co.branch(), 'feature')
        self.assertNotIn('behind', r.stdout)
        self.assertNotIn(co.new[:7], r.stdout, 'nothing named the revision')
        self.assertEqual(self.alerts(), '', 'nothing raised a hand — the defect')
        self.assertIn('starting bridget (spawn #1)', r.stdout,
                      'and it looked exactly as healthy as a current checkout')

    def test_a_restart_on_a_stale_checkout_used_to_confirm_the_wrong_state(self):
        """Restarting is the remedy people reach for. It must now correct.

        Two spawns of the pre-fix supervisor run the old code twice: that is the
        property that makes this class of failure dangerous rather than merely
        annoying. With activation on, the first spawn is already current.
        """
        co = self.checkout()
        co.park_on_feature()
        before = self.supervise(co.bin, BRIDGET_ACTIVATION='off',
                                BRIDGET_MAX_SPAWNS='2')
        self.assertEqual(self.ran(), ['old', 'old'], before.stdout)

        self.marker.unlink()
        self.supervise(co.bin, BRIDGET_MAX_SPAWNS='2')
        self.assertEqual(self.ran(), ['new', 'new'])

    def test_a_merge_landing_mid_run_is_picked_up_by_the_next_spawn(self):
        """Why this runs before every spawn rather than once at startup.

        A merge lands while bridget is up; bridget then exits. That restart is
        the moment that either activates the new code or silently re-runs the
        old, and it is not a moment the supervisor's startup ever sees again.
        """
        co = self.checkout()
        # Level to begin with, so the first spawn has nothing to activate. The
        # checkout moves off main before main is rewound, or the rewind would
        # leave the worktree holding a modification it never made.
        co.park_on_feature()
        git(co.root, 'update-ref', 'refs/heads/main', co.old)
        co.publish()
        # The refinery merging to main while bridget is up, and pushing.
        hook = write_exec(self.tmp / 'merge-hook', f"""#!/bin/bash
            git -C {co.root} update-ref refs/heads/main {co.new}
            git -C {co.root} update-ref refs/remotes/origin/main {co.new}
        """)

        r = self.supervise(co.bin, BRIDGET_MAX_SPAWNS='2', SPAWN_HOOK=str(hook))

        self.assertEqual(self.ran(), ['old', 'new'],
                         'the second spawn did not pick up the merge: ' + r.stdout)
        self.assertEqual(co.head(), co.new)
        self.assertEqual(co.branch(), 'main')


class ActivationRefusalTest(ActivationCase):
    """Every case where the supervisor must NOT touch the checkout — and must
    say so anyway. Refusing in silence would be the reported defect with better
    manners."""

    def assert_refused_loudly(self, r, co, at: str, branch: str):
        self.assertEqual(co.head(), at, 'the checkout was moved')
        self.assertEqual(co.branch(), branch, 'the branch was changed')
        self.assertEqual(self.ran(), ['old'], 'a refusal must still start bridget')
        self.assertEqual(r.returncode, 5, 'a refusal must never be fatal')
        self.assertIn('STALE', r.stdout)
        self.assertIn('STALE', r.stderr,
                      'launchd routes stderr to bridget.err.log, which is where '
                      'a human tails a daemon that is misbehaving')
        self.assertIn('---', self.alerts(), 'nobody was told')

    def test_a_dirty_tree_is_refused_and_left_exactly_as_it_was(self):
        """Someone may be mid-test in there. Their edit outranks our activation."""
        co = self.checkout()
        co.park_on_feature()
        edit = '#!/bin/bash\necho old >> "$MARKER"\n# a human is mid-test\nexit 5\n'
        (co.root / 'bridget').write_text(edit)

        r = self.supervise(co.bin)

        self.assert_refused_loudly(r, co, co.old, 'feature')
        self.assertEqual((co.root / 'bridget').read_text(), edit,
                         'the uncommitted edit was discarded')
        self.assertIn('uncommitted changes', r.stdout)

    def test_a_branch_carrying_unmerged_commits_is_never_moved(self):
        """Ahead of the deploy ref: moving would strand real work."""
        co = self.checkout()
        co.park_on_feature()
        write_exec(co.root / 'bridget', fake_bridget_source('old'))
        (co.root / 'wip.txt').write_text('mid-test\n')
        git(co.root, 'add', 'wip.txt')
        git(co.root, 'commit', '-qm', 'wip')
        git(co.root, 'update-ref', 'refs/remotes/origin/main',
            git(co.root, 'rev-parse', 'feature~1'))
        tip = co.head()

        r = self.supervise(co.bin)

        self.assert_refused_loudly(r, co, tip, 'feature')
        self.assertIn('unmerged code', r.stdout)
        self.assertIn('ahead of', r.stdout)

    def test_a_diverged_checkout_is_never_moved(self):
        co = self.checkout()
        co.park_on_feature()
        (co.root / 'wip.txt').write_text('mine\n')
        git(co.root, 'add', 'wip.txt')
        git(co.root, 'commit', '-qm', 'wip')
        tip = co.head()

        r = self.supervise(co.bin)

        self.assert_refused_loudly(r, co, tip, 'feature')
        self.assertIn('diverged from', r.stdout)

    def test_a_polecat_worktree_is_reported_but_never_yanked_to_main(self):
        """The escape hatch and the repair must not fight each other.

        `BRIDGET_ALLOW_EPHEMERAL_BIN=1` exists so a polecat can supervise its own
        build. Fast-forwarding that checkout to main would destroy precisely the
        mid-test work the dirty-tree rule protects — this repair, committing the
        defect it repairs, one directory over.
        """
        co = self.checkout(self.tmp / '.pogo' / 'polecats' / '6ca7')
        co.park_on_feature()

        r = self.supervise(co.bin, BRIDGET_ALLOW_EPHEMERAL_BIN='1')

        self.assert_refused_loudly(r, co, co.old, 'feature')
        self.assertIn('polecat worktree', r.stdout)

    def test_warn_mode_reports_everything_and_touches_nothing(self):
        co = self.checkout()
        co.park_on_feature()

        r = self.supervise(co.bin, BRIDGET_ACTIVATION='warn')

        self.assert_refused_loudly(r, co, co.old, 'feature')
        self.assertIn('BRIDGET_ACTIVATION=warn', r.stdout)

    def test_a_refusal_names_both_commands_that_would_fix_it(self):
        """A warning a reader cannot act on is a warning they learn to skip."""
        co = self.checkout()
        co.park_on_feature()
        r = self.supervise(co.bin, BRIDGET_ACTIVATION='warn')
        self.assertIn('git checkout main && git merge --ff-only origin/main', r.stdout)
        self.assertIn('launchctl kickstart -k gui/', r.stdout)


class ActivationReportingTest(ActivationCase):
    def test_the_report_goes_to_human_not_the_mayor(self):
        """It is Daniel's fix that is not running, and his log that is read.

        The mayor's inbox is for coordination; this is user-facing, so it goes
        where the apple-side notifier picks it up.
        """
        co = self.checkout()
        co.park_on_feature()
        self.supervise(co.bin, BRIDGET_ACTIVATION='warn')
        self.assertIn('TO=human', self.alerts())
        self.assertNotIn('TO=mayor', self.alerts())

    def test_a_stale_revision_and_an_unrunnable_target_do_not_throttle_each_other(self):
        """One cooldown stamp for both would let either swallow the other for
        fifteen minutes, and they say entirely different things."""
        co = self.checkout()
        co.park_on_feature()
        self.supervise(co.bin, BRIDGET_ACTIVATION='warn')
        self.assertEqual(self.alerts().count('---'), 1)

        # A second run inside the cooldown: same stamp, so the mail is held.
        r = self.supervise(co.bin, BRIDGET_ACTIVATION='warn')
        self.assertEqual(self.alerts().count('---'), 1, 're-mailed inside cooldown')
        self.assertIn('throttled', r.stdout)
        self.assertIn('STALE', r.stdout,
                      'throttling the mail must never throttle the log')

        # The unrelated path alert has its own stamp and is not held by it.
        self.supervise(str(self.tmp / 'nope'), BRIDGET_ACTIVATION='warn')
        self.assertIn('cannot run bridget', self.alerts())

    def test_every_line_of_a_stale_report_carries_the_fleet_date(self):
        """mg-35b1's invariant, on the multi-line body this change introduces.

        The remedy is on the later lines. A continuation line no date pattern
        matches is that ticket's defect at a smaller radius.
        """
        co = self.checkout()
        co.park_on_feature()
        r = self.supervise(co.bin, BRIDGET_ACTIVATION='warn')
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        self.assertGreater(len(lines), 4, 'fixture sanity: the report is multi-line')
        for line in lines:
            self.assertRegex(line, STAMP_RE)

    def test_a_supervisor_that_changed_says_it_is_still_the_old_copy(self):
        """The repair is an artifact of the same kind as the defect.

        bridget is re-exec'd every spawn and so is current for free. This script
        is already running, from the copy bash opened at startup — so a
        fast-forward that changes `bridget-supervise` leaves the supervisor
        stale in exactly the way the ticket is about. It cannot fix that in
        place, so it must not imply that it did.
        """
        root = self.tmp / 'checkout'
        co = self.checkout(root)
        write_exec(root / 'bridget-supervise', SUPERVISE.read_text())
        git(root, 'add', 'bridget-supervise')
        git(root, 'commit', '-qm', 'supervisor')
        git(root, 'branch', '-f', 'feature', 'HEAD')
        (root / 'bridget-supervise').write_text(
            SUPERVISE.read_text() + '\n# a later change to the supervisor\n')
        git(root, 'commit', '-qam', 'change the supervisor')
        co.publish()
        co.park_on_feature()

        env = {k: v for k, v in os.environ.items() if not k.startswith('BRIDGET_')}
        env.update(HOME=str(self.tmp), MARKER=str(self.marker),
                   BRIDGET_BIN=co.bin,
                   BRIDGET_ALERT_CMD=str(self.alert_cmd),
                   BRIDGET_ALERT_STAMP=str(self.tmp / 'alert.stamp'),
                   BRIDGET_REVISION_ALERT_STAMP=str(self.tmp / 'revision.stamp'),
                   BRIDGET_MAX_SPAWNS='1', BRIDGET_MIN_BACKOFF='0',
                   BRIDGET_HEALTHY_RUNTIME='9999')
        # The copy inside the checkout, so the script can recognise itself.
        r = subprocess.run(['bash', str(root / 'bridget-supervise')], env=env,
                           capture_output=True, text=True, timeout=60)

        self.assertEqual(self.ran(), ['new'], 'bridget itself must be current')
        self.assertIn('bridget-supervise changed too', r.stdout)
        self.assertIn('launchctl kickstart -k gui/', r.stdout)
        self.assertIn('STILL THE OLD COPY', self.alerts())


class ActivationTolerationTest(ActivationCase):
    """What must NOT become a new way for bridget to fail to start."""

    def test_a_target_outside_any_checkout_starts_normally_and_silently(self):
        """An install by copy is a legitimate deployment. It just cannot be
        revision-checked, and that is not an error to report every five seconds."""
        plain = write_exec(self.tmp / 'plain' / 'bridget', fake_bridget_source('plain'))
        r = self.supervise(str(plain))
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.ran(), ['plain'])
        self.assertIn('starting bridget (spawn #1)\n', r.stdout)
        self.assertEqual(self.alerts(), '')

    def test_a_checkout_with_no_deploy_ref_says_so_and_starts_anyway(self):
        """Cannot answer the question is a different state from answering
        'fine', and the log has to be able to tell them apart."""
        co = self.checkout()
        git(co.root, 'update-ref', '-d', 'refs/remotes/origin/main')
        git(co.root, 'branch', '-m', 'main', 'trunk')

        r = self.supervise(co.bin)

        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.ran(), ['new'])
        self.assertIn('has no origin/main', r.stdout)

    def test_a_local_main_stands_in_when_there_is_no_remote(self):
        """A clone with no `origin` is still deployable; the local branch of the
        same name is the next best authority."""
        co = self.checkout()
        git(co.root, 'update-ref', '-d', 'refs/remotes/origin/main')
        co.park_on_feature()

        r = self.supervise(co.bin)

        self.assertEqual(self.ran(), ['new'])
        self.assertEqual(co.branch(), 'main')
        self.assertIn('behind main', r.stdout)

    def test_git_missing_from_PATH_is_not_fatal_and_is_not_silent(self):
        """launchd's PATH is not a login shell's. bridget must still start —
        but a missing git must not make a stale checkout look like a current
        one, which is this ticket's failure shape rebuilt inside its own fix."""
        co = self.checkout()
        co.park_on_feature()
        r = self.supervise(co.bin, BRIDGET_GIT=str(self.tmp / 'no-such-git'))
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.ran(), ['old'])
        self.assertIn('is not on PATH', r.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
