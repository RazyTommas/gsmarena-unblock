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
    """Pull other people's messages. Rebase, because our commits are append-only
    files that can always replay cleanly on top of theirs."""
    try:
        sh("git", "fetch", "origin", "--quiet")
        sh("git", "pull", "--rebase", "--quiet", "origin", branch())
    except RuntimeError as e:
        print(f"  ! pull failed ({e.args[0].splitlines()[0]}) — working from local state",
              file=sys.stderr)


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

    n = sub.add_parser("new"); n.add_argument("id"); n.add_argument("--title", required=True)
    n.add_argument("--why", default=""); n.add_argument("--script")
    n.add_argument("--args", nargs="*"); n.add_argument("--control", default="")
    n.add_argument("--deliver", default="")
    n.set_defaults(fn=cmd_new)

    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
