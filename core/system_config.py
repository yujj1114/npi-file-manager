# -*- coding: utf-8 -*-
"""
系统设置 API（仅 superAdmin 可维护）
"""
import json
import os
import shutil
import subprocess
from flask import request, jsonify
from core.db import get_db, get_upload_roots, get_current_upload_root
from core.auth import get_request_user
from core.audit import write_audit


def get_setting(conn, key, default=""):
    r = conn.execute("SELECT value FROM system_config WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
        (key, value),
    )


def register_system_config_routes(app):

    @app.route("/api/admin/system-config")
    def api_system_config_list():
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        conn = get_db()
        rows = conn.execute("SELECT key, value FROM system_config ORDER BY key").fetchall()
        return jsonify({r["key"]: r["value"] for r in rows})

    @app.route("/api/admin/system-config/<key>", methods=["PUT"])
    def api_system_config_update(key):
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        value = (data.get("value") if isinstance(data.get("value"), str) else "").strip()
        conn = get_db()
        set_setting(conn, key, value)
        conn.commit()
        write_audit(u["uid"], "update_system_config", "%s=%s" % (key, value))
        return jsonify({"ok": True})

    @app.route("/api/admin/system-config/upload-roots", methods=["GET"])
    def api_system_config_upload_roots():
        """返回存储路径列表及当前写入路径。"""
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        roots = get_upload_roots()
        current = get_current_upload_root()
        import shutil
        items = []
        for idx, p in enumerate(roots):
            display = p.replace(os.sep, "/")
            st = {"path": display, "exists": False, "writable": False, "total": 0, "free": 0, "current": p == current}
            try:
                st["exists"] = os.path.isdir(p)
                if st["exists"]:
                    st["writable"] = os.access(p, os.W_OK)
                    usage = shutil.disk_usage(p)
                    st["total"] = usage.total
                    st["free"] = usage.free
            except Exception:
                pass
            items.append(st)
        return jsonify({"roots": items, "current": current.replace(os.sep, "/")})

    @app.route("/api/admin/system-config/upload-roots", methods=["PUT"])
    def api_system_config_upload_roots_update():
        """保存完整存储路径列表；新文件将写入列表最后一个路径。"""
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        roots = data.get("roots") or []
        if not isinstance(roots, list):
            return jsonify({"error": "roots 必须是路径数组"}), 400
        from core.db import DATA_DIR
        cleaned = []
        for p in roots:
            if not isinstance(p, str):
                continue
            p = p.strip()
            if not p:
                continue
            # 允许相对路径：基于数据目录解析为绝对路径
            if not os.path.isabs(p):
                p = os.path.join(DATA_DIR, p)
            p = os.path.abspath(os.path.normpath(p))
            cleaned.append(p)
        if not cleaned:
            cleaned = [get_current_upload_root()]
        # 去重并保持顺序
        seen = set()
        uniq = []
        for p in cleaned:
            if p.lower() not in seen:
                seen.add(p.lower())
                uniq.append(p)
        # 尝试创建每个路径
        for p in uniq:
            try:
                os.makedirs(p, exist_ok=True)
                os.makedirs(os.path.join(p, "overviews"), exist_ok=True)
            except Exception as e:
                return jsonify({"error": "无法创建目录 %s: %s" % (p, e)}), 400
        conn = get_db()
        set_setting(conn, "upload_roots", json.dumps(uniq))
        conn.commit()
        write_audit(u["uid"], "update_upload_roots", json.dumps(uniq))
        return jsonify({"ok": True, "roots": [p.replace(os.sep, "/") for p in uniq], "current": uniq[-1].replace(os.sep, "/")})

    @app.route("/api/admin/system-config/upload-root", methods=["PUT"])
    def api_system_config_upload_root():
        """兼容旧单路径设置：将传入路径追加为新的当前写入路径（若已存在则置顶）。"""
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        from core.db import DATA_DIR
        path = (data.get("path") or "").strip()
        if path:
            if not os.path.isabs(path):
                path = os.path.join(DATA_DIR, path)
            path = os.path.abspath(os.path.normpath(path))
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as e:
                return jsonify({"error": "无法创建目录: %s" % e}), 400
        roots = get_upload_roots()
        norm = os.path.normpath(path) if path else ""
        if norm and norm not in roots:
            roots.append(norm)
        conn = get_db()
        set_setting(conn, "upload_roots", json.dumps(roots))
        conn.commit()
        write_audit(u["uid"], "update_upload_root", path)
        return jsonify({"ok": True})

    @app.route("/api/admin/system-config/open-upload-root", methods=["POST"])
    def api_system_config_open_upload_root():
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        roots = get_upload_roots()
        idx = data.get("index")
        if isinstance(idx, int) and 0 <= idx < len(roots):
            path = roots[idx]
        else:
            path = get_current_upload_root()
        if not os.path.isdir(path):
            return jsonify({"error": "目录不存在: %s" % path}), 400
        try:
            if os.name == "nt":
                os.startfile(path)
            elif os.uname().sysname == "Darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            return jsonify({"error": "无法打开目录: %s" % e}), 500
        return jsonify({"ok": True, "path": path})
