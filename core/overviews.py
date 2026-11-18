# -*- coding: utf-8 -*-
"""
概览图 API（项目级 / 零件级），复用 project_overviews 表与 uploads/overviews/

说明：
  - project_overviews 表使用 (project_id, part_id) 复合主键，业务上每个零件只存一条。
    表本身没有 id 列，路由按 SQLite 行内 ROWID (alias id) 作为前端引用。
  - 查看权限：项目成员及以上；管理权限：项目管理员 / 系统管理员。
"""
import os
import time
import uuid

from flask import request, jsonify, send_file
from core.db import get_db, get_overview_dir, get_current_upload_root, locate_overview
from core.auth import get_request_user, is_admin
from core.permissions import get_user_project_role, is_project_admin, can_edit_project_content
from core.audit import write_audit

import openpyxl

ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "pdf", "xlsx", "xls"}


def _can_view_overview(u, pid):
    """查看权限：系统管理员 / 项目成员均可查看概览图。"""
    if u["role"] == "superAdmin":
        return True
    return get_user_project_role(u["uid"], int(pid)) is not None


def _can_manage_overview(u, pid, ptid=None):
    """概览图管理权限。

    项目级概览图（不带 part_id）：仅项目管理员 / 系统管理员。
    零件级概览图（带 part_id）：项目管理员，或本人在该项目任一文件夹
    被授予『可编辑』的成员（与文档权限联动）。
    """
    if str(ptid) in ("", "0", "null", "None"):
        return is_project_admin(u["uid"], int(pid))
    return can_edit_project_content(u["uid"], int(pid))


def register_overview_routes(app):

    @app.route("/api/overviews")
    def api_overviews():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        pid = request.args.get("project_id")
        ptid = request.args.get("part_id")
        if not pid:
            return jsonify({"error": "缺少 project_id"}), 400
        if not _can_view_overview(u, pid):
            return jsonify({"error": "无权访问该项目概览图"}), 403
        conn = get_db()
        sql = ("SELECT o.rowid AS id, o.project_id, o.part_id, o.storage_name, o.original_name, "
               "o.mime, o.size, o.uploader_id, u.name AS uploader_name, o.uploaded_at "
               "FROM project_overviews o LEFT JOIN users u ON o.uploader_id=u.uid "
               "WHERE o.project_id=?")
        params = [pid]
        if ptid:
            sql += " AND part_id=?"
            params.append(ptid)
        rows = conn.execute(sql, params).fetchall()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/overviews", methods=["POST"])
    def api_overview_upload():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        pid = request.form.get("project_id") or request.args.get("project_id")
        ptid = request.form.get("part_id") or request.args.get("part_id") or None
        if not pid:
            return jsonify({"error": "缺少 project_id"}), 400
        if not _can_manage_overview(u, pid, ptid):
            return jsonify({"error": "仅项目管理员可管理项目概览图，或需具备该项目内容编辑权限"}), 403
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "未选择文件"}), 400
        original = f.filename or "overview"
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if ext not in ALLOWED_EXT:
            return jsonify({"error": "不支持的文件类型"}), 400
        current_root = get_current_upload_root()
        ov_dir = get_overview_dir(current_root)
        os.makedirs(ov_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d%H%M%S")
        storage = "%s_%s_%s" % (ts, uuid.uuid4().hex[:6], original)
        path = os.path.join(ov_dir, storage)
        f.save(path)
        mime = f.mimetype or "application/octet-stream"
        size = os.path.getsize(path)
        # 仅保留该项目+零件的一条概览图（覆盖式）
        conn = get_db()
        conn.execute("DELETE FROM project_overviews WHERE project_id=? AND part_id IS ?",
                     (pid, ptid))
        conn.execute(
            """INSERT INTO project_overviews
               (project_id, part_id, storage_name, storage_root, original_name, mime, size, uploader_id, uploaded_at)
               VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
            (pid, ptid, storage, current_root, original, mime, size, u["uid"]),
        )
        conn.commit()
        write_audit(u["uid"], "upload_overview",
                    "project %s part %s file %s" % (pid, ptid, original))
        return jsonify({"ok": True})

    @app.route("/api/overviews/<int:oid>")
    def api_overview_file(oid):
        """下载 / 内联预览概览图实体（项目成员及以上）。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        r = conn.execute(
            "SELECT rowid AS id, * FROM project_overviews WHERE rowid=?",
            (oid,),
        ).fetchone()
        if not r:
            return jsonify({"error": "概览图不存在"}), 404
        if not _can_view_overview(u, r["project_id"]):
            return jsonify({"error": "无权访问该项目概览图"}), 403
        path = locate_overview(r["storage_name"], r["storage_root"] if "storage_root" in r.keys() else None)
        if not path:
            return jsonify({"error": "文件实体缺失"}), 404
        return send_file(path)

    @app.route("/api/overviews/<int:pid>/<ptid>", methods=["DELETE"])
    def api_overview_delete(pid, ptid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        # ptid 可能为空串 / null 表示项目级
        if str(ptid) in ("", "null", "0", "None"):
            ptid_val = None
        else:
            try:
                ptid_val = int(ptid)
            except Exception:
                ptid_val = ptid
        if not _can_manage_overview(u, pid, ptid_val):
            return jsonify({"error": "仅项目管理员可管理项目概览图，或需具备该项目内容编辑权限"}), 403
        r = conn.execute(
            "SELECT * FROM project_overviews WHERE project_id=? AND part_id IS ?",
            (pid, ptid_val),
        ).fetchone()
        if not r:
            return jsonify({"error": "概览图不存在"}), 404
        try:
            p = locate_overview(r["storage_name"], r["storage_root"] if "storage_root" in r.keys() else None)
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
        conn.execute("DELETE FROM project_overviews WHERE project_id=? AND part_id IS ?",
                     (pid, ptid_val))
        conn.commit()
        write_audit(u["uid"], "delete_overview", "project %s part %s" % (pid, ptid_val))
        return jsonify({"ok": True})

    @app.route("/api/overviews/<int:oid>/rows")
    def api_overview_rows(oid):
        """解析 Excel 概览图（xlsx/xls），返回每个 sheet 的二维表格数据供前端渲染。

        权限：项目成员及以上（避免外泄），不要求管理员。
        """
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        r = conn.execute(
            "SELECT rowid AS id, * FROM project_overviews WHERE rowid=?",
            (oid,),
        ).fetchone()
        if not r:
            return jsonify({"error": "概览图不存在"}), 404
        if not _can_view_overview(u, r["project_id"]):
            return jsonify({"error": "无权访问该项目概览图"}), 403
        path = locate_overview(r["storage_name"], r["storage_root"] if "storage_root" in r.keys() else None)
        if not path:
            return jsonify({"error": "文件实体缺失"}), 404
        ext = (r["original_name"] or "").rsplit(".", 1)[-1].lower()
        if ext not in ("xlsx", "xls"):
            return jsonify({"error": "该文件不是 Excel"}), 400
        try:
            wb = openpyxl.load_workbook(path, data_only=True)
        except Exception as e:
            return jsonify({"error": "Excel 解析失败: %s" % e}), 400
        sheets_out = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append([("" if v is None else str(v)) for v in row])
                if len(rows) >= 200:
                    break
            sheets_out.append({"name": ws.title, "rows": rows})
        return jsonify({"name": r["original_name"], "sheets": sheets_out})
