#!/usr/bin/env python3
"""Threading against the *real* mg binary, over two full round-trips.

Every other suite feeds the bridge hand-authored maildir fixtures. That is why
this bug shipped: the fixtures all carried the conversation root in
`References[0]`, and mg only ever writes it there on the first hop. From the
second hop on, `mg mail send --in-reply-to X` — the stateless primitive both
the bridge and every agent reply through — seeds `References: [X]`, where X is
the *parent*. A fixture can assert whatever it likes; mg is the contract.

So these tests shell out to mg and let it write the headers. They fail on the
message that used to split the thread, and they pin the mg behaviour that makes
the message-id index necessary, so a future reader can tell whether the index
is still earning its keep.

Skipped when mg is absent, or when the mg on PATH predates correlation IDs —
there is nothing to thread with, and the bridge is documented to degrade rather
than fail there.
"""
import json
import os
import shutil
import subprocess
import sys
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

    def _maildir(self, agent: str) -> Path:
        return self.home / '.macguffin' / 'mail' / agent / 'new'

    def _agent_mails_human(self, subject: str, body: str, in_reply_to: str = '') -> str:
        """What an agent does: `mg mail send --in-reply-to <the message it read>`.

        Not `mg mail reply`. The bug hid behind the assumption that agents use
        the ancestry-extending command; overwhelmingly they use this one.
        """
        args = ['mail', 'send', 'human', '--from=mayor',
                f'--subject={subject}', f'--body={body}', '--json']
        if in_reply_to:
            args.append(f'--in-reply-to={in_reply_to}')
        return json.loads(self._mg(*args))['msg_id']

    def _newest_mail_to_mayor(self) -> str:
        """The id of the bridge's latest reply, as the agent would read it."""
        unseen = sorted(p.name for p in self._maildir('mayor').iterdir()
                        if p.name not in self.mayor_seen)
        self.assertTrue(unseen, 'the bridge sent the agent nothing')
        self.mayor_seen.update(unseen)
        return unseen[-1]

    async def _deliver_new_mail(self) -> None:
        """Run every unseen maildir file through the real delivery path."""
        for path in sorted(self._maildir('human').iterdir()):
            if path.name in self.delivered:
                continue
            self.delivered.add(path.name)
            await self.b.deliver_mail(
                self.user, path.name, self.b.parse_mail(path.read_text()))

    def _human_replies(self, text: str) -> None:
        conv = self.b.CONVERSATIONS.by_thread(self.channel.threads[0].id)
        self.assertIsNotNone(conv, 'the thread lost its conversation')
        ack = self.b.reply_in_conversation(text, conv)
        self.assertTrue(ack.ok, f'reply not delivered: {ack.text}')

    async def _two_round_trips(self) -> str:
        """root -> human reply -> agent reply -> human reply -> agent reply."""
        root = self._agent_mails_human('design review', 'what do you think?')
        await self._deliver_new_mail()

        self._human_replies('looks good')
        self._agent_mails_human('Re: design review', 'shipping it',
                                in_reply_to=self._newest_mail_to_mayor())
        await self._deliver_new_mail()

        self._human_replies('one more thing')
        self._agent_mails_human('Re: design review', 'fixed',
                                in_reply_to=self._newest_mail_to_mayor())
        await self._deliver_new_mail()
        return root


class TestMgHeaderContract(RealMgTestCase):
    """Pin what mg actually writes. These are the facts the fixtures got wrong."""

    def test_references_carry_the_parent_not_the_root_past_the_first_hop(self):
        root = self._agent_mails_human('design review', 'q')
        second = self._agent_mails_human('Re: design review', 'a', in_reply_to=root)
        third = self._agent_mails_human('Re: design review', 'b', in_reply_to=second)

        hop2 = self.b.parse_mail((self._maildir('human') / second).read_text())
        hop3 = self.b.parse_mail((self._maildir('human') / third).read_text())

        # The first hop looks exactly like every fixture in the tree...
        self.assertEqual(hop2['references'], [root])
        # ...and the second is where that assumption dies.
        self.assertEqual(hop3['references'], [second])
        self.assertNotIn(root, hop3['references'],
                         'mg now extends ancestry; the message-id index may be redundant')

    def test_send_reports_the_id_it_assigned(self):
        """The bridge cannot index its own replies without this."""
        out = self._mg('mail', 'send', 'human', '--from=mayor',
                       '--subject=s', '--body=b', '--json')
        msg_id = json.loads(out)['msg_id']
        self.assertTrue(msg_id)
        self.assertTrue((self._maildir('human') / msg_id).exists(),
                        'msg_id is not the maildir filename')


class TestTwoRoundTrips(RealMgTestCase):
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
        """The agent's reply names *our* message id and nothing older. If we
        never recorded it, `resolve()` would miss and mint a new conversation."""
        await self._two_round_trips()

        conv = next(iter(self.b.CONVERSATIONS.values()))
        outbound = sorted(p.name for p in self._maildir('mayor').iterdir())
        self.assertEqual(len(outbound), 2, 'expected two replies from the human')
        for msg_id in outbound:
            self.assertIn(msg_id, conv.message_ids,
                          'an outbound reply was never folded into the conversation')

    async def test_a_mute_still_applies_after_the_first_round_trip(self):
        """Mutes are keyed on the conversation. A conversation that re-keys on
        every message is a mute that silently stops applying."""
        root = self._agent_mails_human('design review', 'q')
        await self._deliver_new_mail()
        self.b.SETTINGS.mute(root)
        dms_before = len(self.user.sent)

        self._human_replies('looks good')
        self._agent_mails_human('Re: design review', 'shipping it',
                                in_reply_to=self._newest_mail_to_mayor())
        await self._deliver_new_mail()

        self.assertEqual(len(self.user.sent), dms_before,
                         'the mute stopped applying once the chain moved past the root')
        self.assertEqual(len(self.channel.threads[0].sent), 2,
                         'a muted conversation must still be logged in its thread')

    async def test_a_restart_rejoins_the_conversation(self):
        """The index is rebuilt from the persisted map, not held in memory."""
        root = self._agent_mails_human('design review', 'q')
        await self._deliver_new_mail()
        self._human_replies('looks good')
        reply_id = self._newest_mail_to_mayor()

        # Reload the store from disk, as a restarted bridge would.
        store = ConversationStore(self.b.CONVERSATIONS.path)
        self.assertEqual(store.resolve([reply_id]), root,
                         'a restarted bridge forgot the reply it had sent')


if __name__ == '__main__':
    if not HAVE_MG:
        print('test_mg_threading: skipped (no mg with correlation IDs on PATH)')
    unittest.main(verbosity=2)
