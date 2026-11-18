"""
Trio Hub — a tiny shared-workspace MCP server so several AI agents
(e.g. Claude, Muse, Duet) can coordinate: a message board, a task board,
and shared notes. State lives in one SQLite file, so it works whether every
agent spawns its own stdio copy or they all connect to one HTTP server.

Run:
  python server.py                 # stdio (each client launches its own copy)
  python server.py --http          # one shared server at http://127.0.0.1:8765/mcp
  TRIO_DB=/path/hub.db python server.py ...   # pick where state is stored
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from contextlib import contextmanager

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
    _V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    _V2 = False

DB_PATH = os.environ.get("TRIO_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub.db"))
# If a Postgres URL is set (e.g. Neon on Vercel), use it instead of the SQLite file.
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY, role TEXT, joined DOUBLE PRECISION, last_seen DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS messages (
  id {serial}, sender TEXT NOT NULL, recipient TEXT,
  thread TEXT, content TEXT NOT NULL, ts DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS cursors (
  agent TEXT PRIMARY KEY, last_read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tasks (
  id {serial}, title TEXT NOT NULL, description TEXT,
  status TEXT NOT NULL DEFAULT 'open', assignee TEXT, created_by TEXT,
  created DOUBLE PRECISION, updated DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS task_log (
  id {serial}, task_id INTEGER, agent TEXT, note TEXT, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS notes (
  key TEXT PRIMARY KEY, content TEXT, updated_by TEXT, updated DOUBLE PRECISION);
"""
SCHEMA = _SCHEMA.format(serial="SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT")

STATUSES = ("open", "in_progress", "blocked", "review", "done")


class _PGConn:
    """Tiny adapter so the same '?'-style SQL runs on Postgres."""

    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self._c = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False)

    def execute(self, sql, args=()):
        return self._c.execute(sql.replace("?", "%s"), args)

    def executescript(self, sql):
        for stmt in filter(str.strip, sql.split(";")):
            self._c.execute(stmt)

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()


@contextmanager
def db():
    if PG:
        conn = _PGConn()
    else:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


with db() as _c:
    _c.executescript(SCHEMA)


def _touch(conn, agent: str):
    now = time.time()
    conn.execute(
        "INSERT INTO agents(name, role, joined, last_seen) VALUES(?, '', ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
        (agent, now, now),
    )


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago"


def _fmt_msg(r) -> str:
    to = f" → {r['recipient']}" if r["recipient"] else " → everyone"
    th = f" [#{r['thread']}]" if r["thread"] else ""
    return f"#{r['id']} {r['sender']}{to}{th} ({_ago(r['ts'])}): {r['content']}"


def _fmt_task(r) -> str:
    who = r["assignee"] or "unassigned"
    return f"T{r['id']} [{r['status']}] {r['title']} — {who} (by {r['created_by']}, updated {_ago(r['updated'])})"


mcp = _Server(
    "trio-hub",
    instructions=(
        "Shared workspace for a team of AI agents (e.g. Claude, Muse, Duet). "
        "Always pass your own agent name. Start with `join`, then `inbox` to catch up. "
        "Use `post` to talk, tasks to split work, notes for shared artifacts/decisions. "
        "Check `inbox` between steps so you see what teammates said."
    ),
)


# ---------- presence ----------
@mcp.tool()
def join(agent: str, role: str = "") -> str:
    """Register yourself on the hub (e.g. agent='Claude', role='coder'). Returns who else is here."""
    now = time.time()
    with db() as c:
        c.execute(
            "INSERT INTO agents(name, role, joined, last_seen) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET role=COALESCE(NULLIF(excluded.role,''), agents.role), last_seen=excluded.last_seen",
            (agent, role, now, now),
        )
    return f"Welcome, {agent}.\n" + who()


@mcp.tool()
def who() -> str:
    """List every agent on the hub, their role, and when they were last active."""
    with db() as c:
        rows = c.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
    if not rows:
        return "Nobody has joined yet."
    return "\n".join(f"- {r['name']}{' (' + r['role'] + ')' if r['role'] else ''}: last seen {_ago(r['last_seen'])}" for r in rows)


# ---------- messages ----------
@mcp.tool()
def post(agent: str, content: str, to: str = "", thread: str = "") -> str:
    """Send a message. Leave `to` empty to broadcast to everyone, or name one agent. `thread` optionally groups a topic."""
    with db() as c:
        _touch(c, agent)
        mid = c.execute(
            "INSERT INTO messages(sender, recipient, thread, content, ts) VALUES(?,?,?,?,?) RETURNING id",
            (agent, to or None, thread or None, content, time.time()),
        ).fetchone()["id"]
    return f"Posted message #{mid}."


@mcp.tool()
def inbox(agent: str, include_seen: bool = False, limit: int = 50) -> str:
    """Read messages for you (broadcasts + direct messages to you, excluding your own). Marks them as read."""
    with db() as c:
        _touch(c, agent)
        row = c.execute("SELECT last_read FROM cursors WHERE agent=?", (agent,)).fetchone()
        last = 0 if (include_seen or not row) else row["last_read"]
        rows = c.execute(
            "SELECT * FROM messages WHERE id > ? AND sender != ? AND (recipient IS NULL OR recipient = ?) "
            "ORDER BY id DESC LIMIT ?",
            (last, agent, agent, limit),
        ).fetchall()[::-1]
        if rows:
            c.execute(
                "INSERT INTO cursors(agent, last_read) VALUES(?, ?) "
                "ON CONFLICT(agent) DO UPDATE SET last_read=CASE WHEN excluded.last_read > cursors.last_read THEN excluded.last_read ELSE cursors.last_read END",
                (agent, rows[-1]["id"]),
            )
    return "\n".join(_fmt_msg(r) for r in rows) if rows else "No new messages."


@mcp.tool()
def history(thread: str = "", limit: int = 30) -> str:
    """Show the full recent conversation (all senders/recipients), optionally only one thread."""
    with db() as c:
        if thread:
            rows = c.execute("SELECT * FROM messages WHERE thread=? ORDER BY id DESC LIMIT ?", (thread, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return "\n".join(_fmt_msg(r) for r in rows[::-1]) or "No messages yet."


# ---------- tasks ----------
@mcp.tool()
def task_create(agent: str, title: str, description: str = "", assignee: str = "") -> str:
    """Create a task on the shared board. Optionally assign it to an agent."""
    now = time.time()
    with db() as c:
        _touch(c, agent)
        tid = c.execute(
            "INSERT INTO tasks(title, description, assignee, created_by, created, updated) VALUES(?,?,?,?,?,?) RETURNING id",
            (title, description, assignee or None, agent, now, now),
        ).fetchone()["id"]
        c.execute("INSERT INTO task_log(task_id, agent, note, ts) VALUES(?,?,?,?)", (tid, agent, "created", now))
        if assignee:
            c.execute(
                "INSERT INTO messages(sender, recipient, content, ts) VALUES(?,?,?,?)",
                (agent, assignee, f"Assigned you task T{tid}: {title}", now),
            )
    return f"Created T{tid}."


@mcp.tool()
def task_list(status: str = "", assignee: str = "") -> str:
    """List tasks, optionally filtered by status (open/in_progress/blocked/review/done) and/or assignee."""
    q, args = "SELECT * FROM tasks WHERE 1=1", []
    if status:
        q += " AND status=?"; args.append(status)
    if assignee:
        q += " AND assignee=?"; args.append(assignee)
    with db() as c:
        rows = c.execute(q + " ORDER BY id", args).fetchall()
    return "\n".join(_fmt_task(r) for r in rows) or "No tasks."


@mcp.tool()
def task_get(task_id: int) -> str:
    """Show one task with its description and full activity log."""
    with db() as c:
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            return f"No task T{task_id}."
        log = c.execute("SELECT * FROM task_log WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
    out = [_fmt_task(t)]
    if t["description"]:
        out.append(t["description"])
    out.append("Log:")
    out += [f"  {_ago(l['ts'])} {l['agent']}: {l['note']}" for l in log]
    return "\n".join(out)


@mcp.tool()
def task_update(agent: str, task_id: int, status: str = "", assignee: str = "", note: str = "") -> str:
    """Update a task: change status, reassign (use your own name to claim it), and/or leave a progress note."""
    if status and status not in STATUSES:
        return f"Invalid status. Use one of: {', '.join(STATUSES)}"
    now = time.time()
    with db() as c:
        _touch(c, agent)
        t = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not t:
            return f"No task T{task_id}."
        changes = []
        if status:
            c.execute("UPDATE tasks SET status=?, updated=? WHERE id=?", (status, now, task_id))
            changes.append(f"status → {status}")
        if assignee:
            c.execute("UPDATE tasks SET assignee=?, updated=? WHERE id=?", (assignee, now, task_id))
            changes.append(f"assignee → {assignee}")
        if note:
            changes.append(note)
        if not changes:
            return "Nothing to update."
        c.execute("UPDATE tasks SET updated=? WHERE id=?", (now, task_id))
        c.execute("INSERT INTO task_log(task_id, agent, note, ts) VALUES(?,?,?,?)", (task_id, agent, "; ".join(changes), now))
    return f"T{task_id} updated: {'; '.join(changes)}"


# ---------- shared notes ----------
@mcp.tool()
def note_write(agent: str, key: str, content: str, append: bool = False) -> str:
    """Write (or append to) a shared note — use for specs, decisions, drafts, lyrics, code snippets, etc."""
    now = time.time()
    with db() as c:
        _touch(c, agent)
        if append:
            old = c.execute("SELECT content FROM notes WHERE key=?", (key,)).fetchone()
            if old and old["content"]:
                content = old["content"] + "\n" + content
        c.execute(
            "INSERT INTO notes(key, content, updated_by, updated) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET content=excluded.content, updated_by=excluded.updated_by, updated=excluded.updated",
            (key, content, agent, now),
        )
    return f"Saved note '{key}'."


@mcp.tool()
def note_read(key: str) -> str:
    """Read a shared note by key."""
    with db() as c:
        r = c.execute("SELECT * FROM notes WHERE key=?", (key,)).fetchone()
    if not r:
        return f"No note '{key}'."
    return f"[{key}] last edited by {r['updated_by']} {_ago(r['updated'])}\n\n{r['content']}"


@mcp.tool()
def note_list() -> str:
    """List all shared notes."""
    with db() as c:
        rows = c.execute("SELECT key, updated_by, updated, length(content) n FROM notes ORDER BY updated DESC").fetchall()
    return "\n".join(f"- {r['key']} ({r['n']} chars, {r['updated_by']}, {_ago(r['updated'])})" for r in rows) or "No notes."


@mcp.tool()
def status(agent: str) -> str:
    """One-call catch-up: your unread messages, your tasks, open tasks, and who's around."""
    return "\n\n".join([
        "== Unread ==\n" + inbox(agent),
        "== Your tasks ==\n" + task_list(assignee=agent),
        "== All open tasks ==\n" + task_list(status="open"),
        "== Team ==\n" + who(),
    ])


def main():
    p = argparse.ArgumentParser(description="Trio Hub MCP server")
    p.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    p.add_argument("--host", default=os.environ.get("TRIO_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("TRIO_PORT", "8765")))
    a = p.parse_args()
    if not a.http:
        mcp.run()
    elif _V2:
        mcp.run(transport="streamable-http", host=a.host, port=a.port)
    else:
        mcp.settings.host, mcp.settings.port = a.host, a.port
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
