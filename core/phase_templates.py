# -*- coding: utf-8 -*-
"""
阶段模板 API：维护「阶段 → 文档类型」的默认结构
新建零件 / 阶段时依据此处生成文件夹与权限节点。
"""
from flask import request, jsonify
from core.db import get_db
from core.auth import get_request_user
from core.permissions import is_project_admin
from core.audit import write_audit


def _group_templates(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["phase"], []).append(r["doc_type"])
    return groups


def register_phase_template_routes(app):

    @app.route("/api/phase-templates")
    def api_phase_template_list():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        rows = conn.execute(
            "SELECT id, phase, doc_type FROM phase_doc_templates ORDER BY sort_order, phase, doc_type"
        ).fetchall()
        return jsonify(_group_templates(rows))

    @app.route("/api/admin/phase-templates", methods=["POST"])
    def api_phase_template_create():
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        phase = (data.get("phase") or "").strip()
        doc_type = (data.get("doc_type") or "").strip()
        if not phase or not doc_type:
            return jsonify({"error": "阶段与文档类型均必填"}), 400
        conn = get_db()
        try:
            # 新增行继承同阶段现有 sort_order，保证阶段内所有 doc_type 排序一致
            so_row = conn.execute(
                """SELECT MIN(CASE WHEN COALESCE(sort_order,0)=0 THEN NULL ELSE sort_order END) AS so
                   FROM phase_doc_templates WHERE phase=?""",
                (phase,),
            ).fetchone()
            sort_order = so_row["so"]
            DEFAULT_PHASE_ORDER = {
                "Tooling": 10, "Fixture": 20,
                "P1": 30, "P2": 40, "EVT": 50, "DVT": 60, "PVT": 70,
                "MP": 80,
            }
            if sort_order is None:
                sort_order = DEFAULT_PHASE_ORDER.get(phase)
            if sort_order is None:
                max_so = conn.execute(
                    "SELECT COALESCE(MAX(sort_order),0) FROM phase_doc_templates"
                ).fetchone()[0]
                sort_order = max_so + 10
            cur = conn.execute(
                """INSERT INTO phase_doc_templates
                       (phase, doc_type, sort_order, created_by, created_at)
                   VALUES (?,?,?,?,datetime('now','localtime'))""",
                (phase, doc_type, sort_order, u["uid"]),
            )
            # 顺手把同阶段其它可能为 0 的行也统一成该 sort_order，保持归一
            conn.execute(
                "UPDATE phase_doc_templates SET sort_order=? WHERE phase=? AND COALESCE(sort_order,0)=0",
                (sort_order, phase),
            )
            conn.commit()
            write_audit(u["uid"], "create_phase_template", "%s/%s" % (phase, doc_type))
            return jsonify({"ok": True, "id": cur.lastrowid})
        except Exception:
            return jsonify({"error": "该阶段下已存在同名文档类型"}), 409

    @app.route("/api/admin/phase-templates", methods=["DELETE"])
    def api_phase_template_delete():
        u = get_request_user()
        if not u or u["role"] != "superAdmin":
            return jsonify({"error": "需要系统管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        phase = (data.get("phase") or "").strip()
        doc_type = (data.get("doc_type") or "").strip()
        if not phase or not doc_type:
            return jsonify({"error": "阶段与文档类型均必填"}), 400
        conn = get_db()
        conn.execute(
            "DELETE FROM phase_doc_templates WHERE phase=? AND doc_type=?",
            (phase, doc_type),
        )
        conn.commit()
        write_audit(u["uid"], "delete_phase_template", "%s/%s" % (phase, doc_type))
        return jsonify({"ok": True})
