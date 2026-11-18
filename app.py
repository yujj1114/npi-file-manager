# -*- coding: utf-8 -*-
"""
NPI 工程文件管理系统 v5.0 —— 统一后端入口

整合原主服务 + 增强服务全部功能于单一 Flask 应用（端口 5000），
复用原有 npi.db 与 uploads/ 目录，技术栈不变（Flask + 原生 HTML/JS + PyInstaller 打包）。
"""
import os
import sys
import time
import socket
import secrets

from flask import Flask, render_template, jsonify, request, session, make_response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.db import (TEMPLATE_DIR, DB_PATH, DATA_DIR, ensure_dirs, ensure_v5_schema, close_db_teardown)
from core.auth import register_auth_routes
from core.projects import register_project_routes
from core.files import register_file_routes
from core.permissions import register_perm_routes
from core.overviews import register_overview_routes
from core.users import register_user_routes
from core.positions import register_position_routes
from core.phase_templates import register_phase_template_routes
from core.system_config import register_system_config_routes
from core.models3d import register_model3d_routes

ensure_dirs()

# 静态资源目录（three.js 等，本地内置以便离线/内网使用）。
# PyInstaller 单文件模式下 datas 解压到 sys._MEIPASS，需显式拼接，否则打包后 404。
_BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
_STATIC_DIR = os.path.join(_BASE_DIR, "static")

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=_STATIC_DIR)
app.config["JSON_SORT_KEYS"] = False  # 保持 dict 响应顺序（如 /api/phase-templates 按业务排序）
app.json.sort_keys = False

# 会话签名密钥：严禁硬编码默认值（否则攻击者可伪造 session 绕过全部权限校验）。
# 优先使用环境变量 NPI_SECRET；否则在数据库同目录持久化随机密钥文件，保证重启后会话稳定。
_env_secret = os.environ.get("NPI_SECRET")
if _env_secret:
    app.secret_key = _env_secret
else:
    _secret_path = os.path.join(os.path.dirname(DB_PATH), "npi_secret.key")
    _sk = ""
    try:
        if os.path.exists(_secret_path):
            with open(_secret_path, "r") as _f:
                _sk = _f.read().strip()
    except Exception:
        _sk = ""
    if not _sk:
        _sk = secrets.token_hex(32)
        try:
            with open(_secret_path, "w") as _f:
                _f.write(_sk)
            try:
                os.chmod(_secret_path, 0o600)
            except Exception:
                pass
        except Exception:
            pass
    app.secret_key = _sk
app.teardown_appcontext(close_db_teardown)

@app.after_request
def add_no_cache_headers(response):
    """默认禁止浏览器缓存，确保前端修改能及时生效。
    例外：3D 模型 GLB（内容按模型 id 稳定，仅在 retry 时重生成）——
    保留视图设置的 Cache-Control(private, max-age=0, must-revalidate)，
    让浏览器缓存并强制重校验；未变化时服务端只回 304，避免并发浏览重复整份下载。"""
    if request.path.endswith("/glb") and response.mimetype == "model/gltf-binary":
        return response
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.before_request
def _enforce_must_change():
    """强制修改初始密码：未改密前仅允许登录/登出/改密/健康检查/读取身份信息。"""
    p = request.path
    if not p.startswith("/api/"):
        return
    if p in ("/api/login", "/api/logout", "/api/change-password", "/api/health", "/api/me"):
        return
    uid = session.get("uid")
    if not uid:
        return
    try:
        conn = get_db()
        u = conn.execute("SELECT must_change_password FROM users WHERE uid=?", (uid,)).fetchone()
        if u and u["must_change_password"]:
            return jsonify({"error": "请先修改初始密码", "must_change": True}), 403
    except Exception:
        pass

register_auth_routes(app)
register_project_routes(app)
register_file_routes(app)
register_perm_routes(app)
register_overview_routes(app)
register_user_routes(app)
register_position_routes(app)
register_phase_template_routes(app)
register_system_config_routes(app)
register_model3d_routes(app)


@app.route("/")
def index():
    resp = make_response(render_template("main.html"))
    # 禁止缓存首页，避免更新后浏览器仍加载旧 JS（表现为「要点两次才展开」等旧逻辑）
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "db": os.path.exists(DB_PATH), "db_path": DB_PATH})


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在"}), 404
    resp = make_response(render_template("main.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


if __name__ == "__main__":
    def _log_path():
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
        return os.path.join(base, "npi_server.log")

    def _log(msg):
        try:
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + str(msg) + "\n")
        except Exception:
            pass

    def _acquire_lock():
        """跨进程排他锁，防止多实例同时运行。失败返回 (None, False)。"""
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
        lock_path = os.path.join(base, ".npi_lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError:
            return None, False
        try:
            import msvcrt  # Windows
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return fd, True
        except ImportError:
            try:
                import fcntl  # Unix / macOS
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, True
            except OSError:
                os.close(fd)
                return None, False
        except (OSError, IOError):
            os.close(fd)
            return None, False

    def _release_lock(fd):
        try:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except ImportError:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _port_free(port):
        """严格探测端口是否被占用（禁用 SO_REUSEADDR，监听中的端口应返回 False）。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        except Exception:
            pass
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    def _fail(msg):
        print(msg)
        _log(msg)
        try:
            input("按回车退出...")
        except Exception:
            pass
        sys.exit(1)

    port = int(os.environ.get("NPI_PORT", "5000"))
    _log("=== NPI v5.0 启动 === DB: %s | Port: %d" % (DB_PATH, port))

    lock_fd, locked = _acquire_lock()
    if not locked:
        _fail("[NPI v5.0] 已有 NPI 服务实例在运行（检测到 .npi_lock 锁）。"
              "请勿重复启动；如需重启，请先关闭旧实例（任务管理器结束 NPI_Server.exe）。")

    # v5.0 自动迁移：若所连“原来系统”数据库缺 v5 结构，启动时自动补齐（只增不删，数据无损）
    try:
        if ensure_v5_schema():
            _log("数据库结构已确认/升级为 v5.0 兼容")
        else:
            _log("数据库结构检查完成（无需变更或自动跳过）")
    except Exception as e:
        _log("数据库结构检查异常(已忽略): %s" % e)

    if not _port_free(port):
        _release_lock(lock_fd)
        _fail("[NPI v5.0] 端口 %d 已被占用，无法启动。请先关闭占用该端口的程序，"
              "或设置环境变量 NPI_PORT 改用其他端口后重试。" % port)

    from core.db import get_upload_roots
    print("[NPI v5.0] DB: %s" % DB_PATH)
    print("[NPI v5.0] Uploads: %s" % get_upload_roots())
    print("[NPI v5.0] Listening on http://127.0.0.1:%d" % port)
    _log("服务启动成功，监听端口 %d" % port)

    # ------------------------------------------------------------------
    # WSGI 服务器：优先 waitress（生产级），不可用时退回 Flask 内置服务器。
    # 不用内置 app.run 的原因：它是开发服务器，每请求新建线程且无上限，
    # 遇到慢客户端/大文件上传会无界创建线程；且缺少请求体上限与通道超时控制。
    # 注意 waitress 仍是单进程多线程（GIL 限制），换它提升的是抗压稳定性
    # 与资源可控性，不提升吞吐量。
    # ------------------------------------------------------------------
    threads = int(os.environ.get("NPI_THREADS", "0")) or min(32, (os.cpu_count() or 4) * 2)
    try:
        from waitress import serve as _serve
    except ImportError:
        _serve = None

    if _serve is None:
        _log("未安装 waitress，退回 Flask 内置服务器（仅建议开发环境使用）")
        try:
            app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
        except OSError as e:
            _log("启动失败: %s" % e)
            _fail("[NPI v5.0] 服务启动失败: %s" % e)
        finally:
            try:
                _release_lock(lock_fd)
            except Exception:
                pass
    else:
        _log("WSGI 服务器: waitress | 线程池=%d | 请求体上限=4GB | 通道超时=600s" % threads)
        print("[NPI v5.0] WSGI server: waitress, threads=%d" % threads)
        try:
            _serve(
                app,
                host="0.0.0.0",
                port=port,
                threads=threads,
                # NPI 有 DWG/STEP 等大文件，上传上限放宽到 4GB（waitress 默认 1GB）
                max_request_body_size=4 * 1024 * 1024 * 1024,
                # 大文件上传/下载耗时可能较长，通道超时放宽，避免中途被切断
                channel_timeout=600,
                # 连接数上限，防止连接耗尽
                connection_limit=256,
                cleanup_interval=30,
                ident="NPI_Server",
            )
        except OSError as e:
            _log("启动失败: %s" % e)
            _fail("[NPI v5.0] 服务启动失败: %s" % e)
        finally:
            try:
                _release_lock(lock_fd)
            except Exception:
                pass
