# -*- coding: utf-8 -*-
"""
认证与权限辅助
"""
import os
import sys
import functools
import time

from flask import session, request, jsonify
from core.db import (open_conn, is_closed, get_db, DB_PATH, DATA_DIR,
                     ensure_dirs)

ensure_dirs()


try:
    import bcrypt
    HAS_BCRYPT = True
except Exception:
    HAS_BCRYPT = False


# 登录失败锁定（进程内，防止弱口令暴力）：单账号连续失败达到阈值后锁定一段时间
_LOGIN_FAIL = {}
_LOGIN_MAX_FAIL = 5
_LOGIN_LOCK_SECONDS = 900


def _login_locked(uid):
    rec = _LOGIN_FAIL.get(uid)
    if rec and rec.get("until", 0) > time.time():
        return True, int(rec["until"] - time.time())
    return False, 0


def _register_fail(uid):
    rec = _LOGIN_FAIL.get(uid) or {"count": 0, "until": 0}
    rec["count"] += 1
    if rec["count"] >= _LOGIN_MAX_FAIL:
        rec["until"] = time.time() + _LOGIN_LOCK_SECONDS
        rec["count"] = 0
    _LOGIN_FAIL[uid] = rec


def _clear_fail(uid):
    _LOGIN_FAIL.pop(uid, None)


# ---------- 鉴权 ----------
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    return u


def get_request_user():
    """从 session 取当前登录用户（单应用架构下不再依赖 X-NPI-UID 跨域头）。"""
    uid = session.get("uid")
    if not uid:
        return None
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
    return u


def is_admin(u, allow_project_admin=False):
    if not u:
        return False
    if u["role"] == "superAdmin":
        return True
    if allow_project_admin and u["role"] in ("superAdmin", "projectAdmin"):
        return True
    return False


def require_login():
    """装饰器：要求登录。"""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            u = get_request_user()
            if not u:
                return jsonify({"error": "未登录"}), 401
            return f(*args, **kwargs)
        return wrapper
    return decorator


def require_admin(allow_project_admin=False):
    """装饰器：要求管理员（可选允许项目管理员）。"""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            u = get_request_user()
            if not is_admin(u, allow_project_admin=allow_project_admin):
                return jsonify({"error": "需要管理员权限"}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ---------- 认证路由 ----------
def register_auth_routes(app):
    @app.route("/api/me")
    def api_me():
        u = current_user()
        if not u:
            return jsonify(None), 401
        return jsonify({"uid": u["uid"], "name": u["name"],
                        "role": u["role"], "dept": u["dept"],
                        "must_change": bool(u["must_change_password"])})

    @app.route("/api/login", methods=["POST"])
    def api_login():
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        pw = data.get("password") or ""
        locked, remain = _login_locked(uid)
        if locked:
            return jsonify({"error": "账号已锁定，请 %d 秒后重试" % remain}), 423
        conn = get_db()
        u = conn.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        if not u or u["frozen"]:
            return jsonify({"error": "账号不存在或已冻结"}), 401
        ok = False
        ph = (u["password_hash"] or "").strip()
        if HAS_BCRYPT and ph.startswith("$2"):
            # 标准 bcrypt 哈希校验
            try:
                ok = bcrypt.checkpw(pw.encode("utf-8"), ph.encode("utf-8"))
            except Exception:
                ok = False
        if not ok and not ph.startswith("$2"):
            # 兼容未哈希的遗留明文密码（避免脏数据导致账号永久无法登录）
            ok = (pw == ph)
        if not ok:
            _register_fail(uid)
            return jsonify({"error": "密码错误"}), 401
        _clear_fail(uid)
        session["uid"] = u["uid"]
        return jsonify({"uid": u["uid"], "name": u["name"], "role": u["role"],
                        "must_change": bool(u["must_change_password"])})

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.route("/api/change-password", methods=["POST"])
    def api_change_password():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        old = data.get("old") or ""
        new = data.get("new") or ""
        if not new or len(new) < 4:
            return jsonify({"error": "新密码至少 4 位"}), 400
        ph = (u["password_hash"] or "").strip()
        if HAS_BCRYPT and ph.startswith("$2"):
            try:
                if not bcrypt.checkpw(old.encode("utf-8"), ph.encode("utf-8")):
                    return jsonify({"error": "原密码错误"}), 400
            except Exception:
                return jsonify({"error": "原密码错误"}), 400
        elif not ph.startswith("$2"):
            # 遗留明文哈希直接比较
            if old != ph:
                return jsonify({"error": "原密码错误"}), 400
        elif old != ph:
            return jsonify({"error": "原密码错误"}), 400
        nh = bcrypt.hashpw(new.encode("utf-8"), bcrypt.gensalt()).decode("utf-8") if HAS_BCRYPT \
            else new
        conn = get_db()
        conn.execute(
            "UPDATE users SET password_hash=?, must_change_password=0 WHERE uid=?",
            (nh, u["uid"]),
        )
        conn.commit()
        from core.audit import write_audit
        write_audit(u["uid"], "change_password", "修改登录密码")
        return jsonify({"ok": True})
