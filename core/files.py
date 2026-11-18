# -*- coding: utf-8 -*-
"""
文件管理 API（上传 / 下载 / 删除），复用现有 uploads/<project_no>/ 目录
"""
import os
import re

from flask import request, jsonify, send_file
from core.db import get_db, get_current_upload_root, locate_file, build_subpath
from core.auth import get_request_user, is_admin
from core.permissions import get_user_project_role, get_folder_perm
from core.audit import write_audit

ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "pdf", "xlsx", "xls",
               "doc", "docx", "txt", "zip", "ppt", "pptx",
               # 工程 / CAD / 三维格式
               "stp", "step", "dwg", "dxf", "igs", "iges",
               "catpart", "catproduct", "stl", "sldprt", "sldasm",
               "x_t", "x_b", "sat"}


def _proj_no(conn, pid):
    r = conn.execute("SELECT project_no FROM projects WHERE id=?", (pid,)).fetchone()
    return r["project_no"] if r else None


def _part_no(conn, ptid):
    if not ptid:
        return None
    try:
        r = conn.execute("SELECT name FROM parts WHERE id=?", (int(ptid),)).fetchone()
        return r["name"] if r else None
    except Exception:
        return None


_WIN_INVALID = re.compile(r'[\\/:*?"<>|]')


def _safe_storage_name(original):
    """把原始文件名清理为安全的磁盘文件名（保留原文件名，只去除非法字符）。"""
    name = os.path.basename(original or "file").strip()
    if not name:
        name = "file"
    # 去除 Windows 非法字符及控制字符
    name = _WIN_INVALID.sub("", name)
    name = "".join(c for c in name if ord(c) >= 32)
    # 处理连续空格/点号，避免末尾点号导致 Windows 创建失败
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(".")
    if not name:
        name = "file"
    return name


def _unique_name(folder, name):
    """若 folder 下已存在同名文件，自动加 (1)/(2) 序号，不覆盖旧文件。"""
    if not os.path.exists(os.path.join(folder, name)):
        return name
    base, ext = name, ""
    if "." in base:
        base, ext = base.rsplit(".", 1)
        ext = "." + ext
    i = 1
    while True:
        candidate = "%s (%d)%s" % (base, i, ext)
        if not os.path.exists(os.path.join(folder, candidate)):
            return candidate
        i += 1


def _can_write(u, pid, phase, doc_type):
    phase = phase or None
    doc_type = doc_type or None
    if u["role"] == "superAdmin":
        return True
    if get_user_project_role(u["uid"], pid) in ("superAdmin", "projectAdmin"):
        return True
    perm = get_folder_perm(u["uid"], pid, phase, doc_type)
    return perm in ("edit",)


def _can_read(u, pid, phase, doc_type):
    phase = phase or None
    doc_type = doc_type or None
    if u["role"] == "superAdmin":
        return True
    if get_user_project_role(u["uid"], pid) in ("superAdmin", "projectAdmin"):
        return True
    perm = get_folder_perm(u["uid"], pid, phase, doc_type)
    return perm in ("edit", "read")


def register_file_routes(app):

    @app.route("/api/files")
    def api_files():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        pid = request.args.get("project_id")
        ptid = request.args.get("part_id")
        phase = request.args.get("phase")
        doc_type = request.args.get("doc_type")
        conn = get_db()
        sql = "SELECT * FROM files WHERE 1=1"
        params = []
        if pid:
            sql += " AND project_id=?"
            params.append(pid)
        if ptid:
            sql += " AND part_id=?"
            params.append(ptid)
        if phase:
            sql += " AND phase=?"
            params.append(phase)
        if doc_type:
            sql += " AND doc_type=?"
            params.append(doc_type)
        sql += " ORDER BY uploaded_at DESC"
        rows = conn.execute(sql, params).fetchall()
        items = [dict(r) for r in rows]
        # 权限过滤：仅返回当前用户可读取的文件，防止越权列出项目文件
        items = [it for it in items
                 if _can_read(u, it["project_id"], it.get("phase"), it.get("doc_type"))]
        return jsonify(items)

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        pid = request.form.get("project_id") or request.args.get("project_id")
        ptid = request.form.get("part_id") or request.args.get("part_id")
        phase = request.form.get("phase") or request.args.get("phase") or None
        doc_type = request.form.get("doc_type") or request.args.get("doc_type") or None
        if not pid:
            return jsonify({"error": "缺少 project_id"}), 400
        # 文件必须归属于具体文档类型文件夹（files.doc_type 为 NOT NULL）
        if not doc_type:
            return jsonify({"error": "缺少文档类型，请选择具体文档类型文件夹后再上传"}), 400
        conn = get_db()
        if not _can_write(u, int(pid), phase, doc_type):
            return jsonify({"error": "无上传权限"}), 403
        files = request.files.getlist("file")
        if not files:
            return jsonify({"error": "未选择文件"}), 400
        # 先批量校验文件类型，避免在保存部分文件后报错导致磁盘产生孤立文件
        for f in files:
            original = f.filename or "file"
            ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
            if ext and ext not in ALLOWED_EXT:
                return jsonify({"error": "不支持的文件类型: %s" % ext}), 400
        proj_no = _proj_no(conn, int(pid))
        if not proj_no:
            return jsonify({"error": "项目不存在"}), 404
        current_root = get_current_upload_root()
        # 按 零件/阶段/分类 分目录存放（空值用占位符 _）
        part_no = _part_no(conn, ptid)
        subpath = build_subpath(part_no, phase, doc_type)
        folder = os.path.join(current_root, proj_no, subpath)
        os.makedirs(folder, exist_ok=True)
        saved = []
        for f in files:
            original = f.filename or "file"
            # 磁盘存储名保持原文件名；重名时自动加序号，不覆盖历史文件
            storage = _unique_name(folder, _safe_storage_name(original))
            full_path = os.path.join(folder, storage)
            f.save(full_path)
            # storage_name 保存相对子目录 + 文件名，便于按分类定位
            storage_name = os.path.join(subpath, storage)
            conn.execute(
                """INSERT INTO files (project_id, part_id, phase, doc_type, original_name, storage_name, storage_root, uploader_id, uploader_name, size_str, uploaded_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
                (pid, ptid, phase, doc_type, original, storage_name, current_root, u["uid"], u["name"],
                 str(os.path.getsize(full_path)),),
            )
            saved.append(original)
        conn.commit()
        write_audit(u["uid"], "upload", "project %s phase %s doc %s files %s" % (pid, phase, doc_type, saved))
        return jsonify({"ok": True, "count": len(saved)})

    @app.route("/api/files/<int:fid>/download")
    def api_file_download(fid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        r = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not r:
            return jsonify({"error": "文件不存在"}), 404
        if not _can_read(u, r["project_id"], r["phase"], r["doc_type"]):
            return jsonify({"error": "无下载权限"}), 403
        proj_no = _proj_no(conn, r["project_id"])
        path = locate_file(proj_no, r["storage_name"], r["storage_root"] if "storage_root" in r.keys() else None)
        if not path:
            return jsonify({"error": "文件实体缺失"}), 404
        write_audit(u["uid"], "download", "file %s" % r["original_name"])
        return send_file(path, as_attachment=True, download_name=r["original_name"])

    def _can_delete_file(u, file_row):
        # 上传者本人、系统管理员、项目管理员可删除
        if u["role"] == "superAdmin":
            return True
        if file_row["uploader_id"] == u["uid"]:
            return True
        if get_user_project_role(u["uid"], file_row["project_id"]) in ("superAdmin", "projectAdmin"):
            return True
        return False

    @app.route("/api/files/<int:fid>", methods=["DELETE"])
    def api_file_delete(fid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        r = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not r:
            return jsonify({"error": "文件不存在"}), 404
        if not _can_delete_file(u, r):
            return jsonify({"error": "仅上传者、项目管理员或系统管理员可删除"}), 403
        proj_no = _proj_no(conn, r["project_id"])
        path = locate_file(proj_no, r["storage_name"], r["storage_root"] if "storage_root" in r.keys() else None)
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        conn.commit()
        write_audit(u["uid"], "delete_file", "file %s" % r["original_name"])
        return jsonify({"ok": True})
