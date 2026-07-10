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

"""Threading against the *real* mg binary, over two full round-trips.

Every other suite feeds the bridge hand-authored maildir fixtures. That is why
this bug shipped: the fixtures all carried the conversation root in
`References[0]`, and mg only ever writes it there on the first hop. A fixture
can assert whatever it likes; mg is the contract.

So these tests shell out to mg and let it write the headers. They fail on the
message that used to split the thread, and they pin the mg behaviour that makes
the message-id index necessary, so a future reader can tell whether the index
is still earning its keep.

An agent answers its mail one of two ways, and they lose the conversation root
at *different hops*, so both are driven here:

    mg mail send --in-reply-to X   seeds `References: [X]` — the parent, and
                                   nothing else, at every hop. Loses the root
                                   immediately; splits on round-trip one.
    mg mail reply AGENT/ID         extends the References of the message it
                                   read. Carries the root through hop one, then
                                   extends an ancestry that never knew it.
                                   Splits on round-trip two.

Covering only one of them cannot distinguish a complete fix from an incomplete
one — which is the coverage argument that put this file here in the first place,
applied to itself.

Skipped when mg is absent, or when the mg on PATH predates correlation IDs —
there is nothing to thread with, and the bridge is documented to degrade rather
than fail there.
"""
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))

from bridget_core.conversations import ConversationStore  # noqa: E402
from bridget_core.mgshim import help_advertises_in_reply_to  # noqa: E402
from test_threading import FakeTextChannel, FakeUser, load_threaded  # noqa: E402

MG = shutil.which('mg')


def _mg_speaks_correlation_ids() -> bool:
    if not MG:
        return False
    try:
        out = subprocess.run([MG, 'mail', 'send', '--help'],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return help_advertises_in_reply_to(out)


HAVE_MG = _mg_speaks_correlation_ids()


@unittest.skipUnless(HAVE_MG, 'no mg on PATH that speaks --in-reply-to')
class RealMgTestCase(unittest.IsolatedAsyncioTestCase):
    """A sandboxed macguffin root, a real mg, and a fake Discord."""

    #: Which command the agent answers the bridge with: 'send' for
    #: `mg mail send --in-reply-to`, 'reply' for `mg mail reply`.
    AGENT_REPLY = 'send'

    def setUp(self):
        self.b = load_threaded()
        self.home = Path(self.b._fake_home)
        self.channel = FakeTextChannel(555, client=self.b.client)
        self.b.client.channels[555] = self.channel
        self.user = FakeUser()

        # mg roots at $HOME/.macguffin. load_threaded() restores HOME once the
        # module is imported, so re-point it for the subprocesses themselves.
        self._saved_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.home)
        self._mg('init')

        self.delivered: set[str] = set()
        self.mayor_seen: set[str] = set()

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self._saved_home
        shutil.rmtree(self.home, ignore_errors=True)

    # -- driving the real mg ----------------------------------------------

    def _mg(self, *args) -> str:
        r = subprocess.run([MG, *args], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, f'mg {" ".join(args)} failed: {r.stderr}')
        return r.stdout

    def _maildir(self, agent: str, box: str = 'new') -> Path:
        return self.home / '.macguffin' / 'mail' / agent / box

    def _mailbox(self, agent: str) -> list[str]:
        """Every message ever delivered to `agent`. `mg mail reply` marks the
        message it answers read, moving it from `new/` to `cur/`, so a scan of
        `new/` alone loses mail the moment the agent uses that command."""
        names = []
        for box in ('new', 'cur'):
            path = self._maildir(agent, box)
            if path.exists():
                names.extend(p.name for p in path.iterdir())
        return sorted(names)

    def _agent_sends_to_human(self, subject: str, body: str, in_reply_to: str = '') -> str:
        """`mg mail send --in-reply-to` — the stateless primitive. Seeds
        `References: [parent]` and nothing else, at every hop."""
        args = ['mail', 'send', 'human', '--from=mayor',
                f'--subject={subject}', f'--body={body}', '--json']
        if in_reply_to:
            args.append(f'--in-reply-to={in_reply_to}')
        return json.loads(self._mg(*args))['msg_id']

    def _agent_replies_to_human(self, body: str, parent: str) -> str:
        """`mg mail reply` — the ancestry-extending wrapper. Seeds
        `References: [<the parent's own references>, parent]`.

        `--force` because the test drives mg as a third party: reply marks the
        original read, and mg refuses to do that to a mailbox `$POGO_AGENT_NAME`
        does not own.
        """
        out = self._mg('mail', 'reply', f'mayor/{parent}', f'--body={body}',
                       '--force', '--json')
        return json.loads(out)['msg_id']

    def _agent_answers(self, body: str, subject: str = 'Re: design review') -> str:
        """However this suite's agent answers the bridge's latest reply.

        The two commands fail differently — `mg mail send --in-reply-to` drops
        the root on the *first* hop, `mg mail reply` carries it for one hop and
        drops it on the *second* — so the contract below is run under both.
        """
        parent = self._newest_mail_to_mayor()
        if self.AGENT_REPLY == 'reply':
            return self._agent_replies_to_human(body, parent)
        return self._agent_sends_to_human(subject, body, in_reply_to=parent)

    def _newest_mail_to_mayor(self) -> str:
        """The id of the bridge's latest reply, as the agent would read it."""
        unseen = [n for n in self._mailbox('mayor') if n not in self.mayor_seen]
        self.assertTrue(unseen, 'the bridge sent the agent nothing')
        self.mayor_seen.update(unseen)
        return unseen[-1]

    async def _deliver_new_mail(self) -> None:
        """Run every unseen maildir file through the real delivery path."""
        for name in self._mailbox('human'):
            if name in self.delivered:
                continue
            self.delivered.add(name)
            path = next(p for p in (self._maildir('human', 'new') / name,
                                    self._maildir('human', 'cur') / name) if p.exists())
            await self.b.deliver_mail(
                self.user, name, self.b.parse_mail(path.read_text()))

    def _human_replies(self, text: str) -> None:
        conv = self.b.CONVERSATIONS.by_thread(self.channel.threads[0].id)
        self.assertIsNotNone(conv, 'the thread lost its conversation')
        ack = self.b.reply_in_conversation(text, conv)
        self.assertTrue(ack.ok, f'reply not delivered: {ack.kind}: {ack.reason}')

    async def _two_round_trips(self) -> str:
        """root -> human reply -> agent reply -> human reply -> agent reply."""
        root = self._agent_sends_to_human('design review', 'what do you think?')
        await self._deliver_new_mail()

        self._human_replies('looks good')
        self._agent_answers('shipping it')
        await self._deliver_new_mail()

        self._human_replies('one more thing')
        self._agent_answers('fixed')
        await self._deliver_new_mail()
        return root


class TestMgHeaderContract(RealMgTestCase):
    """Pin what mg actually writes. These are the facts the fixtures got wrong.

    Neither reply command carries the conversation root past its own horizon.
    They just lose it at different hops, which is why one of them is not enough
    coverage to tell a complete fix from an incomplete one.
    """

    def _refs(self, agent: str, msg_id: str) -> list[str]:
        path = next(p for p in (self._maildir(agent, 'new') / msg_id,
                                self._maildir(agent, 'cur') / msg_id) if p.exists())
        return self.b.parse_mail(path.read_text())['references']

    def _human_mails_mayor(self, body: str, in_reply_to: str) -> str:
        out = self._mg('mail', 'send', 'mayor', '--from=human', '--subject=Re: design review',
                       f'--body={body}', f'--in-reply-to={in_reply_to}', '--json')
        return json.loads(out)['msg_id']

    def test_send_in_reply_to_names_the_parent_and_loses_the_root_at_hop_two(self):
        root = self._agent_sends_to_human('design review', 'q')
        second = self._agent_sends_to_human('Re: design review', 'a', in_reply_to=root)
        third = self._agent_sends_to_human('Re: design review', 'b', in_reply_to=second)

        # The first hop looks exactly like every fixture in the tree...
        self.assertEqual(self._refs('human', second), [root])
        # ...and the second is where that assumption dies.
        self.assertEqual(self._refs('human', third), [second])
        self.assertNotIn(root, self._refs('human', third),
                         'mg now extends ancestry; the message-id index may be redundant')

    def test_mg_mail_reply_extends_ancestry_but_loses_the_root_at_hop_two(self):
        """`mg mail reply` reads the original and extends *its* References. The
        original was seeded by `mg mail send --in-reply-to`, which carries only
        the parent — so the extension is two ids deep and the root falls off one
        hop later than it does for a bare send."""
        root = self._agent_sends_to_human('design review', 'q')
        h1 = self._human_mails_mayor('a', in_reply_to=root)
        m2 = self._agent_replies_to_human('b', parent=h1)
        h2 = self._human_mails_mayor('c', in_reply_to=m2)
        m3 = self._agent_replies_to_human('d', parent=h2)

        # Hop one keeps the root, which is why a `mg mail reply` chain used to
        # survive exactly one round-trip longer than a bare send.
        self.assertEqual(self._refs('human', m2), [root, h1])
        # Hop two extends the ancestry of a message that never knew the root.
        self.assertEqual(self._refs('human', m3), [m2, h2])
        self.assertNotIn(root, self._refs('human', m3),
                         'mg mail reply now reaches the root; the index may be redundant')

    def test_send_reports_the_id_it_assigned(self):
        """The bridge cannot index its own replies without this."""
        out = self._mg('mail', 'send', 'human', '--from=mayor',
                       '--subject=s', '--body=b', '--json')
        msg_id = json.loads(out)['msg_id']
        self.assertTrue(msg_id)
        self.assertTrue((self._maildir('human') / msg_id).exists(),
                        'msg_id is not the maildir filename')

    def test_mg_mail_reply_reports_the_id_it_assigned(self):
        root = self._agent_sends_to_human('design review', 'q')
        h1 = self._human_mails_mayor('a', in_reply_to=root)
        m2 = self._agent_replies_to_human('b', parent=h1)
        self.assertTrue(m2)
        self.assertIn(m2, self._mailbox('human'), 'msg_id is not the maildir filename')


class TwoRoundTripContract:
    """What must hold whichever command the agent answers with.

    A mixin, not a TestCase: the concrete classes below pair it with
    `RealMgTestCase` once per reply command, so each variant gets its own fresh
    sandbox rather than sharing one and hiding a failure behind the other.
    """

    async def test_the_whole_exchange_stays_in_one_thread(self):
        """The regression. Before the message-id index this made three threads:
        one per inbound mail, because each reply keyed on its own parent."""
        root = await self._two_round_trips()

        self.assertEqual(len(self.channel.threads), 1,
                         f'{len(self.channel.threads)} threads for one conversation')
        self.assertEqual(len(self.b.CONVERSATIONS), 1,
                         'the conversation map grew an entry per message')
        self.assertEqual(len(self.channel.threads[0].sent), 3,
                         'not every inbound mail landed in the thread')
        self.assertIn(root, self.b.CONVERSATIONS.keys(),
                      'the conversation drifted off its root')

    async def test_the_bridge_indexes_the_replies_it_sends(self):
        """The agent's reply names *our* message id. Whether that is the only
        thing tying the answer to its conversation depends on how the agent
        replied, so this assertion — not the thread count — is what pins the
        outbound fold under both commands.

        Under `mg mail send --in-reply-to`, our id is the whole of the answer's
        `References`, so dropping the fold breaks threading outright. Under
        `mg mail reply`, the answer's ancestry also names the previous *inbound*
        message, which the store already knows, so threading limps on and only
        this test and `test_a_restart_rejoins_the_conversation` notice.
        """
        await self._two_round_trips()

        conv = next(iter(self.b.CONVERSATIONS.values()))
        outbound = self._mailbox('mayor')
        self.assertEqual(len(outbound), 2, 'expected two replies from the human')
        for msg_id in outbound:
            self.assertIn(msg_id, conv.message_ids,
                          'an outbound reply was never folded into the conversation')

    async def test_a_mute_still_applies_after_the_first_round_trip(self):
        """Mutes are keyed on the conversation. A conversation that re-keys on
        every message is a mute that silently stops applying."""
        root = self._agent_sends_to_human('design review', 'q')
        await self._deliver_new_mail()
        self.b.SETTINGS.mute(root)
        dms_before = len(self.user.sent)

        self._human_replies('looks good')
        self._agent_answers('shipping it')
        await self._deliver_new_mail()

        self.assertEqual(len(self.user.sent), dms_before,
                         'the mute stopped applying once the chain moved past the root')
        self.assertEqual(len(self.channel.threads[0].sent), 2,
                         'a muted conversation must still be logged in its thread')

    async def test_a_restart_rejoins_the_conversation(self):
        """The index is rebuilt from the persisted map, not held in memory."""
        root = self._agent_sends_to_human('design review', 'q')
        await self._deliver_new_mail()
        self._human_replies('looks good')
        reply_id = self._newest_mail_to_mayor()

        # Reload the store from disk, as a restarted bridge would.
        store = ConversationStore(self.b.CONVERSATIONS.path)
        self.assertEqual(store.resolve([reply_id]), root,
                         'a restarted bridge forgot the reply it had sent')


class TestTwoRoundTripsViaSend(TwoRoundTripContract, RealMgTestCase):
    """The agent answers with `mg mail send --in-reply-to`, as agents mostly do.

    Every hop names only its parent, so an unfixed bridge splits the thread on
    the *first* round-trip, and a bridge that indexes inbound mail but forgets
    its own replies splits it just the same.
    """

    AGENT_REPLY = 'send'


class TestTwoRoundTripsViaReply(TwoRoundTripContract, RealMgTestCase):
    """The agent answers with `mg mail reply`, which extends the ancestry it
    read instead of seeding a fresh one.

    That carries the root through hop one, so an unfixed bridge splits the
    thread on the *second* round-trip rather than the first. The two commands
    therefore fail at different hops, and a suite covering one cannot tell a
    complete fix from an incomplete one.
    """

    AGENT_REPLY = 'reply'


class TestInboundMessageSurvivesRoundTrip(RealMgTestCase):
    """A long inbound message reaches the agent's mailbox with every byte intact.

    mg-7e0c. The unit tests assert the argv bridget builds; this asserts what
    lands in the maildir, through the real `mg`. Only the real thing can show
    that mg stores an over-long subject rather than clipping it — which is what
    proves the 200-char cap was bridget's invention, not a limit it was obeying.
    """

    #: Longer than MG_SUBJECT_LIMIT, with the operative clause last. If any hop
    #: truncates, the authorization inverts: "you can delete and recreate" with
    #: its condition and its refusal both gone.
    LONG = (
        'Sleep wake is not a critical repo you can delete and recreate if '
        'you first confirm nothing else depends on it. '
        + 'Here is some more context that pushes us past the cap. ' * 8
        + 'FINAL INSTRUCTION: do not delete anything.'
    )

    def _only_mail(self, agent: str) -> dict:
        box = self._maildir(agent)
        paths = sorted(box.iterdir())
        self.assertEqual(len(paths), 1, f'expected exactly one mail in {box}')
        return self.b.parse_mail(paths[0].read_text())

    def test_long_channel_chat_arrives_whole(self):
        self.assertGreater(len(self.LONG), 200)
        reply = self.b.handle_channel_message(self.LONG, {'agent': 'mayor'})
        self.assertTrue(reply.startswith('✓ mailed'), reply)

        mail = self._only_mail('mayor')
        self.assertEqual(mail['body'], self.LONG)
        self.assertTrue(mail['body'].endswith('do not delete anything.'))
        self.assertNotEqual(mail['body'], '(no body)')
        # The subject is a label: bounded, one line, and visibly shortened —
        # with a marker, not a bare slice that reads as a finished sentence.
        self.assertLessEqual(len(mail['subject']), 200)
        self.assertIn('[truncated', mail['subject'])
        self.assertNotIn('\n', mail['subject'])

    def test_long_mail_verb_arrives_whole(self):
        # The `mail` verb used to build its own argv and skip build_send_args.
        reply = self.b.handle_command('mail ' + self.LONG)
        self.assertTrue(reply.startswith('✓ mailed'), reply)
        mail = self._only_mail(self.b.CONFIG['mail_recipient'])
        self.assertEqual(mail['body'], self.LONG)

    async def test_on_message_delivers_a_long_channel_message_whole(self):
        """The Discord event handler itself, end to end, into a real maildir.

        `handle_channel_message` sits one call below `on_message`; this drives
        the entrypoint Discord actually calls, so the routing is covered too.
        The bytes are compared against what the human 'typed', which is the
        acceptance test as specified: send > cap, read it back, compare.
        """
        channel = FakeTextChannel(777, client=self.b.client)
        self.b.CHANNELS_BY_SNOWFLAKE[777] = {'agent': 'mayor', 'inbound': True}
        message = types.SimpleNamespace(
            author=types.SimpleNamespace(id=self.b.USER_ID, bot=False),
            channel=channel, content=self.LONG)

        await self.b.on_message(message)

        self.assertTrue(channel.sent, 'bridget acknowledged nothing')
        self.assertTrue(channel.sent[0].startswith('✓ mailed'), channel.sent[0])
        mail = self._only_mail('mayor')
        self.assertEqual(mail['from'], 'human')
        self.assertEqual(mail['body'], self.LONG)

    def test_mg_itself_stores_an_overlong_subject(self):
        # The premise of the fix. If this ever fails, mg grew a cap of its own
        # and bridget must start splitting or marking rather than eliding.
        subject = 'S' * 500
        self._mg('mail', 'send', 'mayor', '--from=human',
                 f'--subject={subject}', '--body=b')
        self.assertEqual(self._only_mail('mayor')['subject'], subject)


if __name__ == '__main__':
    if not HAVE_MG:
        print('test_mg_threading: skipped (no mg with correlation IDs on PATH)')
    unittest.main(verbosity=2)
