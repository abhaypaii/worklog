#!/usr/bin/env python3
"""
worklog — a local, lifetime work journal in a single SQLite file.

Pure Python stdlib. No dependencies. Your data lives in ~/.worklog/worklog.db
(or wherever WORKLOG_DB points).

Capture:
    worklog log "Shipped the caching fix on Atlas, 40% faster. Helped Priya debug the ETL."
    worklog log --date 2026-07-08 "Talked to boss about next quarter's product bets."

Career tracking:
    worklog company add "Acme AI" --start 2026-01-05
    worklog company end "Acme AI" --date 2027-03-31
    worklog role add "ML Engineer" --start 2026-01-05
    worklog promote "Senior ML Engineer" --date 2027-04-01
    worklog career                      # show full company/role timeline

Browse & report:
    worklog list --since 2026-01-01 --until 2026-06-30
    worklog list --grep caching
    worklog export --since 2026-04-01 --until 2026-06-30 > q2_review.md

AI (optional, needs Ollama running locally):
    worklog triage                      # extract structured events from raw entries (qwen)
    worklog embed                       # embed anything un-embedded (nomic-embed-text)
    worklog search "times I improved performance"
    worklog search "led migrations" --top 10

Everything degrades gracefully: without Ollama you still have full logging,
career tracking, keyword search, and markdown export.
"""

import argparse
import datetime as dt

import json
import math
import os
import sqlite3
import sys
import urllib.request

DB_PATH = os.environ.get(
    "WORKLOG_DB",
    os.path.join(os.path.expanduser("~"), ".worklog", "worklog.db"),
)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("WORKLOG_EMBED_MODEL", "nomic-embed-text:v1.5")
CHAT_MODEL = os.environ.get("WORKLOG_CHAT_MODEL", "qwen3:4b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    started_at TEXT,
    ended_at TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    title TEXT NOT NULL,
    level TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS raw_entries (
    id INTEGER PRIMARY KEY,
    logged_at TEXT NOT NULL,
    entry_date TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    company_id INTEGER REFERENCES companies(id),
    role_id INTEGER REFERENCES roles(id),
    processed INTEGER NOT NULL DEFAULT 0,
    embedding TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    raw_entry_id INTEGER NOT NULL REFERENCES raw_entries(id),
    event_date TEXT NOT NULL,
    company_id INTEGER REFERENCES companies(id),
    role_id INTEGER REFERENCES roles(id),
    type TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail TEXT,
    impact TEXT,
    project TEXT,
    people TEXT,           -- JSON array of names
    skills TEXT,           -- JSON array of tags
    resume_worthy INTEGER NOT NULL DEFAULT 0,
    embedding TEXT         -- JSON array of floats
);
"""

TRIAGE_PROMPT = """You are an extraction engine for a personal work journal.
Split the diary entry below into distinct work events. Output ONLY a JSON array,
no prose, no markdown fences. Each element:
{"type": one of ["win","collaboration","decision","blocker","learning","routine",
"feedback","mentoring","recognition","incident","idea"],
"summary": one sentence in achievement-oriented resume voice starting with a strong verb,
"detail": fuller restatement preserving specifics,
"impact": stated outcome/metric or null (NEVER invent metrics),
"project": project name mentioned or null,
"people": array of names mentioned,
"skills": 1-4 short kebab-case skill tags,
"resume_worthy": true/false (be generous: quiet collaboration wins count)}
Never invent details. If nothing extractable, return [].

ENTRY (<<DATE>>):
<<TEXT>>
"""


# ---------- db helpers ----------

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def current_company(conn):
    return conn.execute(
        "SELECT * FROM companies WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


def current_role(conn):
    return conn.execute(
        "SELECT * FROM roles WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


def today():
    return dt.date.today().isoformat()


def parse_date(s, fallback=None):
    if not s:
        return fallback or today()
    dt.date.fromisoformat(s)  # validates
    return s


# ---------- ollama helpers ----------

def ollama(path, payload):
    req = urllib.request.Request(
        f"{OLLAMA_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error", "")
        except Exception:
            detail = ""
        if "not found" in detail.lower() or e.code == 404:
            sys.exit(f"Ollama error at {path}: {detail or 'not found'}\n"
                     f"If the model is missing, run:  ollama pull "
                     f"{payload.get('model', '')}")
        sys.exit(f"Ollama error {e.code} at {path}: {detail}")


def embed_text(text):
    # Newer Ollama: /api/embed (input -> embeddings[]); older: /api/embeddings
    try:
        out = ollama("/api/embed", {"model": EMBED_MODEL, "input": text})
        return out["embeddings"][0]
    except SystemExit:
        raise
    except Exception:
        out = ollama("/api/embeddings", {"model": EMBED_MODEL, "prompt": text})
        return out["embedding"]


def chat(prompt):
    out = ollama("/api/generate", {"model": CHAT_MODEL, "prompt": prompt,
                                   "stream": False, "format": "json", 'think':False,
                                   "options": {"temperature": 0.2}})
    return out["response"]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def need_ollama():
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3)
    except Exception:
        sys.exit(f"Cannot reach Ollama at {OLLAMA_URL}. Start it with `ollama serve` "
                 f"(and `ollama pull {EMBED_MODEL}` / `ollama pull {CHAT_MODEL}` once).")


# ---------- commands ----------

def cmd_log(args):
    conn = db()
    comp, role = current_company(conn), current_role(conn)
    entry_date = parse_date(args.date)
    conn.execute(
        "INSERT INTO raw_entries (logged_at, entry_date, raw_text, company_id, role_id) "
        "VALUES (?,?,?,?,?)",
        (dt.datetime.now().isoformat(timespec="seconds"), entry_date,
         args.text, comp["id"] if comp else None, role["id"] if role else None),
    )
    conn.commit()
    where = f" @ {comp['name']}" if comp else ""
    print(f"Logged for {entry_date}{where}.")
    if not comp:
        print("Tip: no current company set — `worklog company add \"Name\" --start YYYY-MM-DD`")


def cmd_company(args):
    conn = db()
    if args.action == "add":
        prev = current_company(conn)
        start = parse_date(args.start)
        if prev and not prev["ended_at"]:
            conn.execute("UPDATE companies SET ended_at=? WHERE id=?",
                         (start, prev["id"]))
            conn.execute("UPDATE roles SET ended_at=? WHERE ended_at IS NULL", (start,))
            print(f"Closed {prev['name']} (ended {start}).")
        conn.execute("INSERT INTO companies (name, started_at) VALUES (?,?)",
                     (args.name, start))
        print(f"Now at {args.name} (since {start}). Add a role: "
              f"`worklog role add \"Title\" --start {start}`")
    elif args.action == "end":
        end = parse_date(args.date)
        cur = conn.execute("UPDATE companies SET ended_at=? WHERE name=? AND ended_at IS NULL",
                           (end, args.name))
        conn.execute("UPDATE roles SET ended_at=? WHERE ended_at IS NULL", (end,))
        print(f"Ended {args.name} on {end}." if cur.rowcount else
              f"No open company named {args.name!r}.")
    conn.commit()


def cmd_role(args):
    conn = db()
    comp = current_company(conn)
    if not comp:
        sys.exit("Add a company first: `worklog company add \"Name\" --start YYYY-MM-DD`")
    start = parse_date(args.start)
    prev = current_role(conn)
    if prev:
        conn.execute("UPDATE roles SET ended_at=? WHERE id=?", (start, prev["id"]))
    conn.execute("INSERT INTO roles (company_id, title, level, started_at) VALUES (?,?,?,?)",
                 (comp["id"], args.title, args.level, start))
    conn.commit()
    print(f"Role: {args.title} @ {comp['name']} (from {start}).")


def cmd_promote(args):
    # sugar: same as role add — closes current role, opens new one, same company
    args.start = args.date
    args.level = args.level
    args.title = args.title
    cmd_role(args)
    print("Promotion recorded. 🎉")


def cmd_career(args):
    conn = db()
    rows = conn.execute("""
        SELECT c.name, c.started_at cs, c.ended_at ce, r.title, r.level,
               r.started_at rs, r.ended_at re
        FROM companies c LEFT JOIN roles r ON r.company_id = c.id
        ORDER BY c.started_at, r.started_at""").fetchall()
    if not rows:
        print("No career history yet.")
        return
    last = None
    for r in rows:
        if r["name"] != last:
            span = f"{r['cs'] or '?'} → {r['ce'] or 'present'}"
            print(f"\n{r['name']}  ({span})")
            last = r["name"]
        if r["title"]:
            lvl = f" [{r['level']}]" if r["level"] else ""
            print(f"  • {r['title']}{lvl}: {r['rs']} → {r['re'] or 'present'}")


def _entry_filter(args, alias="entry_date"):
    clauses, params = [], []
    if getattr(args, "since", None):
        clauses.append(f"{alias} >= ?"); params.append(args.since)
    if getattr(args, "until", None):
        clauses.append(f"{alias} <= ?"); params.append(args.until)
    return (" AND ".join(clauses) or "1=1"), params


def cmd_list(args):
    conn = db()
    where, params = _entry_filter(args)
    if args.grep:
        where += " AND raw_text LIKE ?"; params.append(f"%{args.grep}%")
    rows = conn.execute(
        f"""SELECT e.*, c.name company FROM raw_entries e
            LEFT JOIN companies c ON c.id = e.company_id
            WHERE {where} ORDER BY entry_date DESC LIMIT ?""",
        (*params, args.limit)).fetchall()
    for r in rows:
        comp = f" [{r['company']}]" if r["company"] else ""
        flag = "" if r["processed"] else " *"
        print(f"{r['entry_date']}{comp}{flag}  {r['raw_text']}")
    if any(not r["processed"] for r in rows):
        print("\n(* = not yet triaged — run `worklog triage`)")


def cmd_triage(args):
    need_ollama()
    conn = db()
    rows = conn.execute(
        "SELECT * FROM raw_entries WHERE processed=0 ORDER BY id LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("Nothing to triage.")
        return
    for r in rows:
        prompt = (TRIAGE_PROMPT
                  .replace("<<DATE>>", r["entry_date"])
                  .replace("<<TEXT>>", r["raw_text"]))
        try:
            events = json.loads(chat(prompt))
            if isinstance(events, dict):     # some models wrap: {"events": [...]}
                events = events.get("events", [])
        except Exception as e:
            print(f"  entry {r['id']}: extraction failed ({e}) — skipped, will retry next run")
            continue
        for ev in events:
            conn.execute(
                """INSERT INTO events (raw_entry_id, event_date, company_id, role_id,
                   type, summary, detail, impact, project, people, skills, resume_worthy)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["id"], r["entry_date"], r["company_id"], r["role_id"],
                 str(ev.get("type", "routine")), str(ev.get("summary", ""))[:500],
                 ev.get("detail"), ev.get("impact"), ev.get("project"),
                 json.dumps(ev.get("people", [])), json.dumps(ev.get("skills", [])),
                 1 if ev.get("resume_worthy") else 0))
        conn.execute("UPDATE raw_entries SET processed=1 WHERE id=?", (r["id"],))
        conn.commit()
        print(f"  entry {r['id']}: {len(events)} event(s) extracted")
    print("Done. Run `worklog embed` to make them searchable.")


def cmd_embed(args):
    need_ollama()
    conn = db()
    ev = conn.execute("SELECT id, summary, detail FROM events WHERE embedding IS NULL").fetchall()
    raw = conn.execute("SELECT id, raw_text FROM raw_entries WHERE embedding IS NULL").fetchall()
    for r in ev:
        text = f"{r['summary']} {r['detail'] or ''}"
        conn.execute("UPDATE events SET embedding=? WHERE id=?",
                     (json.dumps(embed_text(text)), r["id"]))
    for r in raw:
        conn.execute("UPDATE raw_entries SET embedding=? WHERE id=?",
                     (json.dumps(embed_text(r["raw_text"])), r["id"]))
    conn.commit()
    print(f"Embedded {len(ev)} event(s) and {len(raw)} raw entr(ies).")


def cmd_search(args):
    need_ollama()
    conn = db()
    q = embed_text(args.query)
    rows = conn.execute(
        """SELECT e.event_date, e.type, e.summary, e.impact, e.embedding, c.name company
           FROM events e LEFT JOIN companies c ON c.id=e.company_id
           WHERE e.embedding IS NOT NULL""").fetchall()
    source = "events"
    if not rows:  # fall back to raw entries
        rows = conn.execute(
            """SELECT r.entry_date event_date, 'raw' type, r.raw_text summary,
                      NULL impact, r.embedding, c.name company
               FROM raw_entries r LEFT JOIN companies c ON c.id=r.company_id
               WHERE r.embedding IS NOT NULL""").fetchall()
        source = "raw entries"
    if not rows:
        sys.exit("Nothing embedded yet — run `worklog embed` first.")
    scored = sorted(
        ((cosine(q, json.loads(r["embedding"])), r) for r in rows),
        key=lambda t: -t[0])[: args.top]
    print(f"(searching {len(rows)} {source})\n")
    for score, r in scored:
        comp = f" [{r['company']}]" if r["company"] else ""
        impact = f"  → {r['impact']}" if r["impact"] else ""
        print(f"{score:.2f}  {r['event_date']}{comp} ({r['type']}) {r['summary']}{impact}")


def cmd_export(args):
    conn = db()
    where, params = _entry_filter(args, alias="e.event_date")
    rows = conn.execute(
        f"""SELECT e.*, c.name company, r.title role FROM events e
            LEFT JOIN companies c ON c.id=e.company_id
            LEFT JOIN roles r ON r.id=e.role_id
            WHERE {where} ORDER BY e.type, e.event_date""", params).fetchall()
    if not rows:
        print("No triaged events in range (run `worklog triage`?). "
              "Falling back to raw entries:\n", file=sys.stderr)
        where, params = _entry_filter(args)
        for r in conn.execute(
                f"SELECT * FROM raw_entries WHERE {where} ORDER BY entry_date", params):
            print(f"- **{r['entry_date']}** — {r['raw_text']}")
        return
    print(f"# Work log export ({args.since or 'start'} → {args.until or 'now'})\n")
    cur_type = None
    for r in rows:
        if r["type"] != cur_type:
            cur_type = r["type"]
            print(f"\n## {cur_type.replace('_', ' ').title()}\n")
        star = " ⭐" if r["resume_worthy"] else ""
        impact = f" — *{r['impact']}*" if r["impact"] else ""
        proj = f" ({r['project']})" if r["project"] else ""
        print(f"- **{r['event_date']}**{proj} {r['summary']}{impact}{star}")


def cmd_status(args):
    conn = db()
    comp, role = current_company(conn), current_role(conn)
    n_raw = conn.execute("SELECT COUNT(*) c FROM raw_entries").fetchone()["c"]
    n_un = conn.execute("SELECT COUNT(*) c FROM raw_entries WHERE processed=0").fetchone()["c"]
    n_ev = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    n_emb = conn.execute("SELECT COUNT(*) c FROM events WHERE embedding IS NOT NULL").fetchone()["c"]
    print(f"DB: {DB_PATH}")
    print(f"Company: {comp['name'] if comp else '(none set)'}   "
          f"Role: {role['title'] if role else '(none set)'}")
    print(f"Raw entries: {n_raw} ({n_un} untriaged)   Events: {n_ev} ({n_emb} embedded)")


# ---------- cli ----------

def main():
    p = argparse.ArgumentParser(prog="worklog", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("log", help="log a free-text entry")
    s.add_argument("text")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("company", help="add/end a company")
    s.add_argument("action", choices=["add", "end"])
    s.add_argument("name")
    s.add_argument("--start", help="YYYY-MM-DD (for add)")
    s.add_argument("--date", help="YYYY-MM-DD (for end)")
    s.set_defaults(fn=cmd_company)

    s = sub.add_parser("role", help="add a role at the current company")
    s.add_argument("action", choices=["add"])
    s.add_argument("title")
    s.add_argument("--level")
    s.add_argument("--start", help="YYYY-MM-DD")
    s.set_defaults(fn=cmd_role)

    s = sub.add_parser("promote", help="record a promotion (closes current role)")
    s.add_argument("title")
    s.add_argument("--level")
    s.add_argument("--date", help="YYYY-MM-DD (default today)")
    s.set_defaults(fn=cmd_promote)

    s = sub.add_parser("career", help="show company/role timeline")
    s.set_defaults(fn=cmd_career)

    s = sub.add_parser("list", help="list raw entries")
    s.add_argument("--since"); s.add_argument("--until")
    s.add_argument("--grep"); s.add_argument("--limit", type=int, default=30)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("triage", help="AI-extract events from raw entries (Ollama)")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_triage)

    s = sub.add_parser("embed", help="embed events/entries (Ollama)")
    s.set_defaults(fn=cmd_embed)

    s = sub.add_parser("search", help="semantic search (Ollama)")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=5)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("export", help="markdown report of events")
    s.add_argument("--since"); s.add_argument("--until")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("status", help="db + career summary")
    s.set_defaults(fn=cmd_status)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()