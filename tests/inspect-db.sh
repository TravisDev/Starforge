#!/usr/bin/env bash
# Print a snapshot of the SQLite database — table row counts and key rows.
# Honors STARFORGE_DATA_DIR; defaults to the project root.
cd "$(dirname "$0")/.."
DB_PATH="${STARFORGE_DATA_DIR:-.}/board.db"
DB_PATH="$DB_PATH" python - <<'PY'
import os, sqlite3, sys
db = os.environ["DB_PATH"]
if not os.path.exists(db):
    print(f"DB not found at {db}", file=sys.stderr); sys.exit(1)
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

print(f"DB: {db}")
print()
print("--- tables (row counts) ---")
for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    name = r["name"]
    if name.startswith("sqlite_"):
        continue
    n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
    print(f"  {name:20s} {n}")

def section(title, sql):
    print()
    print(f"--- {title} ---")
    rows = list(conn.execute(sql))
    if not rows:
        print("  (empty)")
        return
    for r in rows:
        print(f"  {dict(r)}")

section("projects", "SELECT id, slug, name, color, is_archived FROM projects")
section("users",    "SELECT id, email, display_name, is_admin, is_active FROM users")
section("team_members",
        "SELECT id, project_id, name, type, role, is_active FROM team_members")
section("tasks (latest 5)",
        """SELECT id, project_id, title, status, assignee_id, assignee
           FROM tasks ORDER BY updated_at DESC LIMIT 5""")
section("sso_providers",
        "SELECT id, slug, display_name, issuer, is_enabled FROM sso_providers")
section("sessions (active count)",
        "SELECT COUNT(*) AS c FROM sessions WHERE expires_at > datetime('now')")
PY
