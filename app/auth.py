"""Authentication, Session Management, First-Time Admin Setup, and User Access Control."""
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import HTTPConnection

from app.database import (
    db_session,
    get_resource_path,
    log_audit,
    set_current_username,
    reset_current_username,
)

router = APIRouter()
templates = Jinja2Templates(directory=get_resource_path(os.path.join("app", "templates")))

SESSION_COOKIE_NAME = "sr_session"
DEFAULT_SESSION_HOURS = 12
REMEMBER_SESSION_DAYS = 30
PBKDF2_ITERATIONS = 200_000
MIN_PASSWORD_LEN = 6
USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")

_LOGIN_FAILURES = {}
_LOGIN_LOCK = threading.Lock()
_MAX_LOGIN_ATTEMPTS = 10
_LOGIN_WINDOW_SEC = 300


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-HMAC-SHA256, stdlib only)
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash or not password:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iters = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# Schema migration & helpers
# ---------------------------------------------------------------------------
def ensure_auth_schema(conn) -> None:
    """Idempotently add auth columns to app_users and create app_sessions."""
    cols = {r[1] for r in conn.execute('PRAGMA table_info("app_users")').fetchall()}
    if "password_hash" not in cols:
        conn.execute('ALTER TABLE "app_users" ADD COLUMN "password_hash" TEXT DEFAULT \'\'')
    if "is_active" not in cols:
        conn.execute('ALTER TABLE "app_users" ADD COLUMN "is_active" INTEGER NOT NULL DEFAULT 1')
    if "last_login" not in cols:
        conn.execute('ALTER TABLE "app_users" ADD COLUMN "last_login" TEXT DEFAULT \'\'')

    conn.execute(
        '''CREATE TABLE IF NOT EXISTS "app_sessions" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT,
            "token_hash" TEXT UNIQUE NOT NULL,
            "user_id" INTEGER NOT NULL REFERENCES "app_users"("id") ON DELETE CASCADE,
            "expires_at" TEXT NOT NULL,
            "ip_address" TEXT DEFAULT '',
            "created_at" TEXT DEFAULT CURRENT_TIMESTAMP
        )'''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_app_sessions_expires ON "app_sessions"("expires_at")')

    if conn.execute('SELECT COUNT(*) FROM "app_users"').fetchone()[0] == 0:
        conn.execute(
            'INSERT OR IGNORE INTO "app_users" ("username","full_name","role","is_active") VALUES (?,?,?,1)',
            ("admin", "Production Engineering Admin", "admin"),
        )


def is_setup_complete(conn) -> bool:
    """True once at least one active admin account has a password configured."""
    row = conn.execute(
        'SELECT 1 FROM "app_users" '
        'WHERE LOWER("role")=\'admin\' AND COALESCE("is_active", 1)=1 '
        'AND COALESCE("password_hash", \'\')<>\'\' LIMIT 1'
    ).fetchone()
    return bool(row)


def _normalize_role(role: str) -> str:
    return "admin" if (role or "").strip().lower() == "admin" else "user"


def _user_dict(row) -> dict:
    is_admin = _normalize_role(row["role"]) == "admin"
    return {
        "id": row["id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "role": "admin" if is_admin else "user",
        "role_label": "Admin" if is_admin else "Standard User",
        "is_admin": is_admin,
        "is_active": bool(row["is_active"]),
        "last_login": row["last_login"] or "",
        "created_at": row["created_at"] or "",
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(conn, user_id: int, remember: bool = False, ip_address: str = "") -> tuple[str, int]:
    raw_token = secrets.token_urlsafe(32)
    max_age = (REMEMBER_SESSION_DAYS * 86400) if remember else (DEFAULT_SESSION_HOURS * 3600)
    expires_at = (_utc_now() + timedelta(seconds=max_age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute('DELETE FROM "app_sessions" WHERE "expires_at" <= ?', (_utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),))
    conn.execute(
        'INSERT INTO "app_sessions" ("token_hash","user_id","expires_at","ip_address") VALUES (?,?,?,?)',
        (_hash_token(raw_token), user_id, expires_at, (ip_address or "")[:64]),
    )
    return raw_token, max_age


def get_user_by_session(conn, raw_token: Optional[str]) -> Optional[dict]:
    if not raw_token:
        return None
    now_str = _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    sql = '''SELECT u.* FROM "app_sessions" s
           JOIN "app_users" u ON u."id" = s."user_id"
           WHERE s."token_hash"=? AND s."expires_at" > ? LIMIT 1'''
    args = (_hash_token(raw_token), now_str)
    row = conn.execute(sql, args).fetchone()
    if not row or not row["is_active"] or not (row["password_hash"] or "").strip():
        return None
    return _user_dict(row)


def delete_session(conn, raw_token: Optional[str]) -> None:
    if raw_token:
        conn.execute('DELETE FROM "app_sessions" WHERE "token_hash"=?', (_hash_token(raw_token),))


def delete_user_sessions(conn, user_id: int) -> None:
    conn.execute('DELETE FROM "app_sessions" WHERE "user_id"=?', (user_id,))


def _set_session_cookie(resp, request: Request, raw_token: str, max_age: int) -> None:
    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )


def _safe_next_url(next_url: str) -> str:
    cand = (next_url or "").strip()
    if not cand or not cand.startswith("/") or cand.startswith("//"):
        return "/"
    path_only = cand.split("?", 1)[0]
    if path_only in ("/login", "/setup", "/logout"):
        return "/"
    return cand


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_rate_limited(ip: str, username: str) -> bool:
    key = f"{ip}|{(username or '').strip().lower()}"
    now = time.monotonic()
    with _LOGIN_LOCK:
        attempts = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < _LOGIN_WINDOW_SEC]
        _LOGIN_FAILURES[key] = attempts
        return len(attempts) >= _MAX_LOGIN_ATTEMPTS


def _record_login_failure(ip: str, username: str) -> None:
    key = f"{ip}|{(username or '').strip().lower()}"
    now = time.monotonic()
    with _LOGIN_LOCK:
        attempts = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < _LOGIN_WINDOW_SEC]
        attempts.append(now)
        _LOGIN_FAILURES[key] = attempts


def _clear_login_failures(ip: str, username: str) -> None:
    key = f"{ip}|{(username or '').strip().lower()}"
    with _LOGIN_LOCK:
        _LOGIN_FAILURES.pop(key, None)


def _is_api_request(request: Request) -> bool:
    path = request.url.path
    accept = (request.headers.get("accept") or "").lower()
    return path.startswith("/api/") or ("application/json" in accept and "text/html" not in accept)


def _is_admin_only_path(path: str) -> bool:
    if path == "/admin" or path.startswith("/admin/"):
        return True
    if path == "/api/backup" or path.startswith("/api/backup/") or path == "/api/status":
        return True
    return False


# ---------------------------------------------------------------------------
# Global Auth & Access-Control Middleware (Pure ASGI)
# ---------------------------------------------------------------------------
class AuthMiddleware:
    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        path = conn.url.path
        if path.startswith("/static/") or path in ("/sw.js", "/manifest.json", "/favicon.ico"):
            await self.app(scope, receive, send)
            return

        raw_token = conn.cookies.get(SESSION_COOKIE_NAME)
        with db_session() as db:
            setup_done = is_setup_complete(db)
            user = get_user_by_session(db, raw_token) if setup_done else None

        accept = (conn.headers.get("accept") or "").lower()
        is_api = path.startswith("/api/") or ("application/json" in accept and "text/html" not in accept)

        if not setup_done:
            if path == "/setup":
                await self.app(scope, receive, send)
                return
            resp = (
                JSONResponse({"ok": False, "error": "Initial admin setup required.", "redirect": "/setup"}, status_code=401)
                if is_api
                else RedirectResponse(url="/setup", status_code=303)
            )
            await resp(scope, receive, send)
            return

        if path == "/setup":
            resp = RedirectResponse(url="/" if user else "/login", status_code=303)
            await resp(scope, receive, send)
            return

        if path in ("/login", "/logout"):
            scope.setdefault("state", {})["user"] = user
            tok = set_current_username(user["username"]) if user else None
            try:
                await self.app(scope, receive, send)
            finally:
                if tok is not None:
                    reset_current_username(tok)
            return

        if not user:
            if is_api:
                resp = JSONResponse({"ok": False, "error": "Authentication required.", "redirect": "/login"}, status_code=401)
                await resp(scope, receive, send)
                return
            if scope.get("method", "GET").upper() == "GET":
                target = path + (f"?{conn.url.query}" if conn.url.query else "")
                safe_target = _safe_next_url(target)
                login_url = "/login" if safe_target == "/" else f"/login?next={quote(safe_target)}"
            else:
                login_url = "/login"
            resp = RedirectResponse(url=login_url, status_code=303)
            if raw_token:
                resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
            await resp(scope, receive, send)
            return

        scope.setdefault("state", {})["user"] = user

        if _is_admin_only_path(path) and not user["is_admin"]:
            if is_api:
                resp = JSONResponse({"ok": False, "error": "Administrator access required."}, status_code=403)
            else:
                req = Request(scope, receive=receive)
                resp = templates.TemplateResponse(
                    request=req,
                    name="forbidden.html",
                    context={"request": req, "active": ""},
                    status_code=403,
                )
            await resp(scope, receive, send)
            return

        tok = set_current_username(user["username"])
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_username(tok)


# ---------------------------------------------------------------------------
# First-Time Setup, Login, Logout & Account Routes
# ---------------------------------------------------------------------------
@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, err: str = ""):
    with db_session() as conn:
        if is_setup_complete(conn):
            return RedirectResponse(url="/login", status_code=303)
        row = conn.execute(
            'SELECT "username","full_name" FROM "app_users" WHERE LOWER("role")=\'admin\' ORDER BY "id" LIMIT 1'
        ).fetchone()
        default_username = row["username"] if row else "admin"
        default_full_name = row["full_name"] if row else "Production Engineering Admin"
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={
            "request": request,
            "default_username": default_username,
            "default_full_name": default_full_name,
            "err": err,
        },
    )


@router.post("/setup")
def setup_submit(
    request: Request,
    username: str = Form("admin"),
    full_name: str = Form("Production Engineering Admin"),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    uname = username.strip()
    fname = full_name.strip() or "Production Engineering Admin"
    if not USERNAME_RE.match(uname):
        return RedirectResponse(
            url=f"/setup?err={quote('Username must be 3-32 characters (letters, numbers, ., _, -).')}",
            status_code=303,
        )
    if len(password) < MIN_PASSWORD_LEN:
        return RedirectResponse(
            url=f"/setup?err={quote(f'Password must be at least {MIN_PASSWORD_LEN} characters.')}",
            status_code=303,
        )
    if password != confirm_password:
        return RedirectResponse(url=f"/setup?err={quote('Passwords do not match.')}", status_code=303)

    pw_hash = hash_password(password)
    with db_session(write=True) as conn:
        if is_setup_complete(conn):
            return RedirectResponse(url="/login", status_code=303)
        existing_admin = conn.execute(
            'SELECT "id" FROM "app_users" WHERE LOWER("role")=\'admin\' ORDER BY "id" LIMIT 1'
        ).fetchone()
        conflict = conn.execute(
            'SELECT "id" FROM "app_users" WHERE LOWER("username")=LOWER(?)',
            (uname,),
        ).fetchone()
        if existing_admin and (not conflict or conflict["id"] == existing_admin["id"]):
            user_id = existing_admin["id"]
            conn.execute(
                'UPDATE "app_users" SET "username"=?, "full_name"=?, "role"=\'admin\', '
                '"password_hash"=?, "is_active"=1, "last_login"=CURRENT_TIMESTAMP WHERE "id"=?',
                (uname, fname, pw_hash, user_id),
            )
        elif conflict:
            user_id = conflict["id"]
            conn.execute(
                'UPDATE "app_users" SET "full_name"=?, "role"=\'admin\', "password_hash"=?, '
                '"is_active"=1, "last_login"=CURRENT_TIMESTAMP WHERE "id"=?',
                (fname, pw_hash, user_id),
            )
        else:
            conn.execute(
                'INSERT INTO "app_users" ("username","full_name","role","password_hash","is_active","last_login") '
                'VALUES (?,?, \'admin\', ?, 1, CURRENT_TIMESTAMP)',
                (uname, fname, pw_hash),
            )
            user_id = conn.execute(
                'SELECT "id" FROM "app_users" WHERE LOWER("username")=LOWER(?) LIMIT 1',
                (uname,),
            ).fetchone()[0]

        raw_token, max_age = create_session(conn, user_id, remember=False, ip_address=_client_ip(request))
        log_audit(conn, "SETUP_ADMIN", "app_users", uname, username=uname)

    resp = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(resp, request, raw_token, max_age)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "", err: str = "", msg: str = ""):
    if getattr(request.state, "user", None):
        return RedirectResponse(url=_safe_next_url(next), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "next_url": _safe_next_url(next),
            "err": err,
            "msg": msg,
        },
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: str = Form(""),
    next: str = Form("/"),
):
    uname = username.strip()
    next_url = _safe_next_url(next)
    ip = _client_ip(request)
    if _is_rate_limited(ip, uname):
        return RedirectResponse(
            url=f"/login?next={quote(next_url)}&err={quote('Too many failed attempts. Please wait 5 minutes and try again.')}",
            status_code=303,
        )

    with db_session(write=True) as conn:
        row = conn.execute(
            'SELECT * FROM "app_users" WHERE LOWER("username")=LOWER(?) LIMIT 1',
            (uname,),
        ).fetchone()
        if not row or not verify_password(password, row["password_hash"] or ""):
            _record_login_failure(ip, uname)
            log_audit(conn, "LOGIN_FAILED", "app_users", f"{uname} ({ip})", username=uname or "anonymous")
            return RedirectResponse(
                url=f"/login?next={quote(next_url)}&err={quote('Invalid username or password.')}",
                status_code=303,
            )
        if not row["is_active"]:
            log_audit(conn, "LOGIN_BLOCKED", "app_users", f"{row['username']} disabled", username=row["username"])
            return RedirectResponse(
                url=f"/login?next={quote(next_url)}&err={quote('Your account has been disabled by an administrator.')}",
                status_code=303,
            )

        _clear_login_failures(ip, uname)
        conn.execute('UPDATE "app_users" SET "last_login"=CURRENT_TIMESTAMP WHERE "id"=?', (row["id"],))
        raw_token, max_age = create_session(conn, row["id"], remember=bool(remember), ip_address=ip)
        log_audit(conn, "LOGIN_SUCCESS", "app_users", f"{row['username']} ({ip})", username=row["username"])

    resp = RedirectResponse(url=next_url, status_code=303)
    _set_session_cookie(resp, request, raw_token, max_age)
    return resp


@router.post("/logout")
@router.get("/logout")
def logout_submit(request: Request):
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    user = getattr(request.state, "user", None)
    with db_session(write=True) as conn:
        delete_session(conn, raw_token)
        if user:
            log_audit(conn, "LOGOUT", "app_users", user["username"], username=user["username"])
    resp = RedirectResponse(url="/login?msg=" + quote("You have been signed out."), status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, msg: str = "", err: str = ""):
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={"request": request, "active": "account", "msg": msg, "err": err},
    )


@router.post("/account/password")
def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = request.state.user
    if len(new_password) < MIN_PASSWORD_LEN:
        return RedirectResponse(
            url=f"/account?err={quote(f'New password must be at least {MIN_PASSWORD_LEN} characters.')}",
            status_code=303,
        )
    if new_password != confirm_password:
        return RedirectResponse(url=f"/account?err={quote('New passwords do not match.')}", status_code=303)

    with db_session(write=True) as conn:
        row = conn.execute('SELECT * FROM "app_users" WHERE "id"=?', (user["id"],)).fetchone()
        if not row or not verify_password(current_password, row["password_hash"] or ""):
            return RedirectResponse(url=f"/account?err={quote('Current password is incorrect.')}", status_code=303)
        conn.execute('UPDATE "app_users" SET "password_hash"=? WHERE "id"=?', (hash_password(new_password), user["id"]))
        log_audit(conn, "CHANGE_OWN_PASSWORD", "app_users", user["username"])

    return RedirectResponse(url="/account?msg=" + quote("Password updated successfully."), status_code=303)


# ---------------------------------------------------------------------------
# Admin User Access Control (/admin/users)
# ---------------------------------------------------------------------------
def _active_admin_count(conn, exclude_user_id: Optional[int] = None) -> int:
    if exclude_user_id is None:
        r = conn.execute(
            'SELECT COUNT(*) FROM "app_users" WHERE LOWER("role")=\'admin\' AND "is_active"=1'
        ).fetchone()
    else:
        r = conn.execute(
            'SELECT COUNT(*) FROM "app_users" WHERE LOWER("role")=\'admin\' AND "is_active"=1 AND "id"<>?',
            (exclude_user_id,),
        ).fetchone()
    return int(r[0])


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, msg: str = "", err: str = ""):
    with db_session() as conn:
        rows = conn.execute(
            'SELECT * FROM "app_users" ORDER BY CASE WHEN LOWER("role")=\'admin\' THEN 0 ELSE 1 END, LOWER("username")'
        ).fetchall()
        users = [_user_dict(r) for r in rows]
        active_sessions = {
            r["user_id"]: r["cnt"]
            for r in conn.execute(
                'SELECT "user_id", COUNT(*) AS cnt FROM "app_sessions" WHERE "expires_at" > ? GROUP BY "user_id"',
                (_utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),),
            ).fetchall()
        }
    for u in users:
        u["session_count"] = active_sessions.get(u["id"], 0)
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={"request": request, "active": "admin_users", "users": users, "msg": msg, "err": err},
    )


@router.post("/admin/users/create")
def admin_users_create(
    username: str = Form(...),
    full_name: str = Form(...),
    role: str = Form("user"),
    password: str = Form(...),
):
    uname = username.strip()
    fname = full_name.strip()
    norm_role = _normalize_role(role)
    if not USERNAME_RE.match(uname):
        return RedirectResponse(
            url=f"/admin/users?err={quote('Username must be 3-32 characters (letters, numbers, ., _, -).')}",
            status_code=303,
        )
    if not fname:
        return RedirectResponse(url=f"/admin/users?err={quote('Full name is required.')}", status_code=303)
    if len(password) < MIN_PASSWORD_LEN:
        return RedirectResponse(
            url=f"/admin/users?err={quote(f'Password must be at least {MIN_PASSWORD_LEN} characters.')}",
            status_code=303,
        )

    with db_session(write=True) as conn:
        if conn.execute('SELECT 1 FROM "app_users" WHERE LOWER("username")=LOWER(?)', (uname,)).fetchone():
            return RedirectResponse(url=f"/admin/users?err={quote(f'Username {uname} already exists.')}", status_code=303)
        conn.execute(
            'INSERT INTO "app_users" ("username","full_name","role","password_hash","is_active") VALUES (?,?,?,?,1)',
            (uname, fname, norm_role, hash_password(password)),
        )
        log_audit(conn, "CREATE_USER", "app_users", f"{uname} ({norm_role})")

    return RedirectResponse(url=f"/admin/users?msg={quote(f'User {uname} created.')}", status_code=303)


@router.post("/admin/users/update/{user_id}")
def admin_users_update(
    request: Request,
    user_id: int,
    full_name: str = Form(...),
    role: str = Form("user"),
):
    fname = full_name.strip()
    norm_role = _normalize_role(role)
    if not fname:
        return RedirectResponse(url=f"/admin/users?err={quote('Full name cannot be empty.')}", status_code=303)

    with db_session(write=True) as conn:
        row = conn.execute('SELECT * FROM "app_users" WHERE "id"=?', (user_id,)).fetchone()
        if not row:
            return RedirectResponse(url=f"/admin/users?err={quote('User not found.')}", status_code=303)
        was_admin = _normalize_role(row["role"]) == "admin"
        if was_admin and norm_role != "admin" and _active_admin_count(conn, exclude_user_id=user_id) == 0:
            return RedirectResponse(
                url=f"/admin/users?err={quote('Cannot demote the last active Administrator.')}",
                status_code=303,
            )
        if user_id == request.state.user["id"] and norm_role != "admin":
            return RedirectResponse(
                url=f"/admin/users?err={quote('You cannot remove your own Admin role.')}",
                status_code=303,
            )
        conn.execute('UPDATE "app_users" SET "full_name"=?, "role"=? WHERE "id"=?', (fname, norm_role, user_id))
        uname = row["username"]
        log_audit(conn, "UPDATE_USER", "app_users", f"{uname} -> {norm_role}")

    return RedirectResponse(url=f"/admin/users?msg={quote(f'Updated user {uname}.')}", status_code=303)


@router.post("/admin/users/toggle/{user_id}")
def admin_users_toggle(request: Request, user_id: int):
    if user_id == request.state.user["id"]:
        return RedirectResponse(url=f"/admin/users?err={quote('You cannot disable your own account.')}", status_code=303)

    with db_session(write=True) as conn:
        row = conn.execute('SELECT * FROM "app_users" WHERE "id"=?', (user_id,)).fetchone()
        if not row:
            return RedirectResponse(url=f"/admin/users?err={quote('User not found.')}", status_code=303)
        new_active = 0 if row["is_active"] else 1
        if new_active == 0 and _normalize_role(row["role"]) == "admin" and _active_admin_count(conn, exclude_user_id=user_id) == 0:
            return RedirectResponse(
                url=f"/admin/users?err={quote('Cannot disable the last active Administrator.')}",
                status_code=303,
            )
        conn.execute('UPDATE "app_users" SET "is_active"=? WHERE "id"=?', (new_active, user_id))
        if new_active == 0:
            delete_user_sessions(conn, user_id)
        log_audit(conn, "TOGGLE_USER", "app_users", f"{row['username']} active={new_active}")

    msg = f"Enabled {row['username']}." if new_active else f"Disabled {row['username']} and ended active sessions."
    return RedirectResponse(url=f"/admin/users?msg={quote(msg)}", status_code=303)


@router.post("/admin/users/reset-password/{user_id}")
def admin_users_reset_password(user_id: int, new_password: str = Form(...)):
    if len(new_password) < MIN_PASSWORD_LEN:
        return RedirectResponse(
            url=f"/admin/users?err={quote(f'Password must be at least {MIN_PASSWORD_LEN} characters.')}",
            status_code=303,
        )
    with db_session(write=True) as conn:
        row = conn.execute('SELECT "username" FROM "app_users" WHERE "id"=?', (user_id,)).fetchone()
        if not row:
            return RedirectResponse(url=f"/admin/users?err={quote('User not found.')}", status_code=303)
        uname = row["username"]
        conn.execute('UPDATE "app_users" SET "password_hash"=? WHERE "id"=?', (hash_password(new_password), user_id))
        delete_user_sessions(conn, user_id)
        log_audit(conn, "RESET_PASSWORD", "app_users", uname)
    return RedirectResponse(url=f"/admin/users?msg={quote(f'Password reset for {uname}.')}", status_code=303)


@router.post("/admin/users/delete/{user_id}")
def admin_users_delete(request: Request, user_id: int):
    if user_id == request.state.user["id"]:
        return RedirectResponse(url=f"/admin/users?err={quote('You cannot delete your own account.')}", status_code=303)
    with db_session(write=True) as conn:
        row = conn.execute('SELECT * FROM "app_users" WHERE "id"=?', (user_id,)).fetchone()
        if not row:
            return RedirectResponse(url=f"/admin/users?err={quote('User not found.')}", status_code=303)
        uname = row["username"]
        if _normalize_role(row["role"]) == "admin" and _active_admin_count(conn, exclude_user_id=user_id) == 0:
            return RedirectResponse(
                url=f"/admin/users?err={quote('Cannot delete the last active Administrator.')}",
                status_code=303,
            )
        conn.execute('DELETE FROM "app_users" WHERE "id"=?', (user_id,))
        log_audit(conn, "DELETE_USER", "app_users", uname)
    return RedirectResponse(url=f"/admin/users?msg={quote(f'Deleted user {uname}.')}", status_code=303)
