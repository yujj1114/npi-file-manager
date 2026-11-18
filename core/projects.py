# -*- coding: utf-8 -*-
"""
项目 / 零件 / 阶段 / 文档类型 API（复刻主服务核心）
"""
import json
import os
import re
import shutil

from flask import request, jsonify
from core.db import get_db, get_upload_roots, locate_file, locate_overview
from core.auth import get_request_user, is_admin
from core.permissions import get_user_project_role, get_visible_projects, is_project_admin, can_access_project, can_edit_project_content
from core.audit import write_audit

# 零件信息卡默认字段（当项目未自定义字段时回退使用）
# 注：NO 是 SQLite 关键字，故列名为 no_
DEFAULT_PART_INFO_FIELDS = [
    {"key": "no_", "label": "NO.", "sort_order": 0},
    {"key": "rev", "label": "REV", "sort_order": 1},
    {"key": "model", "label": "模号", "sort_order": 2},
    {"key": "description", "label": "Part description", "sort_order": 3},
    {"key": "process", "label": "制程", "sort_order": 4},
    {"key": "pd", "label": "PD", "sort_order": 5},
    {"key": "dqe", "label": "DQE", "sort_order": 6},
    {"key": "stamp_loc", "label": "冲压地点", "sort_order": 7},
]

_KEY_RE = re.compile(r"^[a-zA-Z0-9_]{1,40}$")


def _get_part_info_fields(conn, project_id):
    """读取项目自定义字段；若无配置则返回默认 8 字段。"""
    rows = conn.execute(
        "SELECT field_key, label, sort_order FROM part_info_fields "
        "WHERE project_id=? ORDER BY sort_order, field_key",
        (project_id,),
    ).fetchall()
    if rows:
        return [
            {"key": r["field_key"], "label": r["label"], "sort_order": r["sort_order"]}
            for r in rows
        ]
    return list(DEFAULT_PART_INFO_FIELDS)


def _proj_with_counts(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
    hidden_col = ", hidden" if "hidden" in cols else ""
    projects = conn.execute(
        "SELECT id, project_no, created_by, created_at%s FROM projects ORDER BY project_no" % hidden_col
    ).fetchall()
    parts = conn.execute(
        "SELECT id, project_id, name FROM parts ORDER BY project_id, name"
    ).fetchall()
    counts = conn.execute(
        "SELECT project_id, part_id, COUNT(*) AS c FROM files GROUP BY project_id, part_id"
    ).fetchall()
    proj_total = {}
    part_count = {}
    for r in counts:
        proj_total[r["project_id"]] = proj_total.get(r["project_id"], 0) + r["c"]
        if r["part_id"] is not None:
            part_count[(r["project_id"], r["part_id"])] = r["c"]
    # 零件下的阶段数（用于目录状态：空目录判定）
    phase_rows = conn.execute(
        "SELECT part_id, COUNT(*) AS c FROM part_phases GROUP BY part_id"
    ).fetchall()
    phase_count = {r["part_id"]: r["c"] for r in phase_rows}
    # 零件实际拥有的阶段列表（按 sort_order 排序，供树形仅显示已选阶段）
    pp_rows = conn.execute(
        """SELECT pp.part_id, pp.phase, MIN(COALESCE(t.sort_order, 0)) AS so
           FROM part_phases pp
           LEFT JOIN phase_doc_templates t ON t.phase = pp.phase
           GROUP BY pp.part_id, pp.phase
           ORDER BY pp.part_id, so, pp.phase"""
    ).fetchall()
    part_phases = {}
    for r in pp_rows:
        part_phases.setdefault(r["part_id"], []).append(r["phase"])
    # 每个（project_id, part_id, phase, doc_type）的文件计数（用于树节点角标）
    dt_rows = conn.execute(
        "SELECT project_id, part_id, phase, doc_type, COUNT(*) AS c FROM files "
        "GROUP BY project_id, part_id, phase, doc_type"
    ).fetchall()
    dt_count = {}
    for r in dt_rows:
        if r["part_id"] is None:
            continue
        dt_count[(r["project_id"], r["part_id"], r["phase"], r["doc_type"])] = r["c"]
    part_by_proj = {}
    for pt in parts:
        d = dict(pt)
        d["file_count"] = part_count.get((pt["project_id"], pt["id"]), 0)
        d["phase_count"] = phase_count.get(pt["id"], 0)
        d["phases"] = part_phases.get(pt["id"], [])
        doc_types = []
        for (ppid, ptid, ph, dt), c in dt_count.items():
            if ppid == pt["project_id"] and ptid == pt["id"]:
                doc_types.append({"phase": ph, "doc_type": dt, "file_count": c})
        d["doc_types"] = doc_types
        part_by_proj.setdefault(pt["project_id"], []).append(d)
    out = []
    for p in projects:
        d = dict(p)
        d["hidden"] = int(d.get("hidden") or 0)
        d["file_count"] = proj_total.get(p["id"], 0)
        d["part_count"] = len(part_by_proj.get(p["id"], []))
        d["parts"] = part_by_proj.get(p["id"], [])
        out.append(d)
    return out


def _seed_custom_doc_types(conn, project_id, part_id, phases, created_by):
    """根据阶段模板，把 (phase, doc_type) 快照到零件的 custom_doc_types。
    保证新建零件不依赖全局模板实时渲染，已有零件独立。"""
    if not phases:
        return
    placeholders = ",".join("?" * len(phases))
    rows = conn.execute(
        "SELECT phase, doc_type FROM phase_doc_templates WHERE phase IN (%s)" % placeholders,
        tuple(phases),
    ).fetchall()
    for r in rows:
        if not conn.execute(
            "SELECT 1 FROM custom_doc_types WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (project_id, part_id, r["phase"], r["doc_type"]),
        ).fetchone():
            conn.execute(
                """INSERT INTO custom_doc_types (project_id, part_id, phase, doc_type, created_by, created_at)
                   VALUES (?,?,?,?,?,datetime('now','localtime'))""",
                (project_id, part_id, r["phase"], r["doc_type"], created_by),
            )


def register_project_routes(app):

    @app.route("/api/projects")
    def api_projects():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        visible = get_visible_projects(u["uid"])
        all_projects = _proj_with_counts(conn)
        # superAdmin 看到全部；其他仅可见项目，且隐藏项目对普通用户不可见
        if u["role"] == "superAdmin":
            out = all_projects
        else:
            if u["role"] == "projectAdmin":
                admin_pids = {p["id"] for p in all_projects}
            else:
                rows = conn.execute(
                    "SELECT project_id FROM project_permissions WHERE user_id=? "
                    "AND role_in_project IN ('projectAdmin','superAdmin')",
                    (u["uid"],),
                ).fetchall()
                admin_pids = {r["project_id"] for r in rows}
            out = [
                p for p in all_projects
                if p["id"] in visible and (not p["hidden"] or p["id"] in admin_pids)
            ]
        for p in out:
            p["canManage"] = (u["role"] == "superAdmin") or (p["id"] in visible and
                           get_user_project_role(u["uid"], p["id"]) in ("superAdmin", "projectAdmin"))
        return jsonify(out)

    @app.route("/api/projects/<int:pid>/parts")
    def api_project_parts(pid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if not can_access_project(u["uid"], pid):
            return jsonify({"error": "无权访问该项目"}), 403
        conn = get_db()
        parts = conn.execute(
            "SELECT id, project_id, name FROM parts WHERE project_id=? ORDER BY name", (pid,)
        ).fetchall()
        return jsonify([dict(p) for p in parts])

    @app.route("/api/projects/<int:pid>/phase-tree")
    def api_project_phase_tree(pid):
        """返回项目实际已有的阶段及对应文档类型（基于零件 part_phases + phase_doc_templates）。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if not can_access_project(u["uid"], pid):
            return jsonify({"error": "无权访问该项目"}), 403
        conn = get_db()
        # 取该项目下所有零件实际存在的阶段，并按阶段模板 sort_order 排序
        phase_rows = conn.execute(
            """SELECT pp.phase,
                      MIN(CASE WHEN COALESCE(t.sort_order,0)=0 THEN NULL ELSE t.sort_order END) AS so
               FROM part_phases pp
               JOIN parts pt ON pt.id = pp.part_id
               LEFT JOIN phase_doc_templates t ON t.phase = pp.phase
               WHERE pt.project_id = ?
               GROUP BY pp.phase
               ORDER BY CASE WHEN MIN(CASE WHEN COALESCE(t.sort_order,0)=0 THEN NULL ELSE t.sort_order END) IS NULL THEN 1 ELSE 0 END,
                        MIN(CASE WHEN COALESCE(t.sort_order,0)=0 THEN NULL ELSE t.sort_order END),
                        pp.phase""",
            (pid,),
        ).fetchall()
        tree = {}
        for r in phase_rows:
            ph = r["phase"]
            dts = [
                dt["doc_type"]
                for dt in conn.execute(
                    """SELECT DISTINCT doc_type, COALESCE(sort_order, 0) AS so
                       FROM phase_doc_templates
                       WHERE phase = ?
                       ORDER BY so, doc_type""",
                    (ph,),
                ).fetchall()
            ]
            tree[ph] = dts
        return jsonify(tree)

    @app.route("/api/parts/<int:ptid>")
    def api_part_detail(ptid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute("SELECT * FROM parts WHERE id=?", (ptid,)).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if not can_access_project(u["uid"], pt["project_id"]):
            return jsonify({"error": "无权访问该项目"}), 403
        return jsonify(dict(pt))

    @app.route("/api/projects/<int:pid>/info_fields")
    def api_project_info_fields_get(pid):
        """项目信息卡字段配置：项目成员可查看。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if not can_access_project(u["uid"], pid):
            return jsonify({"error": "无权访问该项目"}), 403
        conn = get_db()
        fields = _get_part_info_fields(conn, pid)
        return jsonify(fields)

    @app.route("/api/projects/<int:pid>/info_fields", methods=["PUT"])
    def api_project_info_fields_put(pid):
        """保存项目信息卡字段配置：系统管理员 / 项目管理员可维护。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要项目管理员权限"}), 403
        payload = request.get_json(silent=True) or []
        if not isinstance(payload, list):
            return jsonify({"error": "字段列表应为数组"}), 400

        cleaned = []
        seen = set()
        for idx, item in enumerate(payload):
            key = (item.get("key") or "").strip()
            label = (item.get("label") or "").strip()
            if not key or not label:
                return jsonify({"error": "第 %d 项字段 key/label 不能为空" % (idx + 1)}), 400
            if not _KEY_RE.match(key):
                return jsonify(
                    {"error": "第 %d 项字段 key 只能包含字母/数字/下划线" % (idx + 1)}
                ), 400
            if key in seen:
                return jsonify({"error": "字段 key '%s' 重复" % key}), 400
            seen.add(key)
            try:
                sort = int(item.get("sort_order", idx))
            except Exception:
                sort = idx
            cleaned.append((pid, key, label, sort))

        conn = get_db()
        conn.execute("DELETE FROM part_info_fields WHERE project_id=?", (pid,))
        conn.executemany(
            "INSERT INTO part_info_fields (project_id, field_key, label, sort_order) VALUES (?,?,?,?)",
            cleaned,
        )
        conn.commit()
        write_audit(u["uid"], "update_part_info_fields", "project %s" % pid)
        return jsonify({"ok": True, "count": len(cleaned)})

    @app.route("/api/parts/<int:ptid>/info")
    def api_part_info_get(ptid):
        """零件信息卡：项目成员可查看，返回 data + _canEdit 供前端决定只读/可编辑。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute(
            "SELECT project_id, name FROM parts WHERE id=?", (ptid,)
        ).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if not can_access_project(u["uid"], pt["project_id"]):
            return jsonify({"error": "无权访问该项目"}), 403
        fields = _get_part_info_fields(conn, pt["project_id"])
        allowed = {f["key"] for f in fields}
        out = {"data": {k: "" for k in allowed}, "updated_by": "", "updated_by_name": "", "updated_at": ""}
        row = conn.execute(
            "SELECT pi.data, pi.updated_by, u.name AS updated_by_name, pi.updated_at "
            "FROM part_info pi LEFT JOIN users u ON pi.updated_by=u.uid WHERE pi.part_id=?", (ptid,)
        ).fetchone()
        if row:
            stored = {}
            try:
                stored = json.loads(row["data"] or "{}") if row["data"] else {}
                if not isinstance(stored, dict):
                    stored = {}
            except Exception:
                stored = {}
            for k in allowed:
                v = stored.get(k)
                out["data"][k] = "" if v is None else str(v)
            out["updated_by"] = row["updated_by"] or ""
            out["updated_by_name"] = row["updated_by_name"] or ""
            out["updated_at"] = row["updated_at"] or ""
        out["_canEdit"] = can_edit_project_content(u["uid"], pt["project_id"])
        return jsonify(out)

    @app.route("/api/parts/<int:ptid>/info", methods=["PUT"])
    def api_part_info_put(ptid):
        """保存零件信息卡：项目/系统管理员或本人在该项目任一文件夹可编辑的成员可写，
        仅接受该项目配置的字段。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute("SELECT project_id, name FROM parts WHERE id=?", (ptid,)).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if not can_edit_project_content(u["uid"], pt["project_id"]):
            return jsonify({"error": "无该项目内容编辑权限"}), 403

        fields = _get_part_info_fields(conn, pt["project_id"])
        allowed = {f["key"] for f in fields}

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict) or "data" not in payload:
            return jsonify({"error": "请求体需包含 data 对象"}), 400
        incoming = payload["data"]

        def _clean(v):
            v = "" if v is None else str(v).strip()
            return v[:500]

        row = conn.execute(
            "SELECT data FROM part_info WHERE part_id=?", (ptid,)
        ).fetchone()
        stored = {}
        if row and row["data"]:
            try:
                stored = json.loads(row["data"])
                if not isinstance(stored, dict):
                    stored = {}
            except Exception:
                stored = {}
        # 仅覆盖本次提交的允许字段，未提交字段保持原值
        for k in allowed:
            if k in incoming:
                stored[k] = _clean(incoming[k])
        data_json = json.dumps(stored, ensure_ascii=False)

        if row:
            conn.execute(
                "UPDATE part_info SET data=?, updated_by=?, updated_at=datetime('now','localtime') "
                "WHERE part_id=?",
                (data_json, u["uid"], ptid),
            )
        else:
            conn.execute(
                "INSERT INTO part_info (part_id, data, updated_by, updated_at) "
                "VALUES (?,?,?,datetime('now','localtime'))",
                (ptid, data_json, u["uid"]),
            )
        conn.commit()
        write_audit(u["uid"], "update_part_info", "part %s (%s)" % (ptid, pt["name"]))
        return jsonify({"ok": True})

    @app.route("/api/parts/<int:ptid>/phases")
    def api_part_phases(ptid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute("SELECT project_id FROM parts WHERE id=?", (ptid,)).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if not can_access_project(u["uid"], pt["project_id"]):
            return jsonify({"error": "无权访问该项目"}), 403
        rows = conn.execute(
            """SELECT pp.phase FROM part_phases pp
               LEFT JOIN phase_doc_templates t ON t.phase = pp.phase
               WHERE pp.part_id=?
               GROUP BY pp.phase
               ORDER BY MIN(COALESCE(t.sort_order, 0)), pp.phase""", (ptid,)
        ).fetchall()
        return jsonify([r["phase"] for r in rows])

    @app.route("/api/phase-docs")
    def api_phase_docs():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        pid = request.args.get("project_id")
        ptid = request.args.get("part_id")
        phase = request.args.get("phase")
        conn = get_db()
        # 仅返回当前用户可访问项目的自定义文档类型，避免越权枚举其他项目配置
        visible = get_visible_projects(u["uid"])
        # 阶段文档类型模板
        tmpl = conn.execute(
            "SELECT phase, doc_type FROM phase_doc_templates ORDER BY sort_order, phase, doc_type"
        ).fetchall()
        # 自定义文档类型（需包含 id，前端在右键菜单使用）
        custom_rows = conn.execute(
            "SELECT id, project_id, part_id, phase, doc_type FROM custom_doc_types"
        ).fetchall()
        custom = [dict(r) for r in custom_rows
                  if (r["project_id"] in visible) or (r["project_id"] is None)]
        return jsonify({
            "templates": [dict(r) for r in tmpl],
            "custom": custom,
        })

    # ---------- 自定义文档类型 ----------
    # 注：/api/phase-templates（返回 {phase:[doc_types]} 分组结构）由
    # core/phase_templates.py 统一提供，此处不再重复注册，避免路由冲突。
    @app.route("/api/custom-doc-types", methods=["POST"])
    def api_custom_doc_type_create():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        pid = data.get("project_id")
        if not pid:
            return jsonify({"error": "缺少 project_id"}), 400
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], int(pid)):
            return jsonify({"error": "需要项目管理员权限"}), 403
        ptid = data.get("part_id")
        phase = (data.get("phase") or "").strip()
        doc_type = (data.get("doc_type") or "").strip()
        if not phase or not doc_type:
            return jsonify({"error": "阶段与文档类型均必填"}), 400
        conn = get_db()
        conn.execute(
            """INSERT INTO custom_doc_types (project_id, part_id, phase, doc_type, created_by, created_at)
               VALUES (?,?,?,?,?,datetime('now','localtime'))""",
            (pid, ptid, phase, doc_type, u["uid"]),
        )
        conn.commit()
        write_audit(u["uid"], "create_doc_type", "phase=%s doc=%s" % (phase, doc_type))
        return jsonify({"ok": True})

    @app.route("/api/custom-doc-types/<int:ctid>", methods=["DELETE"])
    def api_custom_doc_type_delete(ctid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        row = conn.execute("SELECT project_id FROM custom_doc_types WHERE id=?", (ctid,)).fetchone()
        if not row:
            return jsonify({"error": "记录不存在"}), 404
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], row["project_id"]):
            return jsonify({"error": "需要项目管理员权限"}), 403
        conn.execute("DELETE FROM custom_doc_types WHERE id=?", (ctid,))
        conn.commit()
        write_audit(u["uid"], "delete_doc_type", "id=%s" % ctid)
        return jsonify({"ok": True})

    @app.route("/api/custom-doc-types/<int:ctid>", methods=["PUT"])
    def api_custom_doc_type_rename(ctid):
        """重命名自定义子文件夹（文档类型）。

        会级联同步 files / folder_permissions 中的 doc_type 字段，
        保证已上传文件与权限记录继续归属新名称。
        """
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        row = conn.execute(
            "SELECT project_id, part_id, phase, doc_type FROM custom_doc_types WHERE id=?", (ctid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "记录不存在"}), 404
        pid = row["project_id"]
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要项目管理员权限"}), 403

        data = request.get_json(silent=True) or {}
        new_dt = (data.get("doc_type") or "").strip()
        if not new_dt:
            return jsonify({"error": "新文件夹名称不能为空"}), 400
        if new_dt == row["doc_type"]:
            return jsonify({"ok": True})

        # 避免与同阶段下其他名称重复（含标准模板中的名称）
        existing = conn.execute(
            "SELECT 1 FROM custom_doc_types WHERE project_id=? AND part_id=? AND phase=? AND doc_type=? AND id!=?",
            (pid, row["part_id"], row["phase"], new_dt, ctid),
        ).fetchone()
        if not existing:
            existing = conn.execute(
                "SELECT 1 FROM phase_doc_templates WHERE phase=? AND doc_type=?",
                (row["phase"], new_dt),
            ).fetchone()
        if existing:
            return jsonify({"error": "该阶段下已存在同名文件夹"}), 409

        # 同步更新三处关联数据
        conn.execute(
            "UPDATE custom_doc_types SET doc_type=? WHERE id=?",
            (new_dt, ctid),
        )
        conn.execute(
            "UPDATE files SET doc_type=? WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (new_dt, pid, row["part_id"], row["phase"], row["doc_type"]),
        )
        conn.execute(
            "UPDATE folder_permissions SET doc_type=? WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (new_dt, pid, row["part_id"], row["phase"], row["doc_type"]),
        )
        conn.commit()
        write_audit(
            u["uid"], "rename_doc_type",
            "phase=%s old=%s new=%s" % (row["phase"], row["doc_type"], new_dt),
        )
        return jsonify({"ok": True})

    @app.route("/api/custom-doc-types/rename", methods=["PUT"])
    def api_custom_doc_type_rename_by_key():
        """重命名项目/零件/阶段下的子文件夹（支持标准模板文件夹）。

        标准模板文件夹在 custom_doc_types 中可能不存在记录；重命名时会自动
        插入一条项目级自定义记录，并把 files / folder_permissions 中的旧
        doc_type 迁移到新名称。
        """
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        pid = data.get("project_id")
        ptid = data.get("part_id")
        phase = (data.get("phase") or "").strip()
        old_dt = (data.get("old_doc_type") or "").strip()
        new_dt = (data.get("new_doc_type") or "").strip()
        if not pid or not phase or not old_dt:
            return jsonify({"error": "缺少 project_id / phase / old_doc_type"}), 400
        if not new_dt:
            return jsonify({"error": "新文件夹名称不能为空"}), 400
        if new_dt == old_dt:
            return jsonify({"ok": True})
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], int(pid)):
            return jsonify({"error": "需要项目管理员权限"}), 403

        conn = get_db()
        # 检查是否已有其他自定义记录占用新名称
        existing_custom = conn.execute(
            "SELECT 1 FROM custom_doc_types WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (pid, ptid, phase, new_dt),
        ).fetchone()
        if existing_custom:
            return jsonify({"error": "该阶段下已存在同名文件夹"}), 409

        old_in_template = conn.execute(
            "SELECT 1 FROM phase_doc_templates WHERE phase=? AND doc_type=?",
            (phase, old_dt),
        ).fetchone()
        new_in_template = conn.execute(
            "SELECT 1 FROM phase_doc_templates WHERE phase=? AND doc_type=?",
            (phase, new_dt),
        ).fetchone()
        # 两个都是标准模板名时，会生成同名标准节点冲突，禁止
        if old_in_template and new_in_template:
            return jsonify({"error": "不能直接将一个标准文件夹重命名为另一个标准文件夹"}), 409

        # 迁移 files / folder_permissions
        conn.execute(
            "UPDATE files SET doc_type=? WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (new_dt, pid, ptid, phase, old_dt),
        )
        conn.execute(
            "UPDATE folder_permissions SET doc_type=? WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (new_dt, pid, ptid, phase, old_dt),
        )

        # 维护 custom_doc_types：目标是自定义名则保留/创建记录；目标是标准名则删除自定义映射
        row = conn.execute(
            "SELECT id FROM custom_doc_types WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (pid, ptid, phase, old_dt),
        ).fetchone()
        if new_in_template:
            # 恢复为标准模板：删除旧的自定义映射
            if row:
                conn.execute("DELETE FROM custom_doc_types WHERE id=?", (row["id"],))
        else:
            # 自定义名：查找或创建记录
            if row:
                conn.execute(
                    "UPDATE custom_doc_types SET doc_type=? WHERE id=?",
                    (new_dt, row["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO custom_doc_types (project_id, part_id, phase, doc_type, created_by, created_at)
                       VALUES (?,?,?,?,?,datetime('now','localtime'))""",
                    (pid, ptid, phase, new_dt, u["uid"]),
                )
        conn.commit()
        write_audit(
            u["uid"], "rename_doc_type",
            "project=%s part=%s phase=%s old=%s new=%s" % (pid, ptid, phase, old_dt, new_dt),
        )
        return jsonify({"ok": True})

    @app.route("/api/custom-doc-types/delete", methods=["POST"])
    def api_custom_doc_type_delete_by_key():
        """删除项目/零件/阶段下的子文件夹（支持标准模板文件夹）。

        会同步删除该文件夹下的所有文件（含物理文件）、文件夹权限记录以及
        custom_doc_types 自定义映射。
        """
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        data = request.get_json(silent=True) or {}
        pid = data.get("project_id")
        ptid = data.get("part_id")
        phase = (data.get("phase") or "").strip()
        doc_type = (data.get("doc_type") or "").strip()
        if not pid or not phase or not doc_type:
            return jsonify({"error": "缺少 project_id / phase / doc_type"}), 400
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], int(pid)):
            return jsonify({"error": "需要项目管理员权限"}), 403

        conn = get_db()
        # 获取待删除文件列表，用于物理删除
        files = conn.execute(
            "SELECT id, project_id, part_id, phase, doc_type, storage_name, storage_root FROM files "
            "WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (pid, ptid, phase, doc_type),
        ).fetchall()
        proj_no = conn.execute("SELECT project_no FROM projects WHERE id=?", (pid,)).fetchone()
        proj_no = proj_no["project_no"] if proj_no else str(pid)
        for f in files:
            path = locate_file(proj_no, f["storage_name"], f["storage_root"] if "storage_root" in f.keys() else None)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        # 删除数据库记录
        conn.execute(
            "DELETE FROM files WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (pid, ptid, phase, doc_type),
        )
        conn.execute(
            "DELETE FROM folder_permissions WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (pid, ptid, phase, doc_type),
        )
        conn.execute(
            "DELETE FROM custom_doc_types WHERE project_id=? AND part_id=? AND phase=? AND doc_type=?",
            (pid, ptid, phase, doc_type),
        )
        conn.commit()
        write_audit(
            u["uid"], "delete_doc_type",
            "project=%s part=%s phase=%s doc=%s files=%s" % (pid, ptid, phase, doc_type, len(files)),
        )
        return jsonify({"ok": True})

    # ---------- 项目 / 零件 / 阶段 CRUD（仅项目管理员/系统管理员） ----------
    @app.route("/api/projects", methods=["POST"])
    def api_project_create():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        # 新建项目：系统管理员或项目管理员均可创建；创建者默认成为该项目的项目管理员
        if u["role"] not in ("superAdmin", "projectAdmin"):
            return jsonify({"error": "仅系统管理员或项目管理员可创建新项目"}), 403
        data = request.get_json(silent=True) or {}
        project_no = (data.get("project_no") or "").strip().upper()
        if not project_no:
            return jsonify({"error": "项目编号必填"}), 400
        parts_raw = data.get("parts") or []
        phases = data.get("phases") or []
        # parts 支持列表或 \n 分隔字符串
        if isinstance(parts_raw, str):
            parts = [p.strip() for p in parts_raw.splitlines() if p.strip()]
        else:
            parts = [str(p).strip() for p in parts_raw if str(p).strip()]
        if not parts:
            return jsonify({"error": "至少需要填写一个零件名称"}), 400
        if not phases:
            return jsonify({"error": "至少需要选择一个 NPI 阶段"}), 400
        conn = get_db()
        if conn.execute("SELECT 1 FROM projects WHERE project_no=?", (project_no,)).fetchone():
            return jsonify({"error": "项目编号已存在"}), 409
        try:
            cur = conn.execute(
                "INSERT INTO projects (project_no, created_by, created_at) VALUES (?,?,datetime('now','localtime'))",
                (project_no, u["uid"]),
            )
            pid = cur.lastrowid
            # 批量创建零件与阶段
            for part_name in parts:
                if conn.execute("SELECT 1 FROM parts WHERE project_id=? AND name=?", (pid, part_name)).fetchone():
                    continue
                pcur = conn.execute("INSERT INTO parts (project_id, name) VALUES (?,?)", (pid, part_name))
                ptid = pcur.lastrowid
                for ph in phases:
                    if not conn.execute(
                        "SELECT 1 FROM part_phases WHERE part_id=? AND phase=?", (ptid, ph)
                    ).fetchone():
                        conn.execute("INSERT INTO part_phases (part_id, phase) VALUES (?,?)", (ptid, ph))
                _seed_custom_doc_types(conn, pid, ptid, phases, u["uid"])
            # 所有角色为 projectAdmin 的用户默认自动成为该项目的项目管理员
            # superAdmin 已有全局管理权限，无需写入 project_permissions 表
            pa_users = conn.execute("SELECT uid FROM users WHERE role='projectAdmin'").fetchall()
            for pa in pa_users:
                conn.execute(
                    "INSERT OR IGNORE INTO project_permissions (project_id, user_id, role_in_project) VALUES (?,?,?)",
                    (pid, pa["uid"], "projectAdmin"),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            return jsonify({"error": "创建失败：" + str(e)}), 500
        write_audit(u["uid"], "create_project", "%s parts=%s phases=%s" % (project_no, len(parts), len(phases)))
        return jsonify({"ok": True, "id": pid})

    @app.route("/api/projects/<int:pid>", methods=["DELETE"])
    def api_project_delete(pid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要项目管理员权限"}), 403
        conn = get_db()
        proj = conn.execute("SELECT project_no FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj:
            return jsonify({"error": "项目不存在"}), 404
        # 级联清理：先删子表，再删主表，避免产生孤儿记录与悬空文件
        conn.execute(
            "DELETE FROM part_phases WHERE part_id IN (SELECT id FROM parts WHERE project_id=?)",
            (pid,),
        )
        conn.execute(
            "DELETE FROM part_info WHERE part_id IN (SELECT id FROM parts WHERE project_id=?)",
            (pid,),
        )
        conn.execute("DELETE FROM files WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM custom_doc_types WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM folder_permissions WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM project_permissions WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM parts WHERE project_id=?", (pid,))
        conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        conn.commit()
        # 清理磁盘所有存储路径下的 <project_no> 目录
        for root in get_upload_roots():
            proj_dir = os.path.join(root, proj["project_no"])
            if os.path.isdir(proj_dir):
                try:
                    shutil.rmtree(proj_dir)
                except Exception as _e:
                    write_audit(u["uid"], "delete_project_warn",
                                "清理目录失败 %s: %s" % (proj_dir, _e))
        write_audit(u["uid"], "delete_project", "id=%s %s" % (pid, proj["project_no"]))
        return jsonify({"ok": True})

    @app.route("/api/projects/<int:pid>/parts", methods=["POST"])
    def api_part_create(pid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要项目管理员权限"}), 403
        data = request.get_json(silent=True) or {}

        # 支持批量：优先 names 数组；兼容旧版单 name
        names = []
        if isinstance(data.get("names"), list):
            names = [str(n).strip() for n in data["names"] if str(n).strip()]
        elif isinstance(data.get("name"), str):
            names = [data["name"].strip()]
        if not names:
            return jsonify({"error": "零件名称必填"}), 400
        if len(names) > 50:
            return jsonify({"error": "一次最多创建 50 个零件"}), 400

        conn = get_db()
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
            return jsonify({"error": "项目不存在"}), 404

        # 检查重复
        existing = [r["name"] for r in conn.execute(
            "SELECT name FROM parts WHERE project_id=? AND name IN (%s)" % ",".join(["?"] * len(names)),
            (pid,) + tuple(names),
        ).fetchall()]
        if existing:
            return jsonify({"error": "该项目下已存在同名零件: %s" % ", ".join(existing)}), 409

        # 阶段选择
        requested_phases = data.get("phases")
        if isinstance(requested_phases, list):
            phases = [str(p).strip() for p in requested_phases if str(p).strip()]
            if not phases:
                return jsonify({"error": "请至少选择一个 NPI 阶段"}), 400
        else:
            phases = [r["phase"] for r in conn.execute(
                "SELECT DISTINCT phase FROM phase_doc_templates ORDER BY sort_order, phase"
            ).fetchall()]
        if not phases:
            return jsonify({"error": "当前没有可用的 NPI 阶段，请先配置阶段模板"}), 400

        created_ids = []
        for name in names:
            cur = conn.execute(
                "INSERT INTO parts (project_id, name) VALUES (?,?)",
                (pid, name),
            )
            ptid = cur.lastrowid
            created_ids.append(ptid)
            for ph in phases:
                if not conn.execute(
                    "SELECT 1 FROM part_phases WHERE part_id=? AND phase=?", (ptid, ph)
                ).fetchone():
                    conn.execute("INSERT INTO part_phases (part_id, phase) VALUES (?,?)", (ptid, ph))
            _seed_custom_doc_types(conn, pid, ptid, phases, u["uid"])
            write_audit(u["uid"], "create_part", "project %s part %s phases=%s" % (pid, name, ",".join(phases)))
        conn.commit()
        return jsonify({"ok": True, "created": len(created_ids), "ids": created_ids})

    @app.route("/api/parts/<int:ptid>", methods=["DELETE"])
    def api_part_delete(ptid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute("SELECT project_id, name FROM parts WHERE id=?", (ptid,)).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pt["project_id"]):
            return jsonify({"error": "需要项目管理员权限"}), 403
        # 级联清理：先删子表与磁盘文件，再删主表，避免产生孤儿记录与悬空文件
        proj_no = conn.execute("SELECT project_no FROM projects WHERE id=?", (pt["project_id"],)).fetchone()["project_no"]
        # 1) 删除该零件下的文件及磁盘实体
        for r in conn.execute("SELECT storage_name, storage_root FROM files WHERE part_id=?", (ptid,)).fetchall():
            try:
                p = locate_file(proj_no, r["storage_name"], r["storage_root"] if "storage_root" in r.keys() else None)
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        conn.execute("DELETE FROM files WHERE part_id=?", (ptid,))
        # 2) 删除零件级概览图及磁盘实体
        for r in conn.execute("SELECT storage_name, storage_root FROM project_overviews WHERE project_id=? AND part_id=?", (pt["project_id"], ptid)).fetchall():
            try:
                p = locate_overview(r["storage_name"], r["storage_root"] if "storage_root" in r.keys() else None)
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        conn.execute("DELETE FROM project_overviews WHERE project_id=? AND part_id=?", (pt["project_id"], ptid))
        # 3) 清理其他子表
        conn.execute("DELETE FROM part_phases WHERE part_id=?", (ptid,))
        conn.execute("DELETE FROM folder_permissions WHERE part_id=?", (ptid,))
        conn.execute("DELETE FROM custom_doc_types WHERE part_id=?", (ptid,))
        conn.execute("DELETE FROM part_info WHERE part_id=?", (ptid,))
        # 4) 删除零件主表
        conn.execute("DELETE FROM parts WHERE id=?", (ptid,))
        conn.commit()
        write_audit(u["uid"], "delete_part", "project %s part %s id=%s" % (pt["project_id"], pt["name"], ptid))
        return jsonify({"ok": True})

    @app.route("/api/parts/<int:ptid>/phases", methods=["POST"])
    def api_phase_create(ptid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute("SELECT project_id FROM parts WHERE id=?", (ptid,)).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pt["project_id"]):
            return jsonify({"error": "需要项目管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        # 支持批量：phases: [] 优先；兼容旧 phase: str
        requested = data.get("phases")
        if isinstance(requested, list):
            phases = [str(p).strip() for p in requested if str(p).strip()]
        else:
            one = (data.get("phase") or "").strip()
            phases = [one] if one else []
        if not phases:
            return jsonify({"error": "请至少输入一个阶段名称"}), 400
        # 去重（按提交顺序保留首次出现）
        seen = set()
        uniq = []
        for p in phases:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        phases = uniq
        created = []
        skipped = []
        for ph in phases:
            if conn.execute(
                "SELECT 1 FROM part_phases WHERE part_id=? AND phase=?", (ptid, ph)
            ).fetchone():
                skipped.append(ph)
                continue
            conn.execute(
                "INSERT INTO part_phases (part_id, phase) VALUES (?,?)", (ptid, ph)
            )
            created.append(ph)
        # 模板里有 doc_type 的阶段，自动写入 custom_doc_types 形成预设子文件夹；
        # 手动输入且不在模板里的阶段，模板查不到，自然不建子文件夹。
        _seed_custom_doc_types(conn, pt["project_id"], ptid, phases, u["uid"])
        conn.commit()
        if created:
            write_audit(
                u["uid"], "create_phase",
                "part %s phases=%s" % (ptid, ",".join(created)),
            )
        msg_parts = ["已创建 %d 个阶段" % len(created)]
        if skipped:
            msg_parts.append("已存在跳过 %d 个（%s）" % (len(skipped), ", ".join(skipped)))
        return jsonify({
            "ok": True,
            "created": created,
            "skipped": skipped,
            "message": "；".join(msg_parts),
        })

    @app.route("/api/parts/<int:ptid>/phases/<phase>", methods=["DELETE"])
    def api_phase_delete(ptid, phase):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute("SELECT project_id FROM parts WHERE id=?", (ptid,)).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pt["project_id"]):
            return jsonify({"error": "需要项目管理员权限"}), 403
        # 阶段下存在文件时不允许直接删除，避免数据孤立
        if conn.execute(
            "SELECT 1 FROM files WHERE part_id=? AND phase=?", (ptid, phase)
        ).fetchone():
            return jsonify({"error": "该阶段下存在文件，无法删除"}), 409
        conn.execute("DELETE FROM part_phases WHERE part_id=? AND phase=?", (ptid, phase))
        conn.commit()
        write_audit(u["uid"], "delete_phase", "part %s phase %s" % (ptid, phase))
        return jsonify({"ok": True})

    # ---------- 重命名（项目 / 零件 / 阶段） ----------
    @app.route("/api/projects/<int:pid>", methods=["PUT"])
    def api_project_rename(pid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要项目管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        new_no = (data.get("project_no") or "").strip()
        if not new_no:
            return jsonify({"error": "项目编号必填"}), 400
        conn = get_db()
        existing = conn.execute("SELECT project_no FROM projects WHERE id=?", (pid,)).fetchone()
        if not existing:
            return jsonify({"error": "项目不存在"}), 404
        if new_no != existing["project_no"] and \
           conn.execute("SELECT 1 FROM projects WHERE project_no=? AND id<>?", (new_no, pid)).fetchone():
            return jsonify({"error": "项目编号已被其他项目使用"}), 409
        conn.execute("UPDATE projects SET project_no=? WHERE id=?", (new_no, pid))
        conn.commit()
        # 迁移磁盘所有存储路径下的项目目录，避免历史文件下载失败
        for root in get_upload_roots():
            old_dir = os.path.join(root, existing["project_no"])
            new_dir = os.path.join(root, new_no)
            if os.path.isdir(old_dir) and old_dir != new_dir:
                try:
                    shutil.move(old_dir, new_dir)
                except Exception as _e:
                    write_audit(u["uid"], "rename_project_warn",
                                "迁移目录失败 %s -> %s: %s" % (old_dir, new_dir, _e))
        write_audit(u["uid"], "rename_project", "%s -> %s" % (existing["project_no"], new_no))
        return jsonify({"ok": True})

    @app.route("/api/projects/<int:pid>/hide", methods=["POST"])
    def api_project_toggle_hide(pid):
        """隐藏 / 显示项目：系统管理员或项目管理员可切换。

        隐藏后普通用户在项目树中看不到该项目；管理员仍可见并可继续管理。
        """
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要项目管理员权限"}), 403
        conn = get_db()
        proj = conn.execute("SELECT project_no, hidden FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj:
            return jsonify({"error": "项目不存在"}), 404
        new_hidden = 0 if (proj["hidden"] or 0) else 1
        conn.execute("UPDATE projects SET hidden=? WHERE id=?", (new_hidden, pid))
        conn.commit()
        write_audit(
            u["uid"], "toggle_project_hidden",
            "id=%s %s hidden=%s" % (pid, proj["project_no"], new_hidden),
        )
        return jsonify({"ok": True, "hidden": new_hidden})

    @app.route("/api/parts/<int:ptid>", methods=["PUT"])
    def api_part_rename(ptid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute("SELECT project_id, name FROM parts WHERE id=?", (ptid,)).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pt["project_id"]):
            return jsonify({"error": "需要项目管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        new_name = (data.get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "零件名称必填"}), 400
        if new_name != pt["name"] and \
           conn.execute("SELECT 1 FROM parts WHERE project_id=? AND name=? AND id<>?",
                        (pt["project_id"], new_name, ptid)).fetchone():
            return jsonify({"error": "该项目下已存在同名零件"}), 409
        conn.execute("UPDATE parts SET name=? WHERE id=?", (new_name, ptid))
        conn.commit()
        write_audit(u["uid"], "rename_part", "id=%s %s -> %s" % (ptid, pt["name"], new_name))
        return jsonify({"ok": True})

    @app.route("/api/parts/<int:ptid>/phases/<path:old_phase>", methods=["PUT"])
    def api_phase_rename(ptid, old_phase):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        pt = conn.execute("SELECT project_id FROM parts WHERE id=?", (ptid,)).fetchone()
        if not pt:
            return jsonify({"error": "零件不存在"}), 404
        if u["role"] != "superAdmin" and not is_project_admin(u["uid"], pt["project_id"]):
            return jsonify({"error": "需要项目管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        new_phase = (data.get("phase") or "").strip()
        if not new_phase:
            return jsonify({"error": "阶段名称必填"}), 400
        if new_phase == old_phase:
            return jsonify({"ok": True})
        if conn.execute("SELECT 1 FROM part_phases WHERE part_id=? AND phase=? AND phase<>?",
                        (ptid, new_phase, old_phase)).fetchone():
            return jsonify({"error": "该零件下已存在同名阶段"}), 409
        # 阶段是该零件级 part_phases 中的具名记录；同时在多个文件 / 自定义 / 权限表中
        # 通过 phase 字段引用，因此重命名需一并更新（保证树节点 + 文件归类一致）。
        conn.execute("UPDATE part_phases SET phase=? WHERE part_id=? AND phase=?",
                     (new_phase, ptid, old_phase))
        conn.execute("UPDATE files SET phase=? WHERE part_id=? AND phase=?",
                     (new_phase, ptid, old_phase))
        conn.execute("UPDATE custom_doc_types SET phase=? WHERE part_id=? AND phase=?",
                     (new_phase, ptid, old_phase))
        conn.execute("""UPDATE folder_permissions SET phase=?
                          WHERE project_id=? AND phase=? AND doc_type IS NOT NULL""",
                     (new_phase, pt["project_id"], old_phase))
        conn.execute("""UPDATE folder_permissions SET phase=?
                          WHERE project_id=? AND phase=? AND doc_type IS NULL""",
                     (new_phase, pt["project_id"], old_phase))
        conn.commit()
        write_audit(u["uid"], "rename_phase",
                    "part=%s %s -> %s" % (ptid, old_phase, new_phase))
        return jsonify({"ok": True})
