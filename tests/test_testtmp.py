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

"""The harness's own temp-directory ownership (mg-1f20).

tests/testtmp.py is the one piece of this repo whose failure mode is silent by
construction: a sweep that reaps too little leaks, a sweep that reaps too much
deletes a LIVE run's fixtures and surfaces as a defect in whatever branch
happened to be running. Neither shows up in any other suite, so both directions
are pinned here.

The measurement that this actually stopped the leak is not in this file — it is
tests/tmpdir-leak_test.sh, which counts $TMPDIR before and after a real suite
run. This file is about the rule; that one is about the number.
"""
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'tests'))

import testtmp  # noqa: E402


class NamingTest(unittest.TestCase):
    def test_a_directory_is_nested_under_the_single_root(self):
        d = testtmp.mkdtemp('naming')
        self.addCleanup(testtmp.rmtree, d)
        self.assertEqual(d.parent, testtmp.root())
        self.assertEqual(testtmp.root().name, testtmp.ROOT_NAME)
        self.assertTrue(d.is_dir())

    def test_the_name_carries_this_process_id(self):
        d = testtmp.mkdtemp('naming')
        self.addCleanup(testtmp.rmtree, d)
        self.assertEqual(testtmp.owner_pid(d.name), os.getpid())
        self.assertTrue(d.name.startswith('naming.'))

    def test_two_calls_do_not_collide(self):
        a = testtmp.mkdtemp('naming')
        b = testtmp.mkdtemp('naming')
        self.addCleanup(testtmp.rmtree, a)
        self.addCleanup(testtmp.rmtree, b)
        self.assertNotEqual(a, b)

    def test_a_purpose_reap_could_not_parse_is_refused(self):
        """An unparseable name is one reap() can only age out, which silently
        converts a pid-owned entry into a two-hour one. Loud beats silent."""
        for bad in ('', 'has.dot', 'has/slash', 'trailing.'):
            with self.assertRaises(ValueError, msg=f'purpose {bad!r} was accepted'):
                testtmp.mkdtemp(bad)

    def test_owner_pid_reads_back_what_entry_name_writes(self):
        self.assertEqual(testtmp.owner_pid(testtmp.entry_name('p', 4242, 7)), 4242)
        # …and refuses everything else, so a stranger's directory is aged out
        # rather than misread as owned.
        for name in ('tmpabcdef', 'p.notapid.1', 'p.1', 'p.-1.1', 'p.0.1', 'a.b.c.d'):
            self.assertIsNone(testtmp.owner_pid(name), name)


class ReapTest(unittest.TestCase):
    """The sweep, against a fixture root — both directions."""

    def setUp(self):
        self.root = testtmp.mkdtemp('reapfixture')
        self.addCleanup(testtmp.rmtree, self.root)

    def plant(self, name: str) -> Path:
        d = self.root / name
        d.mkdir()
        (d / 'fixture').write_text('x')
        return d

    def test_a_live_owners_directory_survives(self):
        """The direction that matters. This box runs several polecats and a
        refinery gate at once; a sweep that deleted a running suite's fixtures
        would surface as a branch defect, which is the failure this module
        exists to stop, arriving by a new route."""
        mine = self.plant(testtmp.entry_name('live', os.getpid(), 1))
        testtmp.reap(self.root)
        self.assertTrue(mine.is_dir(), 'reap deleted a LIVE process\'s directory')

    def test_a_live_owner_survives_however_old_it_is(self):
        """Ownership, not age: a suite that has been running for three hours
        still owns its fixtures."""
        mine = self.plant(testtmp.entry_name('old', os.getpid(), 2))
        ancient = time.time() - 10 * testtmp.STALE_AFTER
        os.utime(mine, (ancient, ancient))
        testtmp.reap(self.root)
        self.assertTrue(mine.is_dir())

    def test_a_dead_owners_directory_is_reclaimed(self):
        dead = dead_pid()
        gone = self.plant(testtmp.entry_name('dead', dead, 1))
        testtmp.reap(self.root)
        self.assertFalse(gone.exists(), f'pid {dead} is gone; its directory is not')

    def test_an_unowned_name_is_kept_until_it_is_stale(self):
        fresh = self.plant('tmpsomethingelse')
        testtmp.reap(self.root)
        self.assertTrue(fresh.is_dir(), 'a fresh unowned entry was reaped too eagerly')

        stale = time.time() - testtmp.STALE_AFTER - 60
        os.utime(fresh, (stale, stale))
        testtmp.reap(self.root)
        self.assertFalse(fresh.exists(), 'a stale unowned entry was never reclaimed')

    def test_reap_touches_nothing_outside_the_root_it_is_given(self):
        """It has no opinion about $TMPDIR at large. Reclaiming what has already
        leaked is a separate, careful operation from stopping the leak."""
        # In a second fixture root rather than beside the first: a sibling of
        # the real root would be a dead-pid entry that a CONCURRENT bridget test
        # process's own sweep is entitled to remove, and this suite would then
        # fail for being right.
        elsewhere = testtmp.mkdtemp('reapelsewhere')
        self.addCleanup(testtmp.rmtree, elsewhere)
        outside = elsewhere / f'not-ours.{dead_pid()}.1'
        outside.mkdir()
        testtmp.reap(self.root)
        self.assertTrue(outside.is_dir())

    def test_a_missing_root_is_not_an_error(self):
        testtmp.reap(self.root / 'nope')  # must not raise

    def test_a_recycled_pid_gets_a_clean_directory(self):
        """pids are reused, and the sweep KEEPS any entry whose pid is alive —
        so a dead namesake's directory reads as live. Handing this run that
        directory would seed it with another run's mail and settings, and the
        phantom records would look like a defect in whatever test read them."""
        import itertools
        saved = testtmp._seq
        testtmp._seq = itertools.count(9_000_001)
        self.addCleanup(setattr, testtmp, '_seq', saved)

        namesake = testtmp.root() / testtmp.entry_name('recycled', os.getpid(), 9_000_001)
        namesake.mkdir()
        (namesake / 'stale-mail').write_text('a run that ended days ago')

        fresh = testtmp.mkdtemp('recycled')
        self.addCleanup(testtmp.rmtree, fresh)
        self.assertEqual(fresh, namesake)
        self.assertEqual(list(fresh.iterdir()), [], 'inherited a dead namesake\'s state')


class RmtreeTest(unittest.TestCase):
    """Finding 3: the tree an ordinary rmtree cannot remove, and does not say so."""

    def setUp(self):
        self.tmp = testtmp.mkdtemp('rmtree')
        self.addCleanup(testtmp.rmtree, self.tmp)

    def readonly_nest(self, name: str) -> Path:
        """A go/pkg/mod in miniature: 0444 files inside a 0555 directory.

        The file mode is not what stops the removal — unlink is authorised by
        the PARENT directory's mode. That is why this shape defeats a plain
        rmtree while a tree of read-only files does not."""
        root = self.tmp / name
        cache = root / 'go' / 'pkg' / 'mod' / 'example.com' / 'thing@v1.2.3'
        cache.mkdir(parents=True)
        (cache / 'go.mod').write_text('module example.com/thing\n')
        (cache / 'go.mod').chmod(0o444)
        cache.chmod(0o555)
        (cache.parent).chmod(0o555)
        return root

    def test_the_stdlib_teardown_this_replaces_leaves_the_tree_and_says_nothing(self):
        """POSITIVE CONTROL. Without this, 'testtmp.rmtree removed it' proves
        nothing: it has to be shown that the teardown bridget actually shipped
        did not, and reported success anyway."""
        nest = self.readonly_nest('control')
        shutil.rmtree(nest, ignore_errors=True)  # exactly what four teardowns said
        self.assertTrue(nest.exists(),
                        'the control did not reproduce: nothing to fix')

    def test_it_removes_a_read_only_nest(self):
        nest = self.readonly_nest('real')
        testtmp.rmtree(nest)
        self.assertFalse(nest.exists())

    def test_it_raises_rather_than_reporting_a_removal_that_did_not_happen(self):
        """The other half of finding 3. A teardown whose failure is invisible is
        why the largest thing in the nest was never reclaimed AND nothing said
        so — so an unremovable path must be loud."""
        # Root can unlink regardless of mode, so the assertion is only
        # meaningful as an unprivileged user.
        if os.geteuid() == 0:
            self.skipTest('running as root: no mode makes a path unremovable')
        # The one shape the walk cannot repair from within: the target's PARENT
        # is unwritable, and rmtree is never given the parent to chmod.
        outside = self.tmp / 'outside'
        outside.mkdir()
        inner = outside / 'inner'
        inner.mkdir()
        (inner / 'f').write_text('x')
        os.chmod(outside, 0o500)
        self.addCleanup(os.chmod, outside, 0o700)
        with self.assertRaises(OSError):
            testtmp.rmtree(inner)

    def test_a_symlink_is_removed_not_followed(self):
        outside = self.tmp / 'keepme'
        outside.mkdir()
        (outside / 'precious').write_text('x')
        link = self.tmp / 'link'
        link.symlink_to(outside)
        testtmp.rmtree(link)
        # lexists, not Path.exists(follow_symlinks=False): that keyword is 3.12+
        # and this repo targets 3.10.
        self.assertFalse(os.path.lexists(link))
        self.assertTrue((outside / 'precious').exists(),
                        'rmtree followed a symlink out of the tree')

    def test_removing_something_absent_is_not_an_error(self):
        testtmp.rmtree(self.tmp / 'never-existed')


class RootTest(unittest.TestCase):
    """root() resolution, in a subprocess: it is cached per process, so these
    cannot run in-process without leaving this suite's own root poisoned."""

    def run_probe(self, body: str, tmpdir: Path):
        env = dict(os.environ, TMPDIR=str(tmpdir))
        return subprocess.run(
            [sys.executable, '-c',
             'import sys; sys.path.insert(0, %r)\nimport testtmp\n' % str(REPO / 'tests') + body],
            capture_output=True, text=True, env=env, timeout=60)

    def test_the_root_is_resolved_inside_the_pinned_tmpdir(self):
        pinned = testtmp.mkdtemp('rootpin')
        self.addCleanup(testtmp.rmtree, pinned)
        r = self.run_probe('print(testtmp.mkdtemp("probe"))', pinned)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip().startswith(str(pinned / testtmp.ROOT_NAME)),
                        f'{r.stdout.strip()} is not under the pinned $TMPDIR')

    def test_a_symlink_at_the_root_name_is_refused(self):
        """os.TempDir falls back to a world-writable /tmp when TMPDIR is unset —
        the case in CI — and there a pre-planted symlink at this name would have
        the sweep deleting a directory tree of somebody else's choosing.
        mkdir(exist_ok=True) follows the link and reports success, so the
        refusal has to be explicit."""
        pinned = testtmp.mkdtemp('rootlink')
        self.addCleanup(testtmp.rmtree, pinned)
        elsewhere = pinned / 'elsewhere'
        elsewhere.mkdir()
        (pinned / testtmp.ROOT_NAME).symlink_to(elsewhere)

        r = self.run_probe('testtmp.mkdtemp("probe")', pinned)
        self.assertNotEqual(r.returncode, 0, 'a symlinked root was accepted')
        self.assertIn('symlink', r.stderr)
        self.assertTrue(elsewhere.is_dir())

    def test_the_root_is_private_to_its_owner(self):
        """These hold fake $HOMEs carrying a bridget.env, which carries a bot
        token — the same 0600 reasoning as test_secrets."""
        self.assertEqual(stat.S_IMODE(testtmp.root().stat().st_mode), 0o700)


class TemporaryDirectoryTest(unittest.TestCase):
    def test_it_is_a_context_manager_yielding_a_path_string(self):
        with testtmp.TemporaryDirectory('ctx') as td:
            path = Path(td)
            self.assertTrue(path.is_dir())
            self.assertEqual(path.parent, testtmp.root())
        self.assertFalse(path.exists())

    def test_cleanup_is_idempotent_for_addCleanup_callers(self):
        td = testtmp.TemporaryDirectory('ctx')
        td.cleanup()
        td.cleanup()
        self.assertFalse(Path(td.name).exists())

    def test_it_removes_a_read_only_nest_the_stdlib_class_would_choke_on(self):
        """The stdlib TemporaryDirectory has its own read-only handling; what it
        does not have is the sweep, which is why this is a wrapper rather than a
        subclass. The removal still has to work."""
        td = testtmp.TemporaryDirectory('ctx')
        nest = Path(td.name) / 'mod'
        nest.mkdir()
        (nest / 'go.mod').write_text('x')
        (nest / 'go.mod').chmod(0o444)
        nest.chmod(0o555)
        td.cleanup()
        self.assertFalse(Path(td.name).exists())


def dead_pid() -> int:
    """A pid that is certainly gone: one we started and reaped ourselves."""
    p = subprocess.Popen([sys.executable, '-c', 'pass'])
    p.wait()
    return p.pid


if __name__ == '__main__':
    unittest.main(verbosity=2)
