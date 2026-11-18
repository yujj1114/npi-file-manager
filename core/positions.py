# -*- coding: utf-8 -*-
"""
职位管理 API（仅 superAdmin 可维护）
"""
from flask import request, jsonify
from core.db import get_db
from core.auth import get_request_user, is_admin
from core.audit import write_audit


def register_position_routes(app):

    @app.route("/api/admin/positions")
    def api_positions_list():
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        conn = get_db()
        rows = conn.execute(
            "SELECT id, name, description, active, created_at FROM positions ORDER BY id"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/admin/positions", methods=["POST"])
    def api_position_create():
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        description = (data.get("description") or "").strip()
        if not name:
            return jsonify({"error": "职位名称必填"}), 400
        conn = get_db()
        try:
            cur = conn.execute(
                """INSERT INTO positions (name, description, active, created_at)
                   VALUES (?,?,1,datetime('now','localtime'))""",
                (name, description),
            )
            conn.commit()
            write_audit(u["uid"], "create_position", name)
            return jsonify({"ok": True, "id": cur.lastrowid})
        except Exception:
            return jsonify({"error": "职位名称已存在"}), 409

    @app.route("/api/admin/positions/<int:pid>", methods=["PUT"])
    def api_position_update(pid):
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        description = (data.get("description") or "").strip()
        if not name:
            return jsonify({"error": "职位名称必填"}), 400
        conn = get_db()
        try:
            conn.execute(
                "UPDATE positions SET name=?, description=? WHERE id=?",
                (name, description, pid),
            )
            conn.commit()
            write_audit(u["uid"], "update_position", "id=%s name=%s" % (pid, name))
            return jsonify({"ok": True})
        except Exception:
            return jsonify({"error": "职位名称已存在"}), 409

    @app.route("/api/admin/positions/<int:pid>/toggle", methods=["POST"])
    def api_position_toggle(pid):
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        conn = get_db()
        r = conn.execute("SELECT active, name FROM positions WHERE id=?", (pid,)).fetchone()
        if not r:
            return jsonify({"error": "职位不存在"}), 404
        new_state = 0 if r["active"] else 1
        conn.execute("UPDATE positions SET active=? WHERE id=?", (new_state, pid))
        conn.commit()
        write_audit(u["uid"], "toggle_position", "id=%s name=%s active=%s" % (pid, r["name"], new_state))
        return jsonify({"ok": True, "active": new_state})

    @app.route("/api/admin/positions/<int:pid>", methods=["DELETE"])
    def api_position_delete(pid):
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        conn = get_db()
        r = conn.execute("SELECT name FROM positions WHERE id=?", (pid,)).fetchone()
        if not r:
            return jsonify({"error": "职位不存在"}), 404
        # 若已有用户关联该职位，拒绝删除并提示
        used = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE position_id=?", (pid,)
        ).fetchone()["c"]
        if used:
            return jsonify({"error": "该职位已被 %s 位用户关联，无法删除，请先调整用户职位" % used}), 409
        conn.execute("DELETE FROM positions WHERE id=?", (pid,))
        conn.commit()
        write_audit(u["uid"], "delete_position", "id=%s name=%s" % (pid, r["name"]))
        return jsonify({"ok": True})
