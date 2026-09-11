#!/usr/bin/env python3
"""
relay.py — talk to the other box through the repo.

WHY THE REPO IS THE TRANSPORT
Two agents, two networks, no shared filesystem and no inbound port on either side.
What they DO share is push access to one git remote, so git is the message bus. It
gives us, for free, the three things a bus needs: durability, ordering (commit
history) and authentication (whoever can push is who they say they are).

CONFLICT-FREE BY CONSTRUCTION
Every message is its own file and no file is ever written by both sides:

    relay/tasks/<id>.json          written HERE   (the asking side)
    relay/claims/<id>.<agent>.json written THERE  (the doing side)
    relay/results/<id>/result.json written THERE
    relay/results/<id>/<data...>   written THERE

So a `git pull --rebase` can never hit a content conflict on relay traffic. That is
a design property, not luck: the moment two agents edit one shared index file, this
stops working.

WHAT THE REMOTE BOX IS FOR
It is not a chat partner, it is a COLLECTOR. It holds the one thing this box cannot
get — a different IP — and several of our sources are blocked here and only here:
samfw/fota-cloud (the Samsung patch levels that are the binding limit on the whole
project), gsmarena at volume, and possibly the Qualcomm bulletin API. It fetches and
returns raw data. This box keeps the corpus and does all adjudication, so nothing it
sends is ever trusted as a verdict.

THE RULES A RESULT MUST FOLLOW (learned the hard way, see DESIGN doc)
  * outcome is mandatory and is not a row count: ok | empty | blocked | refused | error.
    A fetch that returned nothing and a fetch that was blocked are different facts,
    and a count of 0 cannot tell them apart.
  * every task carries a CONTROL — a request we already know succeeds. If the control
    fails, the whole result is instrument failure, not a finding about the target.
    An empty result is only evidence when a non-empty result was possible.
  * never commit a credential. Say where it lives, never what it is.

IT IS `msg`, BUT THE BUS IS GIT
The verbs mirror ~/work/git/msg deliberately (send / inbox / read / reply / agents),
and the on-disk format is identical — markdown with YAML frontmatter under
msg/inbox/<agent>/ — so `ls` and `cat` work and nobody has to learn a second thing.

One honest difference. `msg read` is atomic: it MOVES the file on one filesystem, so
a shared mailbox hands each message to exactly one agent. Git has no equivalent. Two
boxes can both read while offline and both pushes succeed, because they touch
different paths. So a claim here is ADVISORY, not exclusive. With two agents that is
fine; past a handful it would need a real lock, and pretending otherwise is how you
get two agents doing the same job and neither noticing.

    python3 relay.py next              # what should I work on?
    python3 relay.py run <id>          # claim, execute, submit  (the usual path)
    python3 relay.py status            # what is open / claimed / done
    python3 relay.py submit <id> --outcome blocked --note "403 from cloudflare"
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CRAWLER = HERE.parent
REPO = CRAWLER.parent
TASKS, CLAIMS, RESULTS = HERE / "tasks", HERE / "claims", HERE / "results"
for d in (TASKS, CLAIMS, RESULTS):
    d.mkdir(parents=True, exist_ok=True)

AGENT = os.environ.get("RELAY_AGENT") or f"box-{os.uname().nodename}"
STALE_CLAIM_HOURS = 6


def sh(*args, cwd=REPO, check=True, capture=True):
    r = subprocess.run(args, cwd=str(cwd), check=False,
                       capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} -> {r.returncode}\n{r.stderr or r.stdout}")
    return (r.stdout or "").strip()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sync():
    """Pull other people's messages. Rebase, because our commits are append-only files
    that always replay cleanly on top of theirs.

    A rebase refuses outright if the working tree is dirty, and `git` reports that as a
    bare exit 128. Left as-is the agent sees "pull failed" and has no idea why or what
    to do — correct behaviour with no forward path. So name the offending files and the
    fix, and carry on with local state rather than dying: a stale read is recoverable,
    a crashed collector mid-harvest is not."""
    dirty = [l for l in sh("git", "status", "--porcelain", check=False).splitlines()
             if l.strip()]
    if dirty:
        print(f"  ! working tree has {len(dirty)} uncommitted change(s); skipping the "
              f"pull so nothing of yours is touched. Reading local state.", file=sys.stderr)
        for l in dirty[:5]:
            print(f"      {l}", file=sys.stderr)
        if len(dirty) > 5:
            print(f"      ... and {len(dirty)-5} more", file=sys.stderr)
        print("      fix: commit them, or `git stash`, then re-run.", file=sys.stderr)
        return False
    try:
        sh("git", "fetch", "origin", "--quiet")
        sh("git", "pull", "--rebase", "--quiet", "origin", branch())
        return True
    except RuntimeError as e:
        first = e.args[0].splitlines()
        detail = next((l for l in first if l.strip() and "->" not in l), first[0])
        print(f"  ! pull failed: {detail.strip()}", file=sys.stderr)
        print("      working from local state — results you push may need a manual rebase.",
              file=sys.stderr)
        return False


def branch():
    return sh("git", "rev-parse", "--abbrev-ref", "HEAD") or "main"


def push(paths, message, tries=4):
    """Commit ONLY the given relay paths and push, rebasing on contention. Scoped to
    relay/ so this can never sweep up unrelated working-tree changes."""
    rel = [str(Path(p).resolve().relative_to(REPO)) for p in paths]
    sh("git", "add", "--", *rel)
    if not sh("git", "diff", "--cached", "--name-only"):
        print("  (nothing to commit)")
        return True
    sh("git", "commit", "--quiet", "-m", message)
    for i in range(tries):
        try:
            sh("git", "push", "--quiet", "origin", branch())
            return True
        except RuntimeError:
            print(f"  push rejected, rebasing (attempt {i+1}/{tries})")
            try:
                sh("git", "pull", "--rebase", "--quiet", "origin", branch())
            except RuntimeError as e:
                print(f"  ! rebase failed: {e.args[0].splitlines()[0]}", file=sys.stderr)
                return False
            time.sleep(1 + i * 2)
    print("  ! could not push after retries — the commit is local, run `git push` by hand",
          file=sys.stderr)
    return False


def load(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def task_state(tid):
    """open | claimed-by-X | done. A claim older than STALE_CLAIM_HOURS is ignored so
    an agent that died mid-task does not park the work forever."""
    if (RESULTS / tid / "result.json").exists():
        return "done", None
    live = None
    for c in CLAIMS.glob(f"{tid}.*.json"):
        d = load(c) or {}
        ts = d.get("claimed_at", "")
        try:
            age = (time.time() - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))) / 3600
        except Exception:
            age = 0
        if age < STALE_CLAIM_HOURS:
            live = d.get("agent", "?")
    return ("claimed", live) if live else ("open", None)


def all_tasks():
    out = []
    for p in sorted(TASKS.glob("*.json")):
        t = load(p)
        if t:
            t["_path"] = p
            t["_state"], t["_by"] = task_state(t["id"])
            out.append(t)
    return out


def cmd_status(a):
    sync()
    ts = all_tasks()
    if not ts:
        print("no tasks")
        return 0
    print(f"{len(ts)} task(s) · agent={AGENT}\n")
    for t in ts:
        mark = {"open": "○", "claimed": "◐", "done": "●"}[t["_state"]]
        by = f" [{t['_by']}]" if t["_by"] else ""
        print(f"  {mark} {t['id']:28} {t['_state']:8}{by}  {t.get('title','')}")
        if t["_state"] == "done":
            r = load(RESULTS / t["id"] / "result.json") or {}
            print(f"      -> {r.get('outcome','?')}  {str(r.get('note',''))[:76]}")
    return 0


def cmd_next(a):
    sync()
    for t in all_tasks():
        if t["_state"] == "open":
            print(json.dumps({k: v for k, v in t.items() if not k.startswith("_")}, indent=2))
            return 0
    print("no open tasks")
    return 1


def cmd_claim(a):
    sync()
    state, by = task_state(a.id)
    if state == "done":
        print(f"{a.id} is already done"); return 1
    if state == "claimed" and by != AGENT and not a.force:
        print(f"{a.id} is claimed by {by} (use --force to take it)"); return 1
    p = CLAIMS / f"{a.id}.{re.sub(r'[^A-Za-z0-9_.-]', '_', AGENT)}.json"
    p.write_text(json.dumps({"id": a.id, "agent": AGENT, "claimed_at": now()}, indent=2))
    push([p], f"relay: {AGENT} claims {a.id}")
    print(f"claimed {a.id}")
    return 0


def cmd_submit(a):
    d = RESULTS / a.id
    d.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in (a.files or []):
        src = Path(f)
        if not src.exists():
            print(f"  ! {f} does not exist — not submitting a result that references it",
                  file=sys.stderr)
            return 2
        shutil.copy2(src, d / src.name)
        copied.append(src.name)
    res = {"id": a.id, "agent": AGENT, "finished_at": now(),
           "outcome": a.outcome, "note": a.note or "",
           "control_ok": (None if a.control is None else bool(a.control)),
           "files": copied, "counts": json.loads(a.counts) if a.counts else {}}
    (d / "result.json").write_text(json.dumps(res, indent=2))
    push([d], f"relay: {AGENT} result {a.id} = {a.outcome}")
    print(json.dumps(res, indent=2))
    return 0


def cmd_run(a):
    """Claim, execute the task's script, submit. The common path."""
    sync()
    t = load(TASKS / f"{a.id}.json")
    if not t:
        print(f"no such task: {a.id}"); return 1
    state, by = task_state(a.id)
    if state == "done" and not a.force:
        print(f"{a.id} already has a result (use --force to redo)"); return 1
    if state == "claimed" and by != AGENT and not a.force:
        print(f"{a.id} is claimed by {by} (use --force)"); return 1

    cmd_claim(argparse.Namespace(id=a.id, force=True))
    script = t.get("script")
    if not script:
        print("this task has no script — do it by hand, then `relay.py submit`")
        print(json.dumps(t, indent=2))
        return 0

    outdir = RESULTS / a.id
    outdir.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, str(CRAWLER / script)] + list(t.get("args") or [])
    argv = [x.replace("{OUT}", str(outdir)) for x in argv]
    print(f"$ {' '.join(argv)}\n")
    log = outdir / "run.log"
    with open(log, "w") as lf:
        p = subprocess.run(argv, cwd=str(CRAWLER), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True)
        lf.write(p.stdout or "")
    print(p.stdout or "")

    # The script is expected to drop its own result.json; if it did not, record the
    # exit code honestly rather than inventing an outcome.
    rp = outdir / "result.json"
    if not rp.exists():
        rp.write_text(json.dumps({
            "id": a.id, "agent": AGENT, "finished_at": now(),
            "outcome": "ok" if p.returncode == 0 else "error",
            "note": f"script exited {p.returncode}; it wrote no result.json of its own",
            "control_ok": None, "files": [], "counts": {}}, indent=2))
    else:
        r = load(rp) or {}
        r.setdefault("agent", AGENT); r.setdefault("finished_at", now())
        r["id"] = a.id
        rp.write_text(json.dumps(r, indent=2))
    push([outdir], f"relay: {AGENT} result {a.id}")
    print(f"\nsubmitted {a.id}: {(load(rp) or {}).get('outcome')}")
    return 0


def cmd_new(a):
    """Publish a task (run from the asking side)."""
    t = {"id": a.id, "title": a.title, "created_at": now(), "created_by": AGENT,
         "why": a.why or "", "script": a.script, "args": a.args or [],
         "control": a.control or "", "deliver": a.deliver or ""}
    p = TASKS / f"{a.id}.json"
    p.write_text(json.dumps(t, indent=2))
    push([p], f"relay: new task {a.id}")
    print(f"published {a.id}")
    return 0


def cmd_pull(a):
    """Asking side: fetch finished results into a local directory."""
    sync()
    n = 0
    for rp in sorted(RESULTS.glob("*/result.json")):
        r = load(rp) or {}
        print(f"● {r.get('id')}  {r.get('outcome')}  by {r.get('agent')}  "
              f"{r.get('finished_at')}")
        if r.get("note"):
            print(f"    {r['note'][:150]}")
        if r.get("counts"):
            print(f"    counts: {r['counts']}")
        if r.get("control_ok") is False:
            print("    ! CONTROL FAILED — treat every number here as instrument failure")
        for f in r.get("files") or []:
            print(f"    file: {rp.parent / f}")
        n += 1
    print(f"\n{n} result(s)")
    return 0



# ── the msg-compatible side: free-form mail, same format, git as the bus ──────
MSG = HERE / "msg"
MBOX, MARCH, AGENTS = MSG / "inbox", MSG / "archive", MSG / "agents"


def _slug(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(s))


def _msg_id():
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"-{os.getpid() % 1000:03d}"


def cmd_register(a):
    """Announce yourself. Optional for receiving, but it is how the other box knows
    you exist and what you are doing — and it is what makes a mistyped recipient get
    caught instead of silently creating a dead mailbox nobody ever reads."""
    sync()
    AGENTS.mkdir(parents=True, exist_ok=True)
    (MBOX / _slug(AGENT)).mkdir(parents=True, exist_ok=True)
    p = AGENTS / f"{_slug(AGENT)}.json"
    p.write_text(json.dumps({"agent": AGENT, "role": a.role or "",
                             "host": os.uname().nodename, "seen": now()}, indent=2))
    keep = MBOX / _slug(AGENT) / ".keep"
    keep.write_text("")
    push([p, keep], f"relay: register {AGENT}")
    n = len(list((MBOX / _slug(AGENT)).glob("*.md")))
    print(f"registered {AGENT}" + (f" — {n} message(s) waiting" if n else " — no mail"))
    return 0


def cmd_agents(a):
    sync()
    rows = []
    for p in sorted(AGENTS.glob("*.json")):
        d = load(p) or {}
        box = MBOX / _slug(d.get("agent", ""))
        rows.append((d.get("agent", "?"), d.get("seen", "?"), d.get("role", ""),
                     len(list(box.glob("*.md"))) if box.exists() else 0))
    # a mailbox with no registration is exactly the dead-letter case worth surfacing
    known = {r[0] for r in rows}
    for box in sorted(MBOX.glob("*")):
        if box.is_dir() and box.name not in {_slug(k) for k in known}:
            rows.append((box.name + "  (never registered)", "-", "", 
                         len(list(box.glob("*.md")))))
    if not rows:
        print("no agents registered yet")
        return 0
    for name, seen, role, unread in rows:
        flag = f"  {unread} unread" if unread else ""
        print(f"  {name:28} last seen {seen}{flag}")
        if role:
            print(f"      {role}")
    return 0


def cmd_send(a):
    sync()
    to = _slug(a.to)
    box = MBOX / to
    registered = (AGENTS / f"{to}.json").exists() or box.exists()
    if not registered and not a.force:
        # Delivering to a mailbox nobody owns is how messages die silently. Refuse,
        # and show who does exist, rather than creating a dead letter box.
        who = [p.stem for p in AGENTS.glob("*.json")] or ["(nobody registered yet)"]
        print(f"refusing to create a mailbox for unknown agent {a.to!r}.")
        print(f"known: {', '.join(who)}   — use --force if the name is right")
        return 1
    box.mkdir(parents=True, exist_ok=True)
    mid = _msg_id()
    p = box / f"{mid}-from-{_slug(AGENT)}.md"
    body = a.message
    if a.file:
        body = (body + "\n\n" if body else "") + Path(a.file).read_text()
    p.write_text(f"---\nid: {mid}\nfrom: {AGENT}\nto: {a.to}\n"
                 f"sent: {now()}\nsubject: {a.subject}\n"
                 + (f"re: {a.re}\n" if a.re else "") + "---\n\n" + (body or "") + "\n")
    push([p], f"relay: msg {AGENT} -> {a.to}: {a.subject[:50]}")
    print(f"sent {mid} to {a.to}")
    return 0


def cmd_reply(a):
    src = None
    for d in (MBOX, MARCH):
        for p in d.rglob(f"{a.id}*.md"):
            src = p; break
        if src:
            break
    if not src:
        print(f"no message {a.id}"); return 1
    head = src.read_text().split("---")[1] if "---" in src.read_text() else ""
    frm = next((l.split(":", 1)[1].strip() for l in head.splitlines()
                if l.startswith("from:")), None)
    subj = next((l.split(":", 1)[1].strip() for l in head.splitlines()
                 if l.startswith("subject:")), "")
    if not frm:
        print("could not read the sender from that message"); return 1
    return cmd_send(argparse.Namespace(to=frm, subject=f"Re: {subj}", message=a.message,
                                       file=None, re=a.id, force=True))


def _my_box():
    return MBOX / _slug(AGENT)


def cmd_inbox(a):
    sync()
    box = _my_box()
    ms = sorted(box.glob("*.md")) if box.exists() else []
    if not ms:
        print(f"no mail for {AGENT}")
        return 0
    print(f"{len(ms)} message(s) for {AGENT}:")
    for p in ms:
        t = p.read_text()
        head = t.split("---")[1] if "---" in t else ""
        g = lambda k: next((l.split(":", 1)[1].strip() for l in head.splitlines()
                            if l.startswith(k + ":")), "?")
        print(f"  {g('id'):22} from {g('from'):20} {g('subject')[:60]}")
    return 0


def cmd_read(a):
    """Print the oldest unread and archive it. NOTE: over git this is not an atomic
    claim — see the module docstring. It is a receipt, not a lock."""
    sync()
    box = _my_box()
    ms = sorted(box.glob("*.md")) if box.exists() else []
    if a.id:
        ms = [p for p in ms if p.name.startswith(a.id)]
    if not ms:
        print(f"no mail for {AGENT}")
        return 1
    p = ms[0]
    print(p.read_text())
    if a.peek:
        return 0
    dest = MARCH / _slug(AGENT)
    dest.mkdir(parents=True, exist_ok=True)
    p.rename(dest / p.name)
    push([p, dest / p.name], f"relay: {AGENT} read {p.name}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("next").set_defaults(fn=cmd_next)
    sub.add_parser("pull").set_defaults(fn=cmd_pull)

    c = sub.add_parser("claim"); c.add_argument("id"); c.add_argument("--force", action="store_true")
    c.set_defaults(fn=cmd_claim)

    r = sub.add_parser("run"); r.add_argument("id"); r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("submit"); s.add_argument("id")
    s.add_argument("--outcome", required=True,
                   choices=["ok", "empty", "blocked", "refused", "error"])
    s.add_argument("--note", default="")
    s.add_argument("--counts", help='JSON, e.g. {"rows":412}')
    s.add_argument("--control", type=int, choices=[0, 1],
                   help="did the task's control request succeed?")
    s.add_argument("--files", nargs="*")
    s.set_defaults(fn=cmd_submit)

    g = sub.add_parser("register"); g.add_argument("-r", "--role", default="")
    g.set_defaults(fn=cmd_register)
    sub.add_parser("agents").set_defaults(fn=cmd_agents)
    sub.add_parser("inbox").set_defaults(fn=cmd_inbox)

    rd = sub.add_parser("read"); rd.add_argument("id", nargs="?")
    rd.add_argument("--peek", action="store_true", help="print without archiving")
    rd.set_defaults(fn=cmd_read)

    sd = sub.add_parser("send"); sd.add_argument("to")
    sd.add_argument("-s", "--subject", required=True)
    sd.add_argument("-m", "--message", default="")
    sd.add_argument("--file", help="append a file as the body")
    sd.add_argument("--re", help="id this is a reply to")
    sd.add_argument("--force", action="store_true", help="send to an unregistered name")
    sd.set_defaults(fn=cmd_send)

    rp = sub.add_parser("reply"); rp.add_argument("id")
    rp.add_argument("-m", "--message", required=True); rp.set_defaults(fn=cmd_reply)

    n = sub.add_parser("new"); n.add_argument("id"); n.add_argument("--title", required=True)
    n.add_argument("--why", default=""); n.add_argument("--script")
    n.add_argument("--args", nargs="*"); n.add_argument("--control", default="")
    n.add_argument("--deliver", default="")
    n.set_defaults(fn=cmd_new)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
