# relay — two agents, two networks, one repo

There is no shared filesystem and no inbound port on either box. What they share is
push access to this repo, so **git is the message bus**: durable, ordered, and
authenticated by whoever can push.

## Conflict-free by construction

Every message is its own file, and **no file is ever written by both sides**:

```
relay/tasks/<id>.json            written by the ASKING box
relay/claims/<id>.<agent>.json   written by the DOING box
relay/results/<id>/result.json   written by the DOING box
relay/results/<id>/<data…>       written by the DOING box
```

`git pull --rebase` can therefore never hit a content conflict on relay traffic.
That is a design property, not luck — the moment two agents edit one shared index
file, this stops working.

## Who does what

The remote box is a **collector**, not a second source of truth. It has the one thing
the asking box cannot get — a different IP — and it returns flat CSVs. It never grows
a corpus of its own, because two databases drifting apart and merged later by hand is
how a corpus acquires contradictions nobody can date.

| | asking box | remote box |
|---|---|---|
| holds `devices.db` | yes | never |
| runs the crawlers that work | yes | — |
| runs the crawlers that are 403 there | no | **yes** |
| ingests, derives, adjudicates | yes | never |
| writes a verdict | yes | never |

## Two rules a result must follow

**1. `outcome` is mandatory, and it is not a row count.**
`ok · empty · blocked · refused · error`. A fetch that returned nothing and a fetch
that was refused are different facts, and a count of `0` cannot tell them apart. This
is not theoretical: `samsung.py` used to delete its rows before fetching, so on the
blocked box every run erased the corpus and logged a green success.

**2. Every task carries a control** — a request already known to succeed. If the
control fails, the whole result is an instrument reading, not a finding about the
target, and `ingest_relay.py` discards it loudly. An empty result is only evidence
when a non-empty result was possible.

Never commit a credential. Say where it lives, never what it is.

## It is `msg`, but the bus is git

The verbs mirror `~/work/git/msg` on purpose, and the on-disk format is identical —
markdown with YAML frontmatter under `msg/inbox/<agent>/` — so `ls` and `cat` work and
nobody learns a second thing.

```bash
python3 relay.py register -r "what you are working on"
python3 relay.py agents            # who exists, and their unread counts
python3 relay.py send box-home -s "subject" -m "body"
python3 relay.py inbox
python3 relay.py read              # oldest unread: prints it and archives it
python3 relay.py read --peek       # look without archiving
python3 relay.py reply <id> -m "..."
```

Sending to an unregistered name is **refused**, with the known names listed. Delivering
to a mailbox nobody owns is how messages die silently; `--force` is there for when the
name really is right.

**One honest difference from `msg`.** `msg read` is atomic — it *moves* the file on one
filesystem, so a shared mailbox hands each message to exactly one agent. Git has no
equivalent: two boxes can both read while offline and both pushes succeed, because they
touch different paths. A claim here is **advisory, not exclusive**. With two agents that
is fine; past a handful it needs a real lock, and pretending otherwise is how you get two
agents doing the same job and neither noticing.

## Commands

On the remote box:

```bash
export RELAY_AGENT=box-<somewhere>    # a name that says where it is
python3 relay.py status               # what is open
python3 relay.py run T001-probe       # claim, execute, push the result
```

On the asking box:

```bash
python3 relay.py pull                 # see what came back
python3 ingest_relay.py --list        # inspect before ingesting
python3 ingest_relay.py --task T002-samsung-fota
python3 derive.py && python3 vuln.py --build && python3 platform_vuln.py --build
```

A claim older than 6 hours is ignored, so an agent that dies mid-task does not park
the work forever.
