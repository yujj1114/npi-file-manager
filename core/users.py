# -*- coding: utf-8 -*-
"""
用户管理 + 审计日志 API（复刻主服务系统管理功能）
"""
import io

import bcrypt
import openpyxl
from flask import request, jsonify, send_file
from core.db import get_db
from core.auth import get_request_user, is_admin
from core.audit import write_audit, get_action_label


def register_user_routes(app):

    @app.route("/api/users")
    def api_users():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if not is_admin(u, allow_project_admin=True):
            return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()
        rows = conn.execute(
            "SELECT u.uid, u.name, u.role, u.frozen, u.dept, u.position_id, "
            "p.name AS position_name, u.created_at "
            "FROM users u LEFT JOIN positions p ON u.position_id=p.id "
            "ORDER BY u.role, u.name"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/users/all")
    def api_users_all():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        if not is_admin(u, allow_project_admin=True):
            return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()
        rows = conn.execute(
            "SELECT u.uid, u.name, u.role, u.dept, u.position_id, p.name AS position_name "
            "FROM users u LEFT JOIN positions p ON u.position_id=p.id "
            "WHERE u.frozen=0 ORDER BY u.name"
        ).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/users/create", methods=["POST"])
    def api_user_create():
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        uid = (data.get("uid") or "").strip()
        name = (data.get("name") or "").strip()
        role = data.get("role") or "pd"
        dept = data.get("dept") or ""
        pw = data.get("password") or "123456"
        position_id = data.get("position_id")
        if not uid or not name:
            return jsonify({"error": "工号与姓名必填"}), 400
        if role not in ("superAdmin", "projectAdmin", "pd"):
            return jsonify({"error": "角色非法"}), 400
        conn = get_db()
        if conn.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone():
            return jsonify({"error": "工号已存在"}), 400
        if position_id is not None:
            position_id = int(position_id) if position_id != "" else None
            if position_id is not None and not conn.execute(
                "SELECT id FROM positions WHERE id=?", (position_id,)
            ).fetchone():
                position_id = None
        nh = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        conn.execute(
            """INSERT INTO users (uid, name, dept, password_hash, role, frozen, must_change_password, position_id, created_at)
               VALUES (?,?,?,?,?,0,1,?,datetime('now','localtime'))""",
            (uid, name, dept, nh, role, position_id),
        )
        conn.commit()
        write_audit(u["uid"], "create_user", "user %s %s" % (uid, name))
        return jsonify({"ok": True})

    @app.route("/api/users/<uid>/role", methods=["PUT"])
    def api_user_role(uid):
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        role = data.get("role")
        if role not in ("superAdmin", "projectAdmin", "pd"):
            return jsonify({"error": "角色非法"}), 400
        conn = get_db()
        if not conn.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone():
            return jsonify({"error": "用户不存在"}), 404
        # 防止降级系统中最后一位超级管理员，否则将无管理员可管理用户/项目
        if role != "superAdmin":
            cur = conn.execute("SELECT role FROM users WHERE uid=?", (uid,)).fetchone()
            if cur and cur["role"] == "superAdmin":
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE role='superAdmin'"
                ).fetchone()["c"]
                if cnt <= 1:
                    return jsonify({"error": "不能降级最后一位超级管理员"}), 409
        conn.execute("UPDATE users SET role=? WHERE uid=?", (role, uid))
        conn.commit()
        write_audit(u["uid"], "update_user_role", "user %s -> %s" % (uid, role))
        return jsonify({"ok": True})

    @app.route("/api/users/<uid>/freeze", methods=["POST"])
    def api_user_freeze(uid):
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        frozen = 1 if data.get("frozen") else 0
        conn = get_db()
        if not conn.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone():
            return jsonify({"error": "用户不存在"}), 404
        conn.execute("UPDATE users SET frozen=? WHERE uid=?", (frozen, uid))
        conn.commit()
        write_audit(u["uid"], "freeze_user", "user %s frozen=%s" % (uid, frozen))
        return jsonify({"ok": True})

    @app.route("/api/users/<uid>/password", methods=["POST"])
    def api_user_password(uid):
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        new_pw = data.get("password") or "123456"
        conn = get_db()
        if not conn.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone():
            return jsonify({"error": "用户不存在"}), 404
        nh = bcrypt.hashpw(new_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        conn.execute("UPDATE users SET password_hash=?, must_change_password=1 WHERE uid=?",
                     (nh, uid))
        conn.commit()
        write_audit(u["uid"], "reset_password", "user %s" % uid)
        return jsonify({"ok": True})

    @app.route("/api/users/<uid>", methods=["PUT"])
    def api_user_update(uid):
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        dept = data.get("dept") or ""
        role = data.get("role")
        password = data.get("password")  # 可选，非空则重置密码
        position_id = data.get("position_id")
        if not name:
            return jsonify({"error": "姓名必填"}), 400
        conn = get_db()
        row = conn.execute("SELECT uid, role FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return jsonify({"error": "用户不存在"}), 404
        # 角色变更时保护最后一位超级管理员
        if role and role != row["role"]:
            if role not in ("superAdmin", "projectAdmin", "pd"):
                return jsonify({"error": "角色非法"}), 400
            if row["role"] == "superAdmin" and role != "superAdmin":
                cnt = conn.execute(
                    "SELECT COUNT(*) AS c FROM users WHERE role='superAdmin'"
                ).fetchone()["c"]
                if cnt <= 1:
                    return jsonify({"error": "不能降级最后一位系统管理员"}), 409
        upd = "UPDATE users SET name=?, dept=?"
        params = [name, dept]
        if role and role != row["role"]:
            upd += ", role=?"
            params.append(role)
        if position_id is not None:
            pid = int(position_id) if position_id != "" else None
            if pid is not None and not conn.execute(
                "SELECT id FROM positions WHERE id=?", (pid,)
            ).fetchone():
                pid = None
            upd += ", position_id=?"
            params.append(pid)
        if password:
            nh = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            upd += ", password_hash=?, must_change_password=1"
            params.append(nh)
        upd += " WHERE uid=?"
        params.append(uid)
        conn.execute(upd, params)
        conn.commit()
        write_audit(u["uid"], "update_user", "user %s" % uid)
        return jsonify({"ok": True})

    @app.route("/api/users/<uid>", methods=["DELETE"])
    def api_user_delete(uid):
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()
        row = conn.execute("SELECT uid, role FROM users WHERE uid=?", (uid,)).fetchone()
        if not row:
            return jsonify({"error": "用户不存在"}), 404
        # 不能删除当前登录账号
        if uid == u["uid"]:
            return jsonify({"error": "不能删除当前登录账号"}), 409
        # 保护最后一位超级管理员
        if row["role"] == "superAdmin":
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role='superAdmin'"
            ).fetchone()["c"]
            if cnt <= 1:
                return jsonify({"error": "不能删除最后一位系统管理员"}), 409
        # 级联清理该用户的权限记录（项目权限 CASCADE 也会删，这里显式清理更稳妥）
        conn.execute("DELETE FROM project_permissions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM folder_permissions WHERE user_id=?", (uid,))
        conn.execute("DELETE FROM users WHERE uid=?", (uid,))
        conn.commit()
        write_audit(u["uid"], "delete_user", "user %s" % uid)
        return jsonify({"ok": True})

    def _norm_role(v):
        if not v:
            return "pd", True
        s = str(v).strip().lower()
        if s in ("superadmin", "系统管理员", "sa", "超级管理员"):
            return "superAdmin", True
        if s in ("projectadmin", "项目管理员", "pa"):
            return "projectAdmin", True
        if s in ("pd", "一般用户", "普通用户", "user"):
            return "pd", True
        return None, False

    def _norm_frozen(v):
        if v is None or v == "":
            return 0, True
        s = str(v).strip()
        if s in ("0", "正常", "启用", "激活", "false", "no"):
            return 0, True
        if s in ("1", "冻结", "禁用", "停用", "true", "yes"):
            return 1, True
        return None, False

    def _load_users_dict(conn):
        rows = conn.execute(
            "SELECT uid, name, dept, role, frozen, position_id FROM users"
        ).fetchall()
        return {r["uid"]: dict(r) for r in rows}

    @app.route("/api/users/export")
    def api_users_export():
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()
        rows = conn.execute(
            "SELECT u.uid, u.name, u.dept, u.role, u.frozen, p.name AS position_name "
            "FROM users u LEFT JOIN positions p ON u.position_id=p.id "
            "ORDER BY u.role, u.name"
        ).fetchall()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "用户列表"
        ws.append(["工号", "姓名", "部门", "职位", "角色", "状态"])
        role_disp = {"superAdmin": "系统管理员", "projectAdmin": "项目管理员", "pd": "一般用户"}
        for r in rows:
            ws.append([
                r["uid"],
                r["name"],
                r["dept"] or "",
                r["position_name"] or "",
                role_disp.get(r["role"], r["role"]),
                "冻结" if r["frozen"] else "正常",
            ])
        for col in ws.columns:
            max_len = 0
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            for cell in col:
                try:
                    val = str(cell.value) if cell.value is not None else ""
                    max_len = max(max_len, len(val))
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max_len + 4, 40)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        write_audit(u["uid"], "export_users", "count=%s" % len(rows))
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="NPI用户列表.xlsx",
        )

    @app.route("/api/users/import/template")
    def api_users_import_template():
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "用户导入模板"
        headers = ["工号", "姓名", "部门", "职位", "角色", "状态"]
        ws.append(headers)
        # 表头加粗
        from openpyxl.styles import Font, PatternFill, Alignment
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="4472C4")
            c.alignment = Alignment(horizontal="center")
        # 示例行
        ws.append(["EMP001", "张三", "研发部", "PD", "一般用户", "正常"])
        # 下拉验证：角色 / 状态
        role_dv = openpyxl.worksheet.datavalidation.DataValidation(
            type="list", formula1='"系统管理员,项目管理员,一般用户"', allow_blank=True
        )
        status_dv = openpyxl.worksheet.datavalidation.DataValidation(
            type="list", formula1='"正常,冻结"', allow_blank=True
        )
        ws.add_data_validation(role_dv)
        ws.add_data_validation(status_dv)
        role_dv.add("E2:E1000")
        status_dv.add("F2:F1000")
        # 列宽
        for col_letter, w in zip("ABCDEF", (12, 12, 14, 12, 12, 10)):
            ws.column_dimensions[col_letter].width = w
        # 填写说明 sheet
        note = wb.create_sheet("填写说明")
        notes = [
            ["列名", "是否必填", "说明"],
            ["工号", "必填", "用户的唯一工号，导入后作为登录账号，不能与已有用户重复"],
            ["姓名", "必填", "用户真实姓名"],
            ["部门", "选填", "用户所属部门"],
            ["职位", "选填", "填写职位字典中已存在的职位名称（如 PD / PQE），留空则不设置职位"],
            ["角色", "选填", "可选：系统管理员 / 项目管理员 / 一般用户（留空默认一般用户）"],
            ["状态", "选填", "可选：正常 / 冻结（留空默认正常）"],
            ["", "", "新增用户默认密码为 123456，首次登录需修改"],
            ["", "", "请勿修改表头行，从第二行开始填写；空行会自动跳过"],
        ]
        for r in notes:
            note.append(r)
        for c in note[1]:
            c.font = Font(bold=True)
        for col_letter, w in zip("ABC", (10, 10, 60)):
            note.column_dimensions[col_letter].width = w
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name="NPI用户导入模板.xlsx",
        )

    @app.route("/api/users/import", methods=["POST"])
    def api_users_import():
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        dry = request.args.get("dry_run", "1") not in ("0", "false", "False")
        if "file" not in request.files:
            return jsonify({"error": "请上传 Excel 文件"}), 400
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "未选择文件"}), 400
        try:
            wb = openpyxl.load_workbook(io.BytesIO(f.read()))
        except Exception as e:
            return jsonify({"error": "无法解析 Excel：" + str(e)}), 400
        ws = wb.active
        conn = get_db()
        existing = _load_users_dict(conn)
        role_disp = {"superAdmin": "系统管理员", "projectAdmin": "项目管理员", "pd": "一般用户"}
        # 职位名称 -> id 映射（用于导入）
        pos_rows = conn.execute("SELECT id, name FROM positions WHERE disabled=0 OR disabled IS NULL").fetchall()
        pos_name_to_id = {r["name"]: r["id"] for r in pos_rows}
        header_map = {}
        rows_out = []
        errors = []
        to_create = []
        to_update = []
        # 解析表头（首行）
        header = [str(c.value or "").strip() for c in next(ws.iter_rows(min_row=1, max_row=1))]
        for idx, h in enumerate(header):
            if h in ("工号", "uid", "用户工号"):
                header_map["uid"] = idx
            elif h in ("姓名", "name", "用户名"):
                header_map["name"] = idx
            elif h in ("部门", "dept", "部门名称"):
                header_map["dept"] = idx
            elif h in ("角色", "role", "权限"):
                header_map["role"] = idx
            elif h in ("状态", "status", "冻结", "frozen"):
                header_map["status"] = idx
            elif h in ("职位", "position", "职位名称"):
                header_map["position"] = idx
        if "uid" not in header_map or "name" not in header_map:
            return jsonify({"error": "Excel 缺少必要列：工号、姓名"}), 400
        uid_idx = header_map["uid"]
        name_idx = header_map["name"]
        dept_idx = header_map.get("dept")
        role_idx = header_map.get("role")
        status_idx = header_map.get("status")
        position_idx = header_map.get("position")
        for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            uid = str(row[uid_idx] or "").strip() if uid_idx < len(row) else ""
            name = str(row[name_idx] or "").strip() if name_idx < len(row) else ""
            dept = ""
            if dept_idx is not None and dept_idx < len(row) and row[dept_idx] is not None:
                dept = str(row[dept_idx]).strip()
            role_raw = ""
            if role_idx is not None and role_idx < len(row) and row[role_idx] is not None:
                role_raw = str(row[role_idx]).strip()
            status_raw = ""
            if status_idx is not None and status_idx < len(row) and row[status_idx] is not None:
                status_raw = str(row[status_idx]).strip()
            position_raw = ""
            if position_idx is not None and position_idx < len(row) and row[position_idx] is not None:
                position_raw = str(row[position_idx]).strip()
            position_id = None
            if position_raw:
                position_id = pos_name_to_id.get(position_raw)
                if position_id is None:
                    rec = {"row": row_idx, "uid": uid, "name": name, "dept": dept,
                           "role_raw": role_raw, "status_raw": status_raw, "position_raw": position_raw}
                    rec["action"] = "错误"
                    rec["action_detail"] = "职位不存在：%s" % position_raw
                    errors.append("第 %s 行（%s）：职位不存在：%s" % (row_idx, uid, position_raw))
                    rows_out.append(rec)
                    continue
            if not uid and not name and not dept and not role_raw and not status_raw and not position_raw:
                continue  # 跳过空行
            rec = {"row": row_idx, "uid": uid, "name": name, "dept": dept, "role_raw": role_raw,
                   "status_raw": status_raw, "position_raw": position_raw, "position_id": position_id}
            if not uid:
                rec["action"] = "错误"
                rec["action_detail"] = "工号为空"
                errors.append("第 %s 行：工号为空" % row_idx)
                rows_out.append(rec)
                continue
            if not name:
                rec["action"] = "错误"
                rec["action_detail"] = "姓名为空"
                errors.append("第 %s 行（%s）：姓名为空" % (row_idx, uid))
                rows_out.append(rec)
                continue
            role, role_ok = _norm_role(role_raw)
            if not role_ok:
                rec["action"] = "错误"
                rec["action_detail"] = "角色无法识别：%s" % role_raw
                errors.append("第 %s 行（%s）：角色无法识别：%s" % (row_idx, uid, role_raw))
                rows_out.append(rec)
                continue
            frozen, frozen_ok = _norm_frozen(status_raw)
            if not frozen_ok:
                rec["action"] = "错误"
                rec["action_detail"] = "状态无法识别：%s" % status_raw
                errors.append("第 %s 行（%s）：状态无法识别：%s" % (row_idx, uid, status_raw))
                rows_out.append(rec)
                continue
            rec["role"] = role
            rec["role_text"] = role_disp.get(role, role)
            rec["frozen"] = frozen
            rec["status_text"] = "冻结" if frozen else "正常"
            old = existing.get(uid)
            if old:
                changed = (
                    old["name"] != name
                    or (old["dept"] or "") != dept
                    or old["role"] != role
                    or old["frozen"] != frozen
                    or (old.get("position_id") or "") != (position_id or "")
                )
                if old["role"] == "superAdmin" and (role != "superAdmin" or frozen == 1):
                    cnt = conn.execute(
                        "SELECT COUNT(*) AS c FROM users WHERE role='superAdmin'"
                    ).fetchone()["c"]
                    if cnt <= 1:
                        rec["action"] = "错误"
                        rec["action_detail"] = "不能变更/冻结最后一位系统管理员"
                        errors.append("第 %s 行（%s）：不能变更/冻结最后一位系统管理员" % (row_idx, uid))
                        rows_out.append(rec)
                        continue
                if changed:
                    rec["action"] = "更新"
                    rec["action_detail"] = ""
                    to_update.append((uid, name, dept, role, frozen, position_id))
                else:
                    rec["action"] = "跳过"
                    rec["action_detail"] = "与系统一致"
            else:
                rec["action"] = "新增"
                rec["action_detail"] = "默认密码 123456"
                to_create.append((uid, name, dept, role, frozen, position_id))
            rows_out.append(rec)
        stats = {
            "total": len(rows_out),
            "create": len(to_create),
            "update": len(to_update),
            "skip": sum(1 for r in rows_out if r["action"] == "跳过"),
            "error": len(errors),
        }
        if dry:
            return jsonify({"ok": True, "dry_run": True, "rows": rows_out, "errors": errors, "stats": stats})
        # 真正执行
        created = 0
        updated = 0
        for uid, name, dept, role, frozen, position_id in to_create:
            if conn.execute("SELECT uid FROM users WHERE uid=?", (uid,)).fetchone():
                continue
            nh = bcrypt.hashpw("123456".encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
            conn.execute(
                """INSERT INTO users (uid, name, dept, password_hash, role, frozen, must_change_password, position_id, created_at)
                   VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))""",
                (uid, name, dept, nh, role, frozen, position_id),
            )
            created += 1
        for uid, name, dept, role, frozen, position_id in to_update:
            conn.execute(
                "UPDATE users SET name=?, dept=?, role=?, frozen=?, position_id=? WHERE uid=?",
                (name, dept, role, frozen, position_id, uid),
            )
            updated += 1
        conn.commit()
        write_audit(u["uid"], "import_users", "create=%s update=%s" % (created, updated))
        return jsonify({"ok": True, "created": created, "updated": updated, "errors": errors, "stats": stats})

    @app.route("/api/audit")
    def api_audit():
        u = get_request_user()
        if not is_admin(u):
            return jsonify({"error": "需要管理员权限"}), 403
        conn = get_db()
        limit = request.args.get("limit", 200)
        try:
            limit = int(limit)
        except Exception:
            limit = 200
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["action_label"] = get_action_label(r["action"])
            u2 = conn.execute("SELECT name FROM users WHERE uid=?", (r["uid"],)).fetchone()
            d["user_name"] = u2["name"] if u2 else r["uid"]
            out.append(d)
        return jsonify(out)
