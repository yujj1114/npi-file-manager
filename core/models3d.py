# -*- coding: utf-8 -*-
"""
零件 3D 模型 API（STEP/IGES/STL → GLB 预览）

设计要点：
  - 转换由**独立的 C++ 可执行** npi_occt_cli.exe 完成（OpenCASCADE），
    Python 端只负责调度与存储，OCCT 的 DLL 不会被打进 NPI_Server.exe。
  - CLI 不存在时功能降级：仍可上传/下载/删除原始模型，只是无法生成预览，
    不会阻塞其它业务。
  - 权限与「零件概览图」保持一致：
      查看 = 项目成员及以上；管理 = 项目管理员，或在该项目任一文件夹有编辑权的成员。
  - 每个零件可有多个 3D 模型（不同于概览图的覆盖式单条）。

目录：uploads/models/            存放上传的原始模型
      uploads/models/glb/        存放转换产物 GLB 与元数据 JSON
"""
import os
import re
import json
import time
import uuid
import shutil
import subprocess
import threading

from flask import request, jsonify, send_file
from core.db import get_db, get_current_upload_root, get_upload_roots, locate_file
from core.auth import get_request_user
from core.permissions import get_user_project_role, is_project_admin, can_edit_project_content
from core.audit import write_audit

# 支持的源格式
ALLOWED_EXT = {"step", "stp", "iges", "igs", "stl", "brep", "brp"}
EXT_LABEL = {
    "step": "STEP", "stp": "STEP",
    "iges": "IGES", "igs": "IGES",
    "stl": "STL",
    "brep": "BREP", "brp": "BREP",
}

# CLI 可执行名（会依次在以下位置查找）
CLI_NAME = "npi_occt_cli.exe" if os.name == "nt" else "npi_occt_cli"
CONVERT_TIMEOUT = 300  # 秒

# ------------------------------------------------------------------ 转换并发锁
# 转换是CPU 密集且耗时的（最长 CONVERT_TIMEOUT 秒）。若不加锁，多人同时首次打开
# 同一个未转换的源文件时，会各自启动一个 CLI 进程重复做同一份计算，白白吃掉多个
# CPU 核；更严重的是多个进程会并发写同一个 .glb 产物，可能把文件写坏。
# 故按缓存键（源文件及其 mtime/size）加锁：同一模型串行转换，不同模型仍可并行。
_CONVERT_LOCKS = {}
_CONVERT_LOCKS_GUARD = threading.Lock()


def _convert_lock_for(key):
    """取得某个缓存键对应的转换锁（懒创建，永不删除）。"""
    with _CONVERT_LOCKS_GUARD:
        lk = _CONVERT_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _CONVERT_LOCKS[key] = lk
        return lk



# ------------------------------------------------------------------ 路径工具

def _models_dir(root=None):
    return os.path.join(root or get_current_upload_root(), "models")


def _glb_dir(root=None):
    return os.path.join(_models_dir(root), "glb")


def _locate_model(storage_name, storage_root=None):
    """定位已上传的原始模型文件（兼容多存储根）。"""
    cands = []
    if storage_root:
        cands.append(os.path.join(storage_root, "models", storage_name))
    for root in get_upload_roots():
        cands.append(os.path.join(root, "models", storage_name))
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


def _locate_glb(glb_name, storage_root=None):
    if not glb_name:
        return None
    cands = []
    if storage_root:
        cands.append(os.path.join(storage_root, "models", "glb", glb_name))
    for root in get_upload_roots():
        cands.append(os.path.join(root, "models", "glb", glb_name))
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


def find_cli():
    """定位 npi_occt_cli 可执行文件。

    查找顺序：
      1) 环境变量 NPI_OCCT_CLI（显式指定）
      2) 运行目录（EXE 同目录，即 01_部署运行/）
      3) 源码目录（开发调试用）
    返回路径或 None。
    """
    env = os.environ.get("NPI_OCCT_CLI")
    if env and os.path.isfile(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))          # 02_源码/core
    bases = [
        os.getcwd(),                                            # 运行目录
        os.path.dirname(here),                                  # 02_源码
        os.path.dirname(os.path.dirname(here)),                 # 工程根
    ]
    for b in bases:
        for sub in ("", "01_部署运行", "bin"):
            # 优先 onedir 目录形式（npi_occt_cli/npi_occt_cli.exe，启动无需解压、快约 20 倍）
            p = os.path.join(b, sub, "npi_occt_cli", CLI_NAME)
            if os.path.isfile(p):
                return p
            p = os.path.join(b, sub, CLI_NAME)
            if os.path.isfile(p):
                return p
    # PATH 中查找
    which = shutil.which(CLI_NAME)
    return which


def cli_available():
    return find_cli() is not None


# ------------------------------------------------------------------ 权限

def _can_view_model(u, pid):
    if u["role"] == "superAdmin":
        return True
    return get_user_project_role(u["uid"], int(pid)) is not None


def _can_manage_model(u, pid):
    """与零件级概览图一致：项目管理员，或在该项目任一文件夹有编辑权的成员。"""
    if u["role"] == "superAdmin":
        return True
    if is_project_admin(u["uid"], int(pid)):
        return True
    return can_edit_project_content(u["uid"], int(pid))


# ------------------------------------------------------------------ 转换

def _runtime_tmp():
    """CLI 解压运行时目录：EXE 是 onefile 打包（含数百 MB OCCT DLL），
    每次启动要解压到 TEMP。C 盘 TEMP 空间紧张会导致解压失败
    （Failed to extract ... return code -1），故重定向到部署目录所在的大盘。"""
    base = os.path.dirname(get_current_upload_root())
    tmp = os.path.join(base, "runtime_tmp")
    try:
        os.makedirs(tmp, exist_ok=True)
    except Exception:
        tmp = None
    return tmp


def _cli_env():
    env = os.environ.copy()
    tmp = _runtime_tmp()
    if tmp:
        env["TEMP"] = env["TMP"] = tmp
    return env


def _run_convert(cli, src_path, glb_path, json_path, unit="mm"):
    """调用 CLI 转换，返回 (ok, meta_dict_or_None, error_msg)。"""
    cmd = [cli, "convert", src_path, glb_path, "--json", json_path, "--unit", unit]
    try:
        # 隐藏控制台窗口（Windows）
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        p = subprocess.run(
            cmd, capture_output=True, timeout=CONVERT_TIMEOUT,
            env=_cli_env(),
            startupinfo=startup if os.name == "nt" else None,
        )
    except subprocess.TimeoutExpired:
        return False, None, "转换超时（超过 %d 秒），模型可能过于复杂" % CONVERT_TIMEOUT
    except Exception as e:
        return False, None, "无法启动转换工具：%s" % e

    meta = None
    if os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = None

    if p.returncode != 0:
        err = (p.stderr or b"").decode("utf-8", "ignore").strip()
        if not err:
            err = (p.stdout or b"").decode("utf-8", "ignore").strip()
        code_msg = {
            2: "无法读取该文件（格式不支持或文件损坏）",
            3: "文件中没有可显示的几何体",
            4: "GLB 写出失败（磁盘空间不足或路径无权限）",
        }.get(p.returncode)
        return False, meta, code_msg or ("转换失败（退出码 %d）%s" % (p.returncode, err[:200]))

    if not os.path.isfile(glb_path):
        return False, meta, "转换未生成 GLB 文件"
    return True, meta, ""


# ------------------------------------------------- 零件文件夹里的 3D 文件（3D 图数据来源）

# 视为「可做 3D 预览」的文件扩展名（与文件上传允许的类型取交集）
DOC_3D_EXT = ("step", "stp", "iges", "igs", "stl", "brep", "brp")


def _ext_of(name):
    name = name or ""
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _file_src_path(row):
    """定位 files 表中某条记录对应的磁盘文件。"""
    conn = get_db()
    pr = conn.execute("SELECT project_no FROM projects WHERE id=?",
                      (row["project_id"],)).fetchone()
    if not pr:
        return None
    root = row["storage_root"] if "storage_root" in row.keys() else None
    return locate_file(pr["project_no"], row["storage_name"], root)


def _file_cache_paths(fid, src_path):
    """按「文件 id + 源文件 mtime/size」生成缓存名：源文件被替换后会自动重转。"""
    try:
        st = os.stat(src_path)
        key = "f%d_%d_%d" % (fid, int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        key = "f%d" % fid
    d = _glb_dir()
    return os.path.join(d, key + ".glb"), os.path.join(d, key + ".json")


def _ensure_file_glb(fid, row):
    """确保该文件已转换为 GLB（带缓存）。返回 (glb_path, meta, err)。"""
    src = _file_src_path(row)
    if not src:
        return None, None, "源文件已丢失（可能已被删除或移动）"
    glb, js = _file_cache_paths(fid, src)

    def _read_cached():
        """产物已存在则返回 (glb, meta)，否则 None。"""
        if not os.path.isfile(glb):
            return None
        meta = None
        if os.path.isfile(js):
            try:
                with open(js, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = None
        return glb, meta

    # 快路径：已有缓存，直接返回且不加锁（读缓存极廉价，避免把命中流量全部串行化）
    hit = _read_cached()
    if hit:
        return hit[0], hit[1], ""

    cli = find_cli()
    if not cli:
        return None, None, "未找到 3D 转换工具 npi_occt_cli.exe"
    try:
        os.makedirs(os.path.dirname(glb), exist_ok=True)
    except Exception:
        pass

    # 慢路径：按缓存键加锁串行转换。
    # 锁键用 glb 路径（内含源文件 mtime/size），源文件被替换后键会变化，
    # 新旧两份缓存可各自独立转换，互不阻塞。
    with _convert_lock_for(glb):
        # 双检：等锁期间可能已被另一个请求转好
        hit = _read_cached()
        if hit:
            return hit[0], hit[1], ""
        # 先写临时产物，成功后再原子改名。
        # 临时名必须保持 .glb / .json 后缀 —— CLI 会按输出文件名的扩展名判断
        # 要不要写二进制 glTF（见 npi_occt_cli.cpp 中对 ".glb" 的比较），
        # 若用 xxx.glb.part 会导致写出 ASCII glTF 内容却命名为 .glb，产物损坏。
        tmp_glb = glb + ".part.glb"
        tmp_js = js + ".part.json"

        def _cleanup():
            for p in (tmp_glb, tmp_js):
                try:
                    os.remove(p)
                except OSError:
                    pass

        try:
            ok, meta, err = _run_convert(cli, src, tmp_glb, tmp_js, "mm")
        except Exception as e:
            _cleanup()
            return None, None, "转换异常：%s" % e
        if not ok or not os.path.isfile(tmp_glb):
            _cleanup()
            return None, meta, err or "转换失败"
        try:
            os.replace(tmp_glb, glb)
            if os.path.isfile(tmp_js):
                os.replace(tmp_js, js)
        except OSError as e:
            _cleanup()
            return None, meta, "转换结果落盘失败：%s" % e
        return glb, meta, ""


# ------------------------------------------------------------------ 路由

def register_model3d_routes(app):

    @app.route("/api/models3d/cli-status")
    def api_models3d_cli_status():
        """前端据此判断是否显示「无法预览」提示。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        cli = find_cli()
        info = {"available": bool(cli), "path": cli or "", "version": ""}
        if cli:
            try:
                startup = None
                if os.name == "nt":
                    startup = subprocess.STARTUPINFO()
                    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                # onefile EXE 首次启动要解压数百 MB，超时给足
                p = subprocess.run([cli, "version"], capture_output=True, timeout=120,
                                   env=_cli_env(), startupinfo=startup)
                info["version"] = (p.stdout or b"").decode("utf-8", "ignore").strip()
            except Exception:
                pass
        return jsonify(info)

    @app.route("/api/models3d")
    def api_models3d_list():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        pid = request.args.get("project_id")
        ptid = request.args.get("part_id")
        if not pid or not ptid:
            return jsonify({"error": "缺少 project_id / part_id"}), 400
        if not _can_view_model(u, pid):
            return jsonify({"error": "无权访问该项目"}), 403
        conn = get_db()
        out = []

        # 数据来源：该零件文件夹里已有的 3D 文件（STEP/IGES/STL/BREP），无需再单独上传
        frows = conn.execute(
            """SELECT f.*, u.name AS uploader_name
               FROM files f LEFT JOIN users u ON f.uploader_id=u.uid
               WHERE f.project_id=? AND f.part_id=?
               ORDER BY f.uploaded_at DESC, f.id DESC""", (pid, ptid)
        ).fetchall()
        for r in frows:
            ext = _ext_of(r["original_name"])
            if ext not in DOC_3D_EXT:
                continue
            fid = r["id"]
            src = _file_src_path(r)
            # 源文件已不在零件文件夹里（被删除/移动）→ 不再作为 3D 图展示
            if not src:
                continue
            meta, converted = {}, False
            glb, js = _file_cache_paths(fid, src)
            if os.path.isfile(glb):
                converted = True
                if os.path.isfile(js):
                    try:
                        with open(js, "r", encoding="utf-8") as fp:
                            meta = json.load(fp)
                    except Exception:
                        meta = {}
            out.append({
                "id": "f%d" % fid,
                "fid": fid,
                "kind": "file",
                "original_name": r["original_name"],
                "src_format": EXT_LABEL.get(ext, ext.upper()),
                "uploader_name": r["uploader_name"] or r["uploader_id"] or "",
                "uploaded_at": r["uploaded_at"],
                "size_str": r["size_str"] or "",
                "status": "ready",          # 首次预览时按需转换（前端显示“正在加载模型…”）
                "converted": converted,     # 是否已有转换缓存
                "glb_url": "/api/models3d/file/%d/glb" % fid,
                "meta_url": "/api/models3d/file/%d/meta" % fid,
                "meta": meta,
            })

        # 只展示零件文件夹里的 3D 文件；默认第一条即最新上传的
        out.sort(key=lambda x: (str(x.get("uploaded_at") or ""), str(x.get("id") or "")),
                 reverse=True)
        return jsonify(out)

    @app.route("/api/models3d/upload", methods=["POST"])
    def api_models3d_upload():
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        pid = request.form.get("project_id")
        ptid = request.form.get("part_id")
        if not pid or not ptid:
            return jsonify({"error": "缺少 project_id / part_id"}), 400
        if not _can_manage_model(u, pid):
            return jsonify({"error": "仅项目管理员或具备该项目编辑权限的成员可上传 3D 模型"}), 403

        f = request.files.get("file")
        if not f:
            return jsonify({"error": "未选择文件"}), 400
        original = f.filename or "model"
        ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
        if ext not in ALLOWED_EXT:
            return jsonify({"error": "不支持的格式：.%s（支持 STEP/IGES/STL/BREP）" % ext}), 400

        unit = (request.form.get("unit") or "mm").strip().lower()
        if unit not in ("mm", "cm", "m", "in", "ft"):
            unit = "mm"

        root = get_current_upload_root()
        m_dir = _models_dir(root)
        g_dir = _glb_dir(root)
        os.makedirs(m_dir, exist_ok=True)
        os.makedirs(g_dir, exist_ok=True)

        ts = time.strftime("%Y%m%d%H%M%S")
        safe = re.sub(r'[\\/:*?"<>|]+', "_", original)
        storage = "%s_%s_%s" % (ts, uuid.uuid4().hex[:6], safe)
        src_path = os.path.join(m_dir, storage)
        f.save(src_path)
        size = os.path.getsize(src_path)

        conn = get_db()
        cur = conn.execute(
            """INSERT INTO part_models
               (project_id, part_id, storage_name, glb_name, original_name,
                src_format, size, status, uploader_id, uploaded_at)
               VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'))""",
            (pid, ptid, storage, "", original, EXT_LABEL.get(ext, ext.upper()),
             size, "pending", u["uid"]),
        )
        mid = cur.lastrowid
        conn.commit()

        # 转换
        cli = find_cli()
        if not cli:
            conn.execute(
                "UPDATE part_models SET status='failed', error_msg=? WHERE id=?",
                ("未找到 3D 转换工具 npi_occt_cli.exe，请将其放入服务运行目录", mid))
            conn.commit()
            write_audit(u["uid"], "upload_model3d",
                        "project %s part %s file %s (no cli)" % (pid, ptid, original))
            return jsonify({"ok": True, "id": mid, "status": "failed",
                            "error": "未找到 3D 转换工具，仅已保存原始文件"})

        glb_name = "%d.glb" % mid
        json_name = "%d.json" % mid
        glb_path = os.path.join(g_dir, glb_name)
        json_path = os.path.join(g_dir, json_name)
        ok, meta, err = _run_convert(cli, src_path, glb_path, json_path, unit)

        if ok:
            conn.execute(
                "UPDATE part_models SET status='ready', glb_name=?, meta_json=?, error_msg='' WHERE id=?",
                (glb_name, json.dumps(meta, ensure_ascii=False) if meta else "", mid))
            conn.commit()
            write_audit(u["uid"], "upload_model3d",
                        "project %s part %s file %s -> %s" % (pid, ptid, original, glb_name))
            return jsonify({"ok": True, "id": mid, "status": "ready", "meta": meta})
        else:
            conn.execute(
                "UPDATE part_models SET status='failed', error_msg=? WHERE id=?",
                (err, mid))
            conn.commit()
            write_audit(u["uid"], "upload_model3d",
                        "project %s part %s file %s failed: %s" % (pid, ptid, original, err))
            return jsonify({"ok": True, "id": mid, "status": "failed", "error": err})

    @app.route("/api/models3d/file/<int:fid>/meta")
    def api_models3d_file_meta(fid):
        """零件文件（STEP/IGES/STL/BREP）的几何元数据；首次调用时按需转换。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            return jsonify({"error": "文件不存在"}), 404
        if not _can_view_model(u, row["project_id"]):
            return jsonify({"error": "无权访问该项目"}), 403
        glb, meta, err = _ensure_file_glb(fid, row)
        if not glb:
            return jsonify({"error": err or "转换失败"}), 500
        return jsonify(meta or {})

    @app.route("/api/models3d/file/<int:fid>/glb")
    def api_models3d_file_glb(fid):
        """零件文件的 GLB 预览；首次调用时按需转换并缓存。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        if not row:
            return jsonify({"error": "文件不存在"}), 404
        if not _can_view_model(u, row["project_id"]):
            return jsonify({"error": "无权访问该项目"}), 403
        glb, meta, err = _ensure_file_glb(fid, row)
        if not glb:
            return jsonify({"error": err or "转换失败"}), 500
        base = (row["original_name"] or "model")
        name = base.rsplit(".", 1)[0] + ".glb"
        resp = send_file(glb, mimetype="model/gltf-binary",
                         as_attachment=False, download_name=name)
        resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        return resp

    @app.route("/api/models3d/<int:mid>/glb")
    def api_models3d_glb(mid):
        """供 three.js 加载的 GLB。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        row = conn.execute("SELECT * FROM part_models WHERE id=?", (mid,)).fetchone()
        if not row:
            return jsonify({"error": "模型不存在"}), 404
        if not _can_view_model(u, row["project_id"]):
            return jsonify({"error": "无权访问"}), 403
        if row["status"] != "ready" or not row["glb_name"]:
            return jsonify({"error": "该模型暂无可预览的转换结果"}), 404
        path = _locate_glb(row["glb_name"])
        if not path:
            return jsonify({"error": "转换结果文件已丢失"}), 404
        resp = send_file(path, mimetype="model/gltf-binary",
                         as_attachment=False,
                         download_name=row["glb_name"])
        # 模型 id 稳定、内容仅在 retry 时重生成：交给浏览器缓存并强制重校验，
        # 未变化时服务端只回 304，避免每个用户每次查看都整份重下 GLB（并发浏览更省带宽/IO）。
        resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        return resp

    @app.route("/api/models3d/<int:mid>/source")
    def api_models3d_source(mid):
        """下载原始模型文件。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        row = conn.execute("SELECT * FROM part_models WHERE id=?", (mid,)).fetchone()
        if not row:
            return jsonify({"error": "模型不存在"}), 404
        if not _can_view_model(u, row["project_id"]):
            return jsonify({"error": "无权访问"}), 403
        path = _locate_model(row["storage_name"])
        if not path:
            return jsonify({"error": "源文件已丢失"}), 404
        return send_file(path, as_attachment=True, download_name=row["original_name"])

    @app.route("/api/models3d/<int:mid>", methods=["DELETE"])
    def api_models3d_delete(mid):
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        row = conn.execute("SELECT * FROM part_models WHERE id=?", (mid,)).fetchone()
        if not row:
            return jsonify({"error": "模型不存在"}), 404
        if not _can_manage_model(u, row["project_id"]):
            return jsonify({"error": "无权删除"}), 403
        # 删除实体文件（尽力而为）
        try:
            p = _locate_model(row["storage_name"])
            if p:
                os.remove(p)
        except Exception:
            pass
        try:
            if row["glb_name"]:
                g = _locate_glb(row["glb_name"])
                if g:
                    os.remove(g)
                j = g[:-4] + ".json" if g and g.endswith(".glb") else None
                if j and os.path.exists(j):
                    os.remove(j)
        except Exception:
            pass
        conn.execute("DELETE FROM part_models WHERE id=?", (mid,))
        conn.commit()
        write_audit(u["uid"], "delete_model3d", "model %s (%s)" % (mid, row["original_name"]))
        return jsonify({"ok": True})

    @app.route("/api/models3d/<int:mid>/retry", methods=["POST"])
    def api_models3d_retry(mid):
        """转换失败后重试（例如刚装好 CLI）。"""
        u = get_request_user()
        if not u:
            return jsonify({"error": "未登录"}), 401
        conn = get_db()
        row = conn.execute("SELECT * FROM part_models WHERE id=?", (mid,)).fetchone()
        if not row:
            return jsonify({"error": "模型不存在"}), 404
        if not _can_manage_model(u, row["project_id"]):
            return jsonify({"error": "无权操作"}), 403
        cli = find_cli()
        if not cli:
            return jsonify({"error": "未找到 3D 转换工具 npi_occt_cli.exe"}), 400
        src = _locate_model(row["storage_name"])
        if not src:
            return jsonify({"error": "源文件已丢失，请重新上传"}), 404
        root = get_current_upload_root()
        g_dir = _glb_dir(root)
        os.makedirs(g_dir, exist_ok=True)
        glb_name = "%d.glb" % mid
        json_name = "%d.json" % mid
        ok, meta, err = _run_convert(cli, src, os.path.join(g_dir, glb_name),
                                     os.path.join(g_dir, json_name), "mm")
        if ok:
            conn.execute(
                "UPDATE part_models SET status='ready', glb_name=?, meta_json=?, error_msg='' WHERE id=?",
                (glb_name, json.dumps(meta, ensure_ascii=False) if meta else "", mid))
            conn.commit()
            return jsonify({"ok": True, "status": "ready", "meta": meta})
        conn.execute("UPDATE part_models SET status='failed', error_msg=? WHERE id=?", (err, mid))
        conn.commit()
        return jsonify({"error": err}), 400
