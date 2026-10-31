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

"""bridget-prune (mg-eb78f), driven as a subprocess against a stub Discord.

The stub is a real HTTP server on 127.0.0.1 holding an in-memory channel, so
the script's actual request/429/pagination/permission code runs. Nothing here
talks to discord.com.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'tests'))
import testtmp  # noqa: E402

SCRIPT = REPO / 'bridget-prune'
TOKEN = 'fakeprunetesttoken.notreal.x'  # deliberately NOT token-shaped (test_secrets)
GUILD, CHANNEL, OTHER_GUILD, OTHER_CHANNEL = '100', '200', '900', '300'
BOT, HUMAN = '10', '11'
EPOCH = 1420070400000
NOW = dt.datetime.now(dt.timezone.utc)
MANAGE_MESSAGES, MANAGE_THREADS = 1 << 13, 1 << 34


def sid(when: dt.datetime, n: int = 0) -> str:
    return str(((int(when.timestamp() * 1000) - EPOCH) << 22) + n)


def iso(when: dt.datetime) -> str:
    return when.isoformat()


class Stub:
    def __init__(self):
        self.messages = {}       # id -> message, CHANNEL only
        self.threads = {}        # id -> thread
        self.everyone_perms = 1 << 10 | 1 << 16
        self.channel_guild = GUILD
        self.calls = []          # (method, path, auth)
        self.ratelimit_first_delete = False
        self.n = 0

    def add(self, age_days: float, author=BOT) -> str:
        self.n += 1
        when = NOW - dt.timedelta(days=age_days)
        mid = sid(when, self.n)
        self.messages[mid] = {'id': mid, 'timestamp': iso(when),
                              'author': {'id': author}, 'content': 'x'}
        return mid

    def add_thread(self, age_days: float, archived: bool) -> str:
        self.n += 1
        when = NOW - dt.timedelta(days=age_days)
        tid = sid(when, self.n)
        self.threads[tid] = {'id': tid, 'parent_id': CHANNEL,
                             'last_message_id': tid, 'archived': archived,
                             'thread_metadata': {'archive_timestamp': iso(when)}}
        return tid

    def handle(self, method, path, body):
        p, _, q = path.partition('?')
        qs = dict(kv.split('=', 1) for kv in q.split('&') if '=' in kv)
        ch = f'/channels/{CHANNEL}'
        if p == '/users/@me':
            return 200, {'id': BOT, 'username': 'bot'}
        if p == ch:
            return 200, {'id': CHANNEL, 'name': 'log', 'guild_id': self.channel_guild,
                         'permission_overwrites': []}
        if p == f'/guilds/{GUILD}':
            return 200, {'id': GUILD, 'owner_id': HUMAN}
        if p == f'/guilds/{GUILD}/roles':
            return 200, [{'id': GUILD, 'permissions': str(self.everyone_perms)}]
        if p == f'/guilds/{GUILD}/members/{BOT}':
            return 200, {'roles': []}
        if p == f'{ch}/messages' and method == 'GET':
            ids = sorted(self.messages, key=int, reverse=True)
            if 'before' in qs:
                ids = [i for i in ids if int(i) < int(qs['before'])]
            return 200, [self.messages[i] for i in ids[:int(qs.get('limit', 50))]]
        if p == f'{ch}/messages' and method == 'POST':
            self.n += 1
            mid = sid(dt.datetime.now(dt.timezone.utc), self.n)
            self.messages[mid] = {'id': mid, 'author': {'id': BOT},
                                  'timestamp': iso(dt.datetime.now(dt.timezone.utc)),
                                  'content': body['content']}
            return 200, self.messages[mid]
        if p.startswith(f'{ch}/messages/'):
            mid = p.rsplit('/', 1)[1]
            if mid not in self.messages:
                return 404, {'message': 'Unknown Message', 'code': 10008}
            if method == 'GET':
                return 200, self.messages[mid]
            if method == 'DELETE':
                if self.ratelimit_first_delete:
                    self.ratelimit_first_delete = False
                    return 429, {'retry_after': 0.05, 'global': False}
                if self.messages[mid]['author']['id'] != BOT and \
                        not self.everyone_perms & MANAGE_MESSAGES:
                    return 403, {'message': 'Missing Permissions'}
                del self.messages[mid]
                return 204, None
        if p == f'/guilds/{GUILD}/threads/active':
            return 200, {'threads': [t for t in self.threads.values() if not t['archived']]}
        if p == f'{ch}/threads/archived/public':
            return 200, {'threads': [t for t in self.threads.values() if t['archived']],
                         'has_more': False}
        if p == f'{ch}/threads/archived/private':
            return 403, {'message': 'Missing Access'}
        if p.startswith('/channels/') and method == 'DELETE':
            tid = p.rsplit('/', 1)[1]
            if tid in self.threads and self.everyone_perms & MANAGE_THREADS:
                del self.threads[tid]
                return 200, {'id': tid}
            return 403, {'message': 'Missing Permissions'}
        return 404, {'message': 'stub: no route ' + p}


def serve(stub: Stub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _do(self):
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            path = self.path[len('/api'):]
            stub.calls.append((self.command, path, self.headers.get('Authorization')))
            status, payload = stub.handle(self.command, path, body)
            data = json.dumps(payload).encode() if payload is not None else b''
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_DELETE = _do

    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.td = testtmp.TemporaryDirectory('prune')
        self.dir = Path(self.td.__enter__())
        self.env = self.dir / 'bridget.env'
        self.env.write_text(f'DISCORD_BOT_TOKEN={TOKEN}\nDISCORD_SERVER_ID={GUILD}\n'
                            f'BRIDGET_LOG_CHANNEL_ID={CHANNEL}\n')
        self.log = self.dir / 'prune.log'
        self.stub = Stub()
        self.srv = serve(self.stub)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.td.cleanup()

    def run_prune(self, *args, base=None):
        env = dict(os.environ, BRIDGET_PRUNE_API_BASE=base or
                   f'http://127.0.0.1:{self.srv.server_address[1]}/api')
        r = subprocess.run([sys.executable, str(SCRIPT), '--env', str(self.env),
                            '--log', str(self.log), *args],
                           capture_output=True, text=True, env=env, timeout=60)
        return r

    def deletes(self):
        return [p for m, p, _ in self.stub.calls if m == 'DELETE']

    def test_live_deletes_only_old_own_messages_in_the_one_channel(self):
        old = [self.stub.add(8 + i) for i in range(150)]   # > one page
        new = [self.stub.add(1), self.stub.add(6.9)]
        foreign = self.stub.add(30, author=HUMAN)
        r = self.run_prune()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(set(self.stub.messages), set(new) | {foreign})
        self.assertTrue(all(p.startswith(f'/channels/{CHANNEL}/messages/')
                            for p in self.deletes()))
        self.assertEqual(len(self.deletes()), len(old))
        text = self.log.read_text()
        self.assertIn('messages: listed 153; deleted 150; kept 3 '
                      '(newer than cutoff 2, not deletable 1, failed 0)', text)
        self.assertIn('threads: skipped (bot lacks Manage Threads', text)

    def test_dry_run_deletes_nothing_but_counts(self):
        for i in range(5):
            self.stub.add(10 + i)
        self.stub.add(1)
        r = self.run_prune('--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.deletes(), [])
        self.assertEqual(len(self.stub.messages), 6)
        self.assertIn('would delete 5; kept 1', self.log.read_text())

    def test_dry_run_positive_control_counts_known_old_message(self):
        # --days 0 makes every message "old": the count must be non-zero, or
        # the zero from a normal dry run proves nothing.
        self.stub.add(0.01)
        r = self.run_prune('--dry-run', '--days', '0')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('would delete 1; kept 0', self.log.read_text())
        self.assertEqual(self.deletes(), [])

    def test_live_run_refuses_days_below_one(self):
        self.stub.add(0.01)
        r = self.run_prune('--days', '0')
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(self.stub.messages), 1)

    def test_429_retry_after_is_honoured(self):
        self.stub.add(9)
        self.stub.ratelimit_first_delete = True
        r = self.run_prune()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.stub.messages, {})
        self.assertEqual(len(self.deletes()), 2)        # the 429, then the retry
        text = self.log.read_text()
        self.assertIn('retry_after=0.05s', text)
        self.assertIn('deleted 1;', text)
        self.assertIn('1 rate-limited', text)

    def test_refuses_channel_outside_configured_guild(self):
        self.stub.add(9)
        self.stub.channel_guild = OTHER_GUILD
        r = self.run_prune()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.deletes(), [])
        self.assertIn('is not in guild', self.log.read_text())

    def test_threads_pruned_only_when_manage_threads_present(self):
        old_a = self.stub.add_thread(20, archived=True)
        old_b = self.stub.add_thread(9, archived=False)
        fresh = self.stub.add_thread(2, archived=False)
        r = self.run_prune()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.stub.threads), 3)      # skipped, not failed
        self.stub.everyone_perms |= MANAGE_THREADS
        r = self.run_prune()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(set(self.stub.threads), {fresh})
        self.assertIn(f'/channels/{old_a}', self.deletes())
        self.assertIn(f'/channels/{old_b}', self.deletes())
        self.assertIn('threads: listed 3; deleted 2; kept 1', self.log.read_text())

    def test_manage_messages_extends_to_other_authors(self):
        self.stub.add(9, author=HUMAN)
        self.stub.everyone_perms |= MANAGE_MESSAGES
        r = self.run_prune()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.stub.messages, {})

    def test_self_test_posts_prunes_and_verifies_only_its_marker(self):
        keep = self.stub.add(0.5)
        old = self.stub.add(30)
        r = self.run_prune('--self-test')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('self-test: PASS', self.log.read_text())
        self.assertEqual(set(self.stub.messages), {keep, old})   # nothing else touched
        self.assertEqual(len(self.deletes()), 1)

    def test_token_never_printed_or_logged(self):
        self.stub.add(9)
        self.stub.ratelimit_first_delete = True
        r = self.run_prune()
        self.run_prune('--dry-run')
        self.run_prune('--self-test')
        # Control: the token really was in play (it reached the server), so
        # its absence below is evidence, not an artifact of never loading it.
        self.assertIn('Bot ' + TOKEN, {a for _, _, a in self.stub.calls})
        for blob in (r.stdout, r.stderr, self.log.read_text()):
            self.assertNotIn(TOKEN, blob)
            self.assertNotIn(TOKEN.split('.')[0], blob)

    def test_api_override_refused_off_loopback(self):
        r = self.run_prune('--dry-run', base='https://evil.example/api')
        self.assertEqual(r.returncode, 2)
        self.assertIn('loopback', self.log.read_text())
        self.assertEqual(self.stub.calls, [])


class PlistTemplateTest(unittest.TestCase):
    def test_template_is_a_daily_non_runatload_job_on_the_prune_script(self):
        import plistlib
        raw = (REPO / 'com.pogo.bridget-prune.plist.example').read_text()
        self.assertNotIn('--', raw.split('<plist')[0].split('<!--', 1)[1].rsplit('-->', 1)[0])
        pl = plistlib.loads(raw.replace('__HOME__', '/Users/x').encode())
        self.assertEqual(pl['Label'], 'com.pogo.bridget-prune')
        self.assertEqual(pl['ProgramArguments'][-3:],
                         ['/Users/x/.pogo/bin/bridget-prune', '--days', '7'])
        self.assertFalse(pl['RunAtLoad'])
        self.assertEqual(set(pl['StartCalendarInterval']), {'Hour', 'Minute'})
        self.assertNotIn('TOKEN', raw.upper().replace('THE TOKEN', ''))


if __name__ == '__main__':
    unittest.main(verbosity=2)
