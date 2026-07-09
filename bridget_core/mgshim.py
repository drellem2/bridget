# bridget_core.mgshim — mg CLI capability seam. GPL-3.0-or-later.
"""The correlation-ID seam: use `mg mail send --in-reply-to` when mg has it.

macguffin gh#66 (`Message-Id` on every message, `--in-reply-to`, `References`)
is the feature that lets a reply thread instead of landing as an unrelated
top-level mail. It is not reliably present: it ships in some builds of `mg` and
not others, and the binary on a developer's PATH can be swapped underneath a
running bridge by an unrelated `go install`.

So the bridge treats it as a *capability*, not a dependency:

    auto (default) — probe `mg mail send --help` once; use the flag if listed.
    on             — assume it exists; useful if the probe is wrong.
    off            — never pass it; every reply is a fresh top-level mail.

Degradation is total and silent-to-the-user: without correlation IDs, replies
still deliver, they just don't thread. The bridge never fails a send because mg
was too old. And because a probe result can go stale — mg really does get
rebuilt mid-session — a send rejected with `unknown flag: --in-reply-to`
downgrades the capability and retries once, rather than reporting a spurious
undeliverable.
"""
from __future__ import annotations

import sys

FLAG_IN_REPLY_TO = '--in-reply-to'

MODES = ('auto', 'on', 'off')
DEFAULT_MODE = 'auto'


def help_advertises_in_reply_to(help_text: str) -> bool:
    """True if `mg mail send --help` lists the flag.

    Matches the flag only where cobra prints it in the Flags block, i.e. as a
    token, so a mention in the prose description alone does not count. (An mg
    build has shipped whose long description documented the flag its flag-set
    did not actually define — the prose is not the contract.)
    """
    if not help_text:
        return False
    in_flags = False
    for line in help_text.splitlines():
        if line.startswith('Flags:') or line.startswith('Global Flags:'):
            in_flags = True
            continue
        if in_flags:
            if line and not line[:1].isspace():
                in_flags = False  # left the Flags block
                continue
            if line.strip().split()[:1] == [FLAG_IN_REPLY_TO]:
                return True
    return False


def is_unknown_flag_error(text: str, flag: str = FLAG_IN_REPLY_TO) -> bool:
    """True if mg rejected `flag` as unknown — the signal that our cached
    capability is stale because the binary changed under us."""
    low = (text or '').lower()
    return 'unknown flag' in low and flag.lstrip('-').lower() in low


class MgCapabilities:
    """Lazily-probed, downgradable view of what the local `mg` supports.

    `run_help` is a zero-arg callable returning `mg mail send --help` output.
    Injected rather than called directly so the core stays free of subprocess
    and the probe is trivially testable.
    """

    def __init__(self, run_help, mode: str = DEFAULT_MODE):
        self.mode = mode if mode in MODES else DEFAULT_MODE
        self._run_help = run_help
        self._probed: bool | None = None

    @property
    def correlation_ids(self) -> bool:
        if self.mode == 'on':
            return True
        if self.mode == 'off':
            return False
        if self._probed is None:
            try:
                self._probed = help_advertises_in_reply_to(self._run_help())
            except Exception as e:
                print(f'mg capability probe failed, assuming no correlation IDs: {e}',
                      file=sys.stderr)
                self._probed = False
        return self._probed

    def downgrade(self) -> None:
        """Record that mg rejected the flag. Sticks for the process lifetime
        unless mode is 'on', which the operator asked us not to second-guess."""
        if self.mode != 'on':
            self._probed = False

    def describe(self) -> str:
        state = 'on' if self.correlation_ids else 'off'
        how = 'forced' if self.mode in ('on', 'off') else 'detected'
        return f'{state} ({how})'


def build_send_args(agent: str, subject: str, body: str, *,
                    sender: str = 'human', in_reply_to: str = '') -> list[str]:
    """The argv for one `mg mail send`. `in_reply_to` is appended only when the
    caller has decided mg supports it."""
    args = [
        'mail', 'send', agent,
        f'--from={sender}',
        f'--subject={subject[:200]}',
        f'--body={body or "(no body)"}',
    ]
    if in_reply_to:
        args.append(f'{FLAG_IN_REPLY_TO}={in_reply_to}')
    return args
