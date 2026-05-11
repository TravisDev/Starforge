"""
Capture screenshots of Starforge for the README.

Authenticates by creating a session row in the DB directly (no need to know
the admin password), then drives Chromium via Playwright to navigate the app
and screenshot key views.

Outputs land in ./screenshots/.

Run: python tests/capture-screenshots.py
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "board.db"
OUT = ROOT / "screenshots"
OUT.mkdir(exist_ok=True)
BASE_URL = "http://localhost:8000"


def make_session_for_first_admin() -> str:
    """Insert a session row for the first admin user. Returns the unhashed
    session token to use as the agent_board_session / starforge_session cookie."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    user = conn.execute(
        "SELECT id, email FROM users WHERE is_admin = 1 AND is_active = 1 "
        "ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not user:
        print("No admin user found in DB. Run the /setup flow first.", file=sys.stderr)
        sys.exit(1)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    conn.execute(
        """INSERT INTO sessions
               (user_id, token_hash, created_at, expires_at, last_seen_at, user_agent, ip)
               VALUES (?, ?, ?, ?, ?, 'screenshot-bot', '127.0.0.1')""",
        (
            user["id"],
            token_hash,
            now.isoformat(),
            (now + timedelta(hours=1)).isoformat(),
            now.isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    print(f"Authenticated as {user['email']} (user_id={user['id']})")
    return token


def shoot(page, name: str, *, full_page: bool = False) -> None:
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=full_page)
    print(f"  -> {path.relative_to(ROOT)}")


def main() -> None:
    token = make_session_for_first_admin()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,  # higher DPI for crisp readme images
        )
        # Set the session cookie under both legacy and current names so the
        # auth dependency accepts us regardless of the rename history.
        for cookie_name in ("starforge_session", "agent_board_session"):
            context.add_cookies([{
                "name": cookie_name,
                "value": token,
                "domain": "localhost",
                "path": "/",
                "httpOnly": True,
                "sameSite": "Lax",
            }])
        page = context.new_page()

        # 1. Main board — kanban + team pane + project dropdown
        page.goto(f"{BASE_URL}/")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)
        shoot(page, "board")

        # 2. Task modal showing a real conversation thread (task 8 has comments)
        # Click the first task card we can find.
        try:
            page.evaluate("""
                () => {
                    const cards = document.querySelectorAll('.card');
                    for (const c of cards) {
                        const title = c.querySelector('.title');
                        if (title && title.textContent.toLowerCase().includes('test')) {
                            c.click(); return;
                        }
                    }
                    if (cards.length) cards[0].click();
                }
            """)
            page.wait_for_selector("#overlay.open", timeout=3000)
            page.wait_for_timeout(600)
            shoot(page, "task-with-comments")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"  (skipped task-with-comments: {e})")

        # 3. Team-member modal for Beep Boop 42 (or first AI agent)
        try:
            page.evaluate("""
                () => {
                    const rows = document.querySelectorAll('.member');
                    for (const r of rows) {
                        if (r.querySelector('.type-pill.ai_agent')) { r.click(); return; }
                    }
                    if (rows.length) rows[0].click();
                }
            """)
            page.wait_for_selector("#memOverlay.open", timeout=3000)
            page.wait_for_timeout(600)
            shoot(page, "team-member-modal")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception as e:
            print(f"  (skipped team-member-modal: {e})")

        # 4. Projects management page
        page.goto(f"{BASE_URL}/projects")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        shoot(page, "projects-page")

        # 5. Admin settings page (SSO + agent runtime poll interval)
        page.goto(f"{BASE_URL}/settings")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        shoot(page, "settings-page")

        browser.close()
    print("done.")


if __name__ == "__main__":
    main()
