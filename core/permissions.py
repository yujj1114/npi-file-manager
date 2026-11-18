# -*- coding: utf-8 -*-
"""
权限管理模块（整合原增强服务权限逻辑 + 主服务可见项目逻辑）
"""
import json

from flask import request, jsonify
from core.db import get_db
from core.auth import get_request_user, is_admin


def get_user_project_role(uid, pid):
    conn = get_db()
    u = conn.execute("SELECT role FROM users WHERE uid=?", (uid,)).fetchone()
    if u and u["role"] == "superAdmin":
        return "superAdmin"
    rec = conn.execute(
        "SELECT role_in_project FROM project_permissions WHERE project_id=? AND user_id=?",
        (pid, uid),
    ).fetchone()
    if rec:
        return rec["role_in_project"] or "pd"
    return None


def is_project_admin(uid, pid):
    return get_user_project_role(uid, pid) in ("superAdmin", "projectAdmin")


def can_edit_project_content(uid, pid):
    """零件图片/零件信息等『项目内容』的编辑权判定。

    项目管理员 / 系统管理员，或本人在该项目任一文件夹被授予『可编辑』
    （folder_permissions.permission='edit'）即可编辑；
    与授权界面里各用户的阶段/文档类型权限实时联动。
    """
    if is_project_admin(uid, pid):
        return True
    conn = get_db()
    r = conn.execute(
        """SELECT 1 FROM folder_permissions
           WHERE project_id=? AND user_id=? AND part_id IS NULL AND permission='edit'
           LIMIT 1""",
        (pid, uid),
    ).fetchone()
    return bool(r)


def get_visible_projects(uid):
    conn = get_db()
    u = conn.execute("SELECT role FROM users WHERE uid=?", (uid,)).fetchone()
    if u and u["role"] == "superAdmin":
        rows = conn.execute("SELECT id FROM projects").fetchall()
        return {r["id"]: True for r in rows}
    rows = conn.execute(
        "SELECT project_id FROM project_permissions WHERE user_id=?", (uid,)
    ).fetchall()
    return {r["project_id"]: True for r in rows}


def can_access_project(uid, pid):
    """判断 uid 是否为 pid 项目成员（superAdmin 视为全体成员）。"""
    conn = get_db()
    u = conn.execute("SELECT role FROM users WHERE uid=?", (uid,)).fetchone()
    if u and u["role"] == "superAdmin":
        return True
    rec = conn.execute(
        "SELECT 1 FROM project_permissions WHERE project_id=? AND user_id=?",
        (pid, uid),
    ).fetchone()
    return bool(rec)


def get_folder_perm(uid, pid, phase, doc_type):
    conn = get_db()
    # 阶段为 none 时，子文件夹强制无权限
    if phase is not None and doc_type is not None:
        ph = conn.execute(
            """SELECT permission FROM folder_permissions
               WHERE project_id=? AND user_id=? AND part_id IS NULL AND phase=? AND doc_type IS NULL""",
            (pid, uid, phase),
        ).fetchone()
        if ph and ph["permission"] == "none":
            return ""
    if phase is not None and doc_type is not None:
        r = conn.execute(
            """SELECT permission FROM folder_permissions
               WHERE project_id=? AND user_id=? AND part_id IS NULL AND phase=? AND doc_type=?""",
            (pid, uid, phase, doc_type),
        ).fetchone()
        if r and r["permission"]:
            return r["permission"]
    if phase is not None:
        r = conn.execute(
            """SELECT permission FROM folder_permissions
               WHERE project_id=? AND user_id=? AND part_id IS NULL AND phase=? AND doc_type IS NULL""",
            (pid, uid, phase),
        ).fetchone()
        if r and r["permission"]:
            return r["permission"]
    r = conn.execute(
        """SELECT permission FROM folder_permissions
           WHERE project_id=? AND user_id=? AND part_id IS NULL AND phase IS NULL AND doc_type IS NULL""",
        (pid, uid),
    ).fetchone()
    if r and r["permission"]:
        return r["permission"]
    return ""


def phase_summary(phase_permissions):
    if not phase_permissions:
        return "无权限"
    label = {"edit": "可编辑", "read": "只读", "none": "无权限"}
    parts = []
    for ph in sorted(phase_permissions.keys()):
        perm = phase_permissions[ph]
        if perm in ("inherit", "none", ""):
            continue
        parts.append("%s:%s" % (ph, label.get(perm, perm)))
    return " ".join(parts) if parts else "无权限"


def build_folder_tree(conn, pid, uid):
    proj = conn.execute("SELECT id, project_no FROM projects WHERE id=?", (pid,)).fetchone()
    if not proj:
        return None
    root = conn.execute(
        """SELECT permission FROM folder_permissions
           WHERE project_id=? AND user_id=? AND part_id IS NULL AND phase IS NULL AND doc_type IS NULL""",
        (pid, uid),
    ).fetchone()
    tree = {"projectNo": proj["project_no"], "permission": (root["permission"] if root else "")}
    # 只取该项目实际存在的阶段（基于零件上传记录），按阶段模板 sort_order 排序
    phase_rows = conn.execute(
        """SELECT DISTINCT pp.phase, MIN(COALESCE(t.sort_order, 0)) AS so
           FROM part_phases pp
           JOIN parts pt ON pt.id = pp.part_id
           LEFT JOIN phase_doc_templates t ON t.phase = pp.phase
           WHERE pt.project_id = ?
           GROUP BY pp.phase
           ORDER BY so, pp.phase""",
        (pid,),
    ).fetchall()
    phase_order = [r["phase"] for r in phase_rows]
    phase_map = {}
    for phase in phase_order:
        dts = [
            dt["doc_type"]
            for dt in conn.execute(
                """SELECT DISTINCT doc_type, COALESCE(sort_order, 0) AS so
                   FROM phase_doc_templates
                   WHERE phase = ?
                   ORDER BY so, doc_type""",
                (phase,),
            ).fetchall()
        ]
        phase_map[phase] = dts
    fp_rows = conn.execute(
        """SELECT phase, doc_type, permission FROM folder_permissions
           WHERE project_id=? AND user_id=? AND part_id IS NULL""",
        (pid, uid),
    ).fetchall()
    fp = {}
    for r in fp_rows:
        fp[(r["phase"], r["doc_type"])] = r["permission"]
    phases = []
    for phase in phase_order:
        phase_perm = fp.get((phase, None), "")
        dts = sorted(set(phase_map.get(phase, [])))
        # 阶段为 none 时，子文件夹强制无权限；阶段 edit/read 时，子文件夹无记录则继承阶段
        if phase_perm == "none":
            doc_types = [{"docType": dt, "permission": "none"} for dt in dts]
        elif phase_perm in ("edit", "read"):
            doc_types = [{"docType": dt, "permission": (fp.get((phase, dt)) or phase_perm)} for dt in dts]
        else:
            doc_types = [{"docType": dt, "permission": fp.get((phase, dt), "")} for dt in dts]
        phases.append({"phase": phase, "permission": phase_perm, "docTypes": doc_types})
    tree["phases"] = phases
    return tree


def register_perm_routes(app):

    @app.route("/api/permissions/multi")
    def api_permissions_multi():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        pids = request.args.get("pids", "")
        pid_list = [int(x) for x in pids.split(",") if x.strip().isdigit()]
        conn = get_db()
        out = {}
        for pid in pid_list:
            role = get_user_project_role(u["uid"], pid)
            folders = {}
            rows = conn.execute(
                """SELECT phase, doc_type, permission FROM folder_permissions
                   WHERE project_id=? AND user_id=? AND part_id IS NULL""",
                (pid, u["uid"]),
            ).fetchall()
            for r in rows:
                ph = r["phase"] or ""
                dt = r["doc_type"] or ""
                key = (ph + ("|" + dt if dt else "")) if (ph or dt) else ""
                folders[key] = r["permission"] or ""
            out[str(pid)] = {"role_in_project": role, "folders": folders}
        return jsonify(out)

    @app.route("/api/admin/project-permissions/<int:pid>")
    def api_admin_project_perms(pid):
        u = get_request_user()
        if not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()
        proj = conn.execute("SELECT id, project_no FROM projects WHERE id=?", (pid,)).fetchone()
        if not proj:
            return jsonify({"error": "项目不存在"}), 404
        rows = conn.execute(
            """SELECT user_id, role_in_project, allowed_phases, phase_permissions, created_at
               FROM project_permissions
               WHERE project_id=? AND user_id NOT IN (SELECT uid FROM users WHERE role='superAdmin')
               ORDER BY user_id""",
            (pid,),
        ).fetchall()
        fp_rows = conn.execute(
            """SELECT user_id, phase, permission FROM folder_permissions
               WHERE project_id=? AND part_id IS NULL AND doc_type IS NULL AND phase IS NOT NULL""",
            (pid,),
        ).fetchall()
        fp_map = {}
        for r in fp_rows:
            fp_map.setdefault(r["user_id"], {})[r["phase"]] = r["permission"]
        user_rows = conn.execute("SELECT uid, name FROM users").fetchall()
        user_name_map = {r["uid"]: r["name"] for r in user_rows}
        out = []
        for r in rows:
            try:
                pp = json.loads(r["phase_permissions"] or "{}")
            except Exception:
                pp = {}
            pp.update(fp_map.get(r["user_id"], {}))
            role = r["role_in_project"]
            summary = "项目管理员（全部可管理）" if role == "projectAdmin" else phase_summary(pp)
            out.append({
                "userId": r["user_id"],
                "userName": user_name_map.get(r["user_id"], r["user_id"]),
                "roleInProject": role,
                "phaseSummary": summary,
                "createdAt": r["created_at"],
            })
        return jsonify(out)

    @app.route("/api/admin/project-permissions/<int:pid>", methods=["POST"])
    def api_admin_project_perm_create(pid):
        u = get_request_user()
        if not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        tuid = (data.get("userId") or "").strip()
        role = data.get("roleInProject") or "pd"
        if not tuid:
            return jsonify({"error": "缺少用户工号"}), 400
        if role not in ("pd", "projectAdmin"):
            return jsonify({"error": "项目角色非法"}), 400
        conn = get_db()
        if not conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone():
            return jsonify({"error": "项目不存在"}), 404
        if not conn.execute("SELECT uid FROM users WHERE uid=?", (tuid,)).fetchone():
            return jsonify({"error": "用户不存在"}), 404
        conn.execute(
            """INSERT OR REPLACE INTO project_permissions
               (project_id, user_id, role_in_project, allowed_phases, phase_permissions, created_at)
               VALUES (?, ?, ?, '*', '{}', datetime('now','localtime'))""",
            (pid, tuid, role),
        )
        conn.commit()
        from core.audit import write_audit
        write_audit(u["uid"], "grant_perm", "project %s grant %s %s" % (pid, tuid, role))
        return jsonify({"ok": True})

    @app.route("/api/admin/project-permissions/<int:pid>/<uid>", methods=["PUT"])
    def api_admin_project_perm_update(pid, uid):
        u = get_request_user()
        if not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        role = data.get("roleInProject")
        if role not in ("pd", "projectAdmin"):
            return jsonify({"error": "项目角色非法"}), 400
        conn = get_db()
        if not conn.execute("SELECT 1 FROM project_permissions WHERE project_id=? AND user_id=?", (pid, uid)).fetchone():
            return jsonify({"error": "权限记录不存在"}), 404
        conn.execute(
            "UPDATE project_permissions SET role_in_project=? WHERE project_id=? AND user_id=?",
            (role, pid, uid),
        )
        conn.commit()
        from core.audit import write_audit
        write_audit(u["uid"], "update_perm", "project %s user %s -> %s" % (pid, uid, role))
        return jsonify({"ok": True})

    @app.route("/api/admin/project-permissions/<int:pid>/<uid>", methods=["DELETE"])
    def api_admin_project_perm_delete(pid, uid):
        u = get_request_user()
        if not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()
        conn.execute("DELETE FROM project_permissions WHERE project_id=? AND user_id=?", (pid, uid))
        conn.execute("DELETE FROM folder_permissions WHERE project_id=? AND user_id=?", (pid, uid))
        conn.commit()
        from core.audit import write_audit
        write_audit(u["uid"], "revoke_perm", "project %s revoke %s" % (pid, uid))
        return jsonify({"ok": True})

    @app.route("/api/admin/project-permissions/<int:pid>/<uid>/folder-tree")
    def api_admin_folder_tree_get(pid, uid):
        u = get_request_user()
        if not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()
        tree = build_folder_tree(conn, pid, uid)
        if tree is None:
            return jsonify({"error": "项目不存在"}), 404
        return jsonify(tree)

    @app.route("/api/admin/project-permissions/<int:pid>/<uid>/folder-tree", methods=["PUT"])
    def api_admin_folder_tree_put(pid, uid):
        u = get_request_user()
        if not is_project_admin(u["uid"], pid):
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        entries = data.get("entries") or []
        if not isinstance(entries, list):
            return jsonify({"error": "entries 必须是数组"}), 400
        conn = get_db()
        if not conn.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone():
            return jsonify({"error": "项目不存在"}), 404
        if not conn.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone():
            return jsonify({"error": "用户不存在"}), 404
        conn.execute(
            """INSERT OR IGNORE INTO project_permissions
               (project_id, user_id, role_in_project, allowed_phases, phase_permissions, created_at)
               VALUES (?, ?, 'pd', '*', '{}', datetime('now','localtime'))""",
            (pid, uid),
        )
        conn.execute(
            "DELETE FROM folder_permissions WHERE project_id=? AND user_id=? AND part_id IS NULL",
            (pid, uid),
        )
        valid = {"edit", "read", "none"}
        for e in entries:
            perm = e.get("permission")
            if perm not in valid and perm != "":
                continue
            phase = e.get("phase")
            doc_type = e.get("docType")
            if phase is None and doc_type is not None:
                continue
            if not perm:
                continue
            conn.execute(
                """INSERT INTO folder_permissions
                   (project_id, user_id, part_id, phase, doc_type, permission, created_at, updated_at)
                   VALUES (?,?,NULL,?,?,?,datetime('now','localtime'),datetime('now','localtime'))""",
                (pid, uid, phase, doc_type, perm),
            )
        conn.commit()
        from core.audit import write_audit
        write_audit(u["uid"], "save_folder_perm", "project %s user %s folder perm" % (pid, uid))
        return jsonify({"ok": True})

    @app.route("/api/permissions/batch", methods=["POST"])
    def api_permissions_batch():
        u = get_request_user()
        data = request.get_json(silent=True) or {}
        project_ids = [int(x) for x in (data.get("projectIds") or []) if str(x).isdigit()]
        user_ids = data.get("userIds") or []
        role = data.get("role") or "pd"
        if role not in ("pd", "projectAdmin"):
            return jsonify({"error": "项目角色非法"}), 400
        # 初始权限：支持两种模式
        # 1) 新：entries=[{phase, docType, permission}, ...] 按阶段/文档类型矩阵设置
        # 2) 旧：initPermission=edit/read/none 统一设置（兼容旧前端）
        entries = data.get("entries")
        if not isinstance(entries, list):
            entries = None
        init_perm = data.get("initPermission")
        if init_perm not in ("edit", "read", "none"):
            init_perm = "edit" if role == "projectAdmin" else "read"
        # 新：userPerms = {uid: initPermission|entries} —— 多职位混勾时每位用户按自身 initPermission 分别赋权
        user_perms_raw = data.get("userPerms")
        if isinstance(user_perms_raw, list):
            user_perms_raw = {x.get("uid"): x.get("initPermission") for x in user_perms_raw if isinstance(x, dict)}
        if not isinstance(user_perms_raw, dict):
            user_perms_raw = None
        # 新：按职位分组的矩阵覆盖 posEntries={posKey:[{phase,docType,permission}]}
        #     + userPos={uid:posKey} —— 每位用户只套用自己职位那一套手动覆盖
        user_pos_raw = data.get("userPos")
        if not isinstance(user_pos_raw, dict):
            user_pos_raw = {}
        pos_entries_raw = data.get("posEntries")
        pos_entries = {}
        if isinstance(pos_entries_raw, dict):
            for k, lst in pos_entries_raw.items():
                if not isinstance(lst, list):
                    continue
                cleaned = []
                for e in lst:
                    if not isinstance(e, dict):
                        continue
                    perm = e.get("permission")
                    if perm not in ("edit", "read", "none"):
                        continue
                    phase = e.get("phase")
                    doc_type = e.get("docType")
                    if phase is None and doc_type is not None:
                        continue
                    cleaned.append((phase, doc_type, perm))
                if cleaned:
                    pos_entries[str(k or "").strip().lower()] = cleaned
        if not project_ids or not user_ids:
            return jsonify({"error": "请选择项目和用户"}), 400
        # superAdmin 可授权任意项目；projectAdmin 只能授权自己管理的项目
        is_super = u and u["role"] == "superAdmin"
        if not is_super:
            for pid in project_ids:
                if not is_project_admin(u["uid"], pid):
                    return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()

        # 检查重复授权
        placeholders = ",".join("?" * len(project_ids))
        existing = []
        for uid in user_ids:
            rows = conn.execute(
                "SELECT project_id FROM project_permissions WHERE project_id IN (%s) AND user_id=?" % placeholders,
                project_ids + [uid]
            ).fetchall()
            for r in rows:
                existing.append((int(r["project_id"]), uid))
        overwrite = data.get("overwrite") is True
        if existing and not overwrite:
            proj_rows = conn.execute(
                "SELECT id, project_no FROM projects WHERE id IN (%s)" % placeholders, project_ids
            ).fetchall()
            proj_map = {r["id"]: r["project_no"] for r in proj_rows}
            user_rows = conn.execute(
                "SELECT uid, name FROM users WHERE uid IN (%s)" % ",".join("?" * len(user_ids)), user_ids
            ).fetchall()
            user_map = {r["uid"]: r["name"] for r in user_rows}
            return jsonify({
                "error": "以下项目与用户已存在授权关系，确认后将覆盖原权限",
                "existing": [
                    {
                        "projectId": pid,
                        "projectNo": proj_map.get(pid, str(pid)),
                        "userId": uid,
                        "userName": user_map.get(uid, uid)
                    }
                    for pid, uid in existing
                ]
            }), 409

        # 确认覆盖：先删除已有授权记录
        if existing and overwrite:
            for pid, uid in existing:
                conn.execute(
                    "DELETE FROM project_permissions WHERE project_id=? AND user_id=?",
                    (pid, uid)
                )
                conn.execute(
                    "DELETE FROM folder_permissions WHERE project_id=? AND user_id=? AND part_id IS NULL",
                    (pid, uid)
                )

        # 准备阶段/文档类型模板（user_perms 与 entries/init_perm 都需要）
        templates = conn.execute(
            "SELECT phase, doc_type FROM phase_doc_templates ORDER BY phase, doc_type"
        ).fetchall()
        phase_dt = {}
        for r in templates:
            phase_dt.setdefault(r["phase"], set()).add(r["doc_type"])

        # 过滤合法的 entries（矩阵覆盖：userPerms 默认 + entries 覆盖）
        valid = {"edit", "read", "none"}
        clean_entries = None
        if entries:
            clean_entries = []
            for e in entries:
                perm = e.get("permission")
                if perm not in valid:
                    continue
                phase = e.get("phase")
                doc_type = e.get("docType")
                if phase is None and doc_type is not None:
                    continue
                clean_entries.append((phase, doc_type, perm))

        def _apply_overrides(items, over):
            """把覆盖项合并进默认条目，并做级联：
            阶段级 (phase,None) → 该阶段下未被显式覆盖的子文件夹跟随；
            根级 (None,None) → 未被任何显式阶段覆盖的全部跟随。
            单点子文件夹 (phase,dt) 优先级最高。
            """
            merged = {}
            for phase, doc_type, perm in items:
                merged[(phase, doc_type)] = perm
            for phase, doc_type, perm in over:
                merged[(phase, doc_type)] = perm
            phase_overrides = {p: perm for p, dt, perm in over if p is not None and dt is None}
            explicit_dt = {(p, dt) for p, dt, _ in over if p is not None and dt is not None}
            for (p, dt) in list(merged.keys()):
                if p is not None and dt is not None and (p, dt) not in explicit_dt and p in phase_overrides:
                    merged[(p, dt)] = phase_overrides[p]
            root_perm = next((perm for p, dt, perm in over if p is None and dt is None), None)
            if root_perm is not None:
                phase_explicit = {p for p, dt, _ in over if p is not None}
                for (p, dt) in list(merged.keys()):
                    if p not in phase_explicit:
                        merged[(p, dt)] = root_perm
            return [(p, dt, perm) for (p, dt), perm in merged.items()]

        def _entries_for_user(tuid):
            """每位用户的 folder_permissions 初始化条目。

            优先级：
              1) userPerms（按职位） → 默认填满所有 (phase, None) + (phase, dt)
              2) 该用户职位对应的 posEntries 覆盖（无则用「全部」标签的通用覆盖）
              3) 退回旧的全局 entries 覆盖
              4) 仅 initPermission → 按 init_perm 填满所有
            """
            # 该用户所属职位的覆盖集：优先职位专属，其次「全部」标签的通用覆盖
            upos = str(user_pos_raw.get(tuid) or "").strip().lower()
            over = pos_entries.get(upos)
            if over is None:
                over = pos_entries.get("")
            if over is None and clean_entries is not None:
                over = clean_entries

            items = []
            if user_perms_raw is not None:
                u_perm = user_perms_raw.get(tuid)
                if u_perm not in valid:
                    u_perm = init_perm
                items = [(phase, None, u_perm) for phase in phase_dt]
                for phase, dts in phase_dt.items():
                    for dt in dts:
                        items.append((phase, dt, u_perm))
            elif over is not None:
                items = list(over)
            else:
                items = [(phase, None, init_perm) for phase in phase_dt]
                for phase, dts in phase_dt.items():
                    for dt in dts:
                        items.append((phase, dt, init_perm))
            if over is not None and user_perms_raw is not None:
                items = _apply_overrides(items, over)
            return items

        for pid in project_ids:
            for tuid in user_ids:
                conn.execute(
                    """INSERT INTO project_permissions
                       (project_id, user_id, role_in_project, allowed_phases, phase_permissions, created_at)
                       VALUES (?, ?, ?, '*', '{}', datetime('now','localtime'))""",
                    (pid, tuid, role),
                )
                # 初始化该用户在该项目上的阶段/子文件夹权限
                conn.execute(
                    "DELETE FROM folder_permissions WHERE project_id=? AND user_id=? AND part_id IS NULL",
                    (pid, tuid)
                )
                for phase, doc_type, perm in _entries_for_user(tuid):
                    conn.execute(
                        """INSERT INTO folder_permissions
                           (project_id, user_id, part_id, phase, doc_type, permission, created_at, updated_at)
                           VALUES (?, ?, NULL, ?, ?, ?, datetime('now','localtime'), datetime('now','localtime'))""",
                        (pid, tuid, phase, doc_type, perm),
                    )
        conn.commit()
        from core.audit import write_audit
        if user_perms_raw is not None:
            write_audit(u["uid"], "batch_grant",
                        "batch project %s users %s role %s userPerms=%s" % (
                            project_ids, user_ids, role, user_perms_raw))
            return jsonify({"ok": True, "granted": len(project_ids) * len(user_ids), "role": role,
                            "mode": "userPerms"})
        elif entries:
            write_audit(u["uid"], "batch_grant", "batch project %s users %s role %s entries=%s" % (project_ids, user_ids, role, len(clean_entries or [])))
            return jsonify({"ok": True, "granted": len(project_ids) * len(user_ids), "role": role, "entries": len(clean_entries or [])})
        else:
            write_audit(u["uid"], "batch_grant", "batch project %s users %s role %s init %s" % (project_ids, user_ids, role, init_perm))
            return jsonify({"ok": True, "granted": len(project_ids) * len(user_ids), "role": role, "initPermission": init_perm})
