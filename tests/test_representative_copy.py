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

"""The INBOUND seam of the representative relay (mg-65d2).

A representative crew agent owns `human` as its inbox and writes a separate
terminal box a person reads, so OUTBOUND agent mail routes through it. Inbound —
Daniel's Discord replies, which bridget relays to agents as `--from=human` — does
NOT: those are his own words, and putting a rewriter in the middle of them would
be worse than the problem the representative solves.

But with outbound relayed and inbound invisible, the representative has no record
of what it has already told him or what he has already settled. It re-summarises
answered questions, and agents get replies referring to text the representative
wrote rather than text they sent. Thread integrity breaks in one direction only.

The fix is a COPY, not the relay position, and this pins its four properties:

  1. OFF by default. An install with no POGO_REPRESENTATIVE behaves exactly as
     bridget always has — no extra send, no extra mailbox, no behaviour change.
  2. The copy goes to the representative's OWN box, never to `human`. `human` is
     the representative's work queue and is watched by a deadman that delivers
     anything unprocessed there after 15 minutes, raw. Copying Daniel's own words
     into it would make them a candidate for being notified back at him whenever
     the representative was slow.
  3. The copy is marked as a copy. Unmarked, it reads as a task, and the
     representative's whole job is deciding what to act on.
  4. A failed copy NEVER fails the relay. The reply has already reached its agent
     by the time this runs and the human has already been told so. Losing one
     line of the representative's context is a far smaller harm than reporting a
     delivered message as undeliverable.
"""
import unittest
from unittest import mock

from test_env_defaults import load_bridget


class RepresentativeCopyDisabledTest(unittest.TestCase):
    """Default: the seam does not exist."""

    def test_representative_default_is_empty(self):
        b = load_bridget()
        self.assertEqual(b.REPRESENTATIVE, '')

    def test_no_copy_is_sent_when_unconfigured(self):
        b = load_bridget()
        with mock.patch.object(b, 'run_mg', return_value=(0, '', '')) as run_mg:
            b.relay_copy_to_representative('mayor', 'subject', 'body')
        run_mg.assert_not_called()


class RepresentativeCopyEnabledTest(unittest.TestCase):
    """Configured: one extra send, to the right box, marked as a copy."""

    def setUp(self):
        self.b = load_bridget({'POGO_REPRESENTATIVE': 'representative'})

    def test_configured_name_is_read(self):
        self.assertEqual(self.b.REPRESENTATIVE, 'representative')

    def test_copy_addresses_the_representative_not_human(self):
        b = self.b
        with mock.patch.object(b, 'run_mg', return_value=(0, '', '')) as run_mg:
            b.relay_copy_to_representative('mayor', 'go ahead', 'yes, ship it')
        run_mg.assert_called_once()
        args = run_mg.call_args[0][0]
        self.assertIn('representative', args,
                      'the copy must be addressed to the representative')
        # `human` appears legitimately as --from (the copy carries Daniel's
        # authorship), so assert on the RECIPIENT position specifically: mg's
        # arg order is `mail send <recipient> ...`.
        recipient = args[args.index('send') + 1]
        self.assertEqual(recipient, 'representative')
        self.assertNotEqual(
            recipient, 'human',
            'the copy must never land in the deadman-watched work queue')

    def test_copy_is_marked_as_a_copy_and_names_the_real_addressee(self):
        b = self.b
        with mock.patch.object(b, 'run_mg', return_value=(0, '', '')) as run_mg:
            b.relay_copy_to_representative('mayor', 'go ahead', 'yes, ship it')
        blob = ' '.join(str(a) for a in run_mg.call_args[0][0])
        self.assertIn('[inbound copy]', blob,
                      'the subject must mark this as a copy, not a task')
        self.assertIn('mayor', blob,
                      'the copy must name the agent the reply actually went to')
        self.assertIn('yes, ship it', blob,
                      "Daniel's words must survive verbatim — the whole point of "
                      'not routing inbound through the representative')

    def test_a_refused_recipient_is_reported_not_swallowed(self):
        """mg refuses a recipient it has never seen.

        A POGO_REPRESENTATIVE naming an unregistered box therefore fails on
        every reply, forever, while the representative's context quietly rots.
        Silent-forever is precisely the shape mg-f04b found fifteen times; one
        stderr line per occurrence is what makes it findable at all.
        """
        b = self.b
        refusal = (1, '', 'no mailbox named "representative"')
        with mock.patch.object(b, 'run_mg', return_value=refusal), \
                mock.patch('sys.stderr') as stderr:
            b.relay_copy_to_representative('mayor', 'go ahead', 'yes, ship it')
        written = ''.join(
            str(c[0][0]) for c in stderr.write.call_args_list if c[0])
        self.assertIn('representative', written)
        self.assertIn('failed', written)

    def test_a_failed_copy_does_not_raise(self):
        """The relay has already succeeded by the time this runs."""
        b = self.b
        with mock.patch.object(b, 'run_mg', side_effect=OSError('mg is gone')):
            # No assertion needed beyond "does not propagate": an exception here
            # would surface in the Discord handler as a failed reply, telling the
            # human his message was undeliverable when it was already delivered.
            b.relay_copy_to_representative('mayor', 'go ahead', 'yes, ship it')


if __name__ == '__main__':
    unittest.main()
