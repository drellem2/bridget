# Forensics: what actually made #log unreadable on 2026-08-19 (mg-2ab2)

mg-27e0 shipped a bound on bridget's open-thread population and recorded, in the
commit message, the README, the CHANGELOG, two source comments and a design doc,
that ~966 open threads were what stopped Daniel's Discord client rendering `#log`.

**That causal claim is refuted.** This document records the measurements that
refute it, the mechanism that replaces it, and the part that is still open. The
bound itself is untouched and stays — see "What survives" below.

Everything here is read-only against Discord. Nothing was archived.


## The discriminating measurement, re-derived

The mayor's own numbers, kept here as its numbers: **966** active threads at
08:20Z, **968** at 09:09Z, **967** at 09:52Z.

Re-derived independently for this ticket at **2026-08-19 ~10:05Z**, via
`GET /guilds/{guild}/threads/active`:

| quantity | value |
|---|---|
| active threads, guild-wide | **971** |
| `has_more` | `false` (so 971 is the whole set, not a page) |
| parented to `#log` (`1525142015925555252`) | **971 of 971** |
| carrying `archived: true` | **0** |

The count did not fall. It rose, monotonically, across every measurement anyone
took, while the channel went from unreadable to readable. **The standing thread
count is not the variable.**

That alone eliminates two of the four candidate explanations:

- **(1) the new 60-minute idle archive drained the existing threads** — refuted.
- **(2) Discord's own 24h auto-archive drained them** — refuted.


## The 09:08Z startup line is TRUE, and now has evidence

The ticket flagged that if (1) were the answer, bridget's shipped startup line —
*"the backlog is archived separately"* — would be a false claim already in
production. It is not false. Splitting `auto_archive_duration` by creation era
gives a clean natural experiment across the 09:08Z deploy of mg-27e0:

| threads created | count | `auto_archive_duration` |
|---|---|---|
| before 2026-08-19 09:08Z (pre-mg-27e0 build) | **958** | `1440` — all of them |
| after 2026-08-19 09:08Z (post-mg-27e0 build) | **13** | `60` — all of them |

Zero pre-existing threads were re-stamped. The new build applied its shorter
timer only to threads it created, which is exactly what the line claims and
exactly what `conversations.py`'s schema-v3 note says it does. The line stands.


## What the count was never measuring

Two facts nobody had measured, and both matter for how much weight the 966
deserved in the first place:

- **`member_count` histogram: 791 threads have 1 member, 180 have 2.** The bot is
  a member of all 971 (the payload returns 971 bot memberships). So Daniel has
  joined at most 180 of them; 791 contain the bot and nobody else.
- **The whole active-thread payload is 791,339 bytes** — ~814 bytes per thread.
  Large, but not a number that obviously defeats a desktop client, and demonstrably
  not one that defeats it *now*.

The guild itself is small: **5 channels total** (2 categories, 2 text, 1 voice)
and **2 members**. Daniel's *"it says no text channels"* was therefore never a
report of guild state — there are two text channels, and there were throughout.
It was a client that had not populated what the server was offering it.


## One of the three symptoms was never a rendering failure at all

mg-27e0 bundled three of Daniel's reports as one fault. One of them decouples
cleanly:

> "the bot has a grey status icon so it looks not active but i did receive some
> updates"

The bot **was** not active. `bridget.err.log` shows it unable to resolve
discord.com for ~71 hours (below). A greyed-out bot presence during that window
is the status indicator working correctly — it is the one symptom with a
straightforward, fully-established cause, and it has nothing to do with how many
threads the channel holds. Reading it as evidence for the render theory was the
error; it was evidence for the outage.


## What actually happened: a burst, not a population

`bridget.log` is silent from **2026-08-16T07:26:41Z to 2026-08-19T06:54:30Z** —
71h28m. That silence is *not* bridget being down, and reading it that way is the
same adjacency error this ticket exists to correct. `bridget.err.log` shows the
process alive and failing the whole time:

```
[2026-08-16T06:41:09Z] deliver failed ... Cannot connect to host discord.com:443
                       ssl:default [nodename nor servname provided, or not known]
   ... 40,449 such lines; 2047 on 08-16, 2870 on 08-17, 2874 on 08-18 ...
[2026-08-19T06:48:43Z] deliver failed ... (last of the continuous run)
```

`nodename nor servname provided` is DNS. bridget was **up and unable to resolve
discord.com for ~71 hours** (2026-08-16T06:41:09Z → 2026-08-19T06:48:43Z), retrying
every 30s and queueing everything it could not deliver.

Connectivity returned at ~06:49Z, and the queue drained as a burst. Thread
creations that morning, from `thread_metadata.create_timestamp`:

```
06:49Z   23  #######################
06:50Z   27  ###########################
06:54Z   11  ###########
06:55Z    3  ###
06:56Z   27  ###########################
06:57Z    9  #########
06:59Z   22  ######################
07:00Z    2  ##
07:02Z    2  ##      <- rate has already collapsed
07:04Z    2  ##
07:09Z    2  ##
07:13Z    1  #
07:14Z    1  #
```

**122 threads in the 06:00Z hour; peak 27 in the 06:56Z minute.** The whole rest
of the day produced 36. bridget's own relay counter agrees: at 06:59:57Z it logged
`relay: 171 delivered in the last 262762s`.

The REST path recovered before the gateway did, which is why 50 threads exist at
06:49–06:50Z with no corresponding stdout lines — the websocket did not re-IDENTIFY
and print `logged in` until 06:54:30Z.

**Daniel reached this conclusion himself, unprompted, at 09:35:28Z** — twelve
minutes before this forensics ticket was filed, in reply to the mayor's third
request to authorise the archive:

> "The fleet was down for a couple days so probably what happened was the bridge
> came up and a bunch of automated alerts blew up the server. Another instance of
> needing better deduplication in the bridge I bet"

Every clause of that is confirmed by the measurements above. He was asked for
"go" or "no" and instead supplied the correct mechanism; the reply was read as a
non-answer to the archive question rather than as evidence, and the ticket that
followed listed four candidate explanations, none of them his.

His last clause is also supported: the 06:59:48Z log shows `dedup: suppressed
repeat #2/#3/#4 of [ack-watch] 'FLEET BLACKOUT — 26x fires delivered in the last
3h0m0s, NONE completed'`. Deduplication was running and suppressing repeats; the
distinct-alert flood still went through.


## Verdict on the four candidates

| # | explanation | verdict |
|---|---|---|
| 1 | 60-min idle archive drained existing threads | **refuted** — count rose; 958 pre-existing threads still carry 1440 |
| 2 | Discord's own 24h auto-archive drained them | **refuted** — same measurement; 0 archived |
| 3 | the client recovered independently, count materially unchanged | **supported, with a mechanism**: an arrival burst, not a population |
| 4 | something in the two bridget restarts | **refuted** — see below |

**(4) is refuted on mechanism, not just on adjacency.** The 09:08Z and 09:39Z
restarts created 13 threads between them and changed no guild-visible state: the
channel inventory is unchanged at 5, the bot holds no Manage Channels permission
(`403 Missing Permissions` for every `#agent` channel it tried to create, in
`bridget.err.log` at 06:54:32Z), and nothing was archived or deleted. A bot
process restarting is not observable to another user's client except through
guild state, and the only guild state that changed was threads being *added*.
The burst had already decayed to ≤2 threads/minute by 07:00Z — before either
restart.

So the fix was neither the bound nor the restarts. The load that broke the client
ended on its own at ~07:00Z when the backlog finished draining.


## What is still open, stated as such

**The burst decayed by ~07:00Z; Daniel reported readable at 09:45:20Z.** That is a
~2h45m gap this evidence does not close. Server-side data cannot see a client
reload, a cache rebuild, or when he next opened the app, and no measurement here
distinguishes those.

One suggestive but non-conclusive datum: Daniel was posting thread replies at
08:33:07Z, 09:32:27Z and 09:33:59Z — while the mayor's 09:09Z message asserted
"Your Discord is still broken". A notification can be clicked through to a thread
without the channel list rendering, so this does not prove the client was healthy
at 08:33Z. It does mean the assertion at 09:09Z was an assumption, and that
evidence bearing on it was arriving in the mayor's own inbound stream.

**Not investigated here** (out of scope, worth its own item): bridget has no
rate-limiting on *thread creation* per unit time, and no coalescing of a drained
backlog. That — not the standing population — is what this incident actually
exercised, and it is what Daniel asked for.


## What survives

The bound (mg-27e0) stays, and this document does not argue against it:

- 1150+ unbounded threads accumulating in one channel is a real liability whatever
  broke the client on 2026-08-19.
- `BRIDGET_THREAD_EVICTION_BATCH` limits how fast the set drains, which is the
  one part of mg-27e0 that acts on *rate*.

What must not survive is the claim that the bound **fixed this incident**, or that
a Discord client cannot render ~1000 threads in a channel. It rendered 971 while
this document was being written.
