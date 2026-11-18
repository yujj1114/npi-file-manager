# -*- coding: utf-8 -*-
"""
NPI 工程文件管理系统 v5.0 —— 统一后端核心数据库层

设计要点：
  - 复用原有 npi.db（通过 NPI_DB_PATH 环境变量指定，默认指向 v4.0 交付版库）
  - 复用原有 uploads/ 上传目录
  - 请求级连接复用，统一 busy_timeout，避免多实例写锁冲突
"""
import os
import sqlite3
import sys

from flask import g, has_app_context

if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")


def get_db_path():
    """定位 npi.db。优先用环境变量；否则按候选目录查找已有库；最后回退到工作目录。"""
    env = os.environ.get("NPI_DB_PATH")
    if env and os.path.exists(env):
        return env
    candidates = [
        os.getcwd(),
        os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else None,
        BASE_DIR,
        os.path.dirname(BASE_DIR),
        # 默认复用 v4.0 交付版（同一台机器开发/过渡期）
        r"D:\学习\NPI工程文件管理系统_v4.0_交付版\npi.db",
        r"D:\学习\NPI工程文件管理系统_v4.0_部署包\npi.db",
    ]
    for c in candidates:
        if not c:
            continue
        p = c if c.endswith(".db") else os.path.join(c, "npi.db")
        if os.path.exists(p):
            return p
    # 回退：在工作目录新建（首次部署场景）
    return os.path.join(os.getcwd(), "npi.db")


def get_data_dir():
    return os.path.dirname(get_db_path())


DB_PATH = get_db_path()
DATA_DIR = get_data_dir()
DEFAULT_UPLOAD_ROOT = os.path.join(DATA_DIR, "uploads")
DEFAULT_OVERVIEW_DIR = os.path.join(DEFAULT_UPLOAD_ROOT, "overviews")


def _normalize_root(path):
    """把用户配置的相对/绝对路径统一解析为绝对路径（基于 DATA_DIR）。"""
    if not path or not isinstance(path, str):
        return None
    path = path.strip()
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(DATA_DIR, path)
    return os.path.abspath(os.path.normpath(path))


def _load_upload_roots(conn):
    """从 system_config 读取存储路径列表；兼容旧的单条 upload_root。"""
    roots = []
    try:
        r = conn.execute("SELECT value FROM system_config WHERE key='upload_roots'").fetchone()
        if r and r["value"]:
            import json
            val = json.loads(r["value"])
            if isinstance(val, list):
                roots = [_normalize_root(p) for p in val if isinstance(p, str)]
                roots = [p for p in roots if p]
    except Exception:
        roots = []
    if roots:
        return roots
    # 兼容旧配置：单条 upload_root
    try:
        r = conn.execute("SELECT value FROM system_config WHERE key='upload_root'").fetchone()
        if r and r["value"]:
            path = _normalize_root(r["value"])
            if path:
                return [path]
    except Exception:
        pass
    return [DEFAULT_UPLOAD_ROOT]


def get_upload_roots():
    """返回所有有效存储路径列表（默认至少包含默认目录）。"""
    try:
        conn = open_conn()
        roots = _load_upload_roots(conn)
        conn.close()
        return roots
    except Exception:
        return [DEFAULT_UPLOAD_ROOT]


def get_current_upload_root():
    """新文件写入路径：取列表最后一个（用户最后添加的路径）。"""
    roots = get_upload_roots()
    return roots[-1] if roots else DEFAULT_UPLOAD_ROOT


def get_overview_dir(root=None):
    return os.path.join(root or get_current_upload_root(), "overviews")


def _find_existing_file(candidates):
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def _find_recursive(root, project_no, basename):
    """兼容旧系统：旧文件可能嵌套在组件子目录（如 <项目>/<组件(料号)>/<阶段>/<分类>/<文件名>），
    而 DB 仅记录纯文件名。递归按 basename 兜底查找，不改动旧文件布局。"""
    import glob
    pattern = os.path.join(root, project_no, "**", basename)
    try:
        matches = glob.glob(pattern, recursive=True)
    except Exception:
        matches = []
    for m in matches:
        if os.path.isfile(m):
            return m
    return None


def build_subpath(part_no, phase, doc_type):
    """把零件/阶段/分类拼成分级子目录（替换非法分隔符，空值用占位符）。"""
    def seg(v):
        v = (v or "").strip()
        if not v:
            return "_"
        return v.replace("/", "_").replace("\\", "_")
    return os.path.join(seg(part_no), seg(phase), seg(doc_type))


def _resolve_root(path):
    """解析 storage_root 字段：相对路径基于 DATA_DIR。"""
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(DATA_DIR, path)
    return os.path.abspath(os.path.normpath(path))


def locate_file(project_no, storage_name, storage_root=None):
    """
    根据 project_no + storage_name 定位文件实体。
    新格式 storage_name 可含子目录（如 001/DVT/BOM/xxx.ppt），直接定位；
    旧平铺格式自动用 basename 兜底，兼容未迁移的历史数据。
    """
    base = _resolve_root(storage_root) if storage_root else DEFAULT_UPLOAD_ROOT
    candidates = [
        os.path.join(base, project_no, storage_name),
        os.path.join(base, project_no, os.path.basename(storage_name)),
    ]
    for root in get_upload_roots():
        candidates.append(os.path.join(root, project_no, storage_name))
        candidates.append(os.path.join(root, project_no, os.path.basename(storage_name)))
    hit = _find_existing_file(candidates)
    if hit:
        return hit
    # 兼容旧系统：直接/ basename 匹配失败，再递归按文件名在 <项目> 下任意层级查找
    bn = os.path.basename(storage_name)
    for root in set([base] + get_upload_roots()):
        r = _find_recursive(root, project_no, bn)
        if r:
            return r
    return None


def locate_overview(storage_name, storage_root=None):
    """定位概览图实体；storage_root 为空时兼容旧数据。"""
    candidates = []
    if storage_root:
        candidates.append(os.path.join(_resolve_root(storage_root), "overviews", storage_name))
    candidates.append(os.path.join(DEFAULT_OVERVIEW_DIR, storage_name))
    for root in get_upload_roots():
        candidates.append(os.path.join(root, "overviews", storage_name))
    return _find_existing_file(candidates)


def ensure_dirs():
    os.makedirs(DEFAULT_UPLOAD_ROOT, exist_ok=True)
    os.makedirs(DEFAULT_OVERVIEW_DIR, exist_ok=True)
    # 确保所有已配置路径存在
    for root in get_upload_roots():
        try:
            if root != DEFAULT_UPLOAD_ROOT:
                os.makedirs(root, exist_ok=True)
                os.makedirs(os.path.join(root, "overviews"), exist_ok=True)
        except Exception:
            pass


def ensure_v5_schema():
    """v5.0 启动时的幂等迁移：若所连数据库缺少 v5.0 新增结构，自动补齐。

    仅“只增不删”，对原有业务数据（用户/项目/权限/文件索引/审计等）完全无损。
    用于部署包直接连接“原来系统(v4.0)”的数据库场景：启动即自动升级结构，无需在
    目标机手动跑迁移脚本。已为 v5 结构的库则直接跳过（no-op）。
    返回 True 表示已确认为 v5 结构（含本次补齐），False 表示检查异常（不影响启动）。
    """
    try:
        conn = open_conn()
        cur = conn.cursor()
        # 1) project_overviews：项目总览/封面图
        has = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='project_overviews'"
        ).fetchone()
        if not has:
            cur.execute(
                """CREATE TABLE "project_overviews" (
                    project_id    INTEGER NOT NULL,
                    part_id       INTEGER DEFAULT NULL,
                    storage_name  TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    mime          TEXT DEFAULT '',
                    size          INTEGER DEFAULT 0,
                    uploader_id   TEXT,
                    uploaded_at   TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (project_id, part_id)
                )"""
            )
        # 2) project_permissions.phase_permissions：按阶段权限
        cols = [r[1] for r in cur.execute("PRAGMA table_info(project_permissions)")]
        if "phase_permissions" not in cols:
            cur.execute("ALTER TABLE project_permissions ADD COLUMN phase_permissions TEXT")
        # 3) phase_doc_templates.sort_order：阶段模板显示/创建顺序
        cols = [r[1] for r in cur.execute("PRAGMA table_info(phase_doc_templates)")]
        if "sort_order" not in cols:
            cur.execute("ALTER TABLE phase_doc_templates ADD COLUMN sort_order INTEGER DEFAULT 0")
            # 按业务习惯初始化常用阶段顺序（模具/治具 → 开发验证 → MP）
            defaults = {
                "Tooling": 10, "Fixture": 20,
                "P1": 30, "P2": 40, "EVT": 50, "DVT": 60, "PVT": 70,
                "MP": 80,
            }
            for ph, ord_ in defaults.items():
                cur.execute(
                    "UPDATE phase_doc_templates SET sort_order=? WHERE phase=?",
                    (ord_, ph),
                )
        # 3b) 归一化 sort_order：同一阶段所有 doc_type 行的 sort_order 必须一致，
        # 否则按 MIN(COALESCE(sort_order,0)) 取阶段排序时会因某行 0 值被顶到最前。
        # 策略：先为已知阶段补齐默认值；再逐阶段用已有非零最小值统一全行；
        # 仍没有的就按当前全局最大 sort_order +10 递增分配。
        DEFAULT_PHASE_ORDER = {
            "Tooling": 10, "Fixture": 20,
            "P1": 30, "P2": 40, "EVT": 50, "DVT": 60, "PVT": 70,
            "MP": 80,
        }
        for ph, ord_ in DEFAULT_PHASE_ORDER.items():
            cur.execute(
                "UPDATE phase_doc_templates SET sort_order=? WHERE phase=? AND COALESCE(sort_order,0)=0",
                (ord_, ph),
            )
        phase_rows = cur.execute(
            """SELECT phase,
                      MIN(CASE WHEN COALESCE(sort_order,0)=0 THEN NULL ELSE sort_order END) AS so_min
               FROM phase_doc_templates GROUP BY phase"""
        ).fetchall()
        max_so = cur.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM phase_doc_templates"
        ).fetchone()[0]
        next_so = max_so + 10
        for r in phase_rows:
            ph = r["phase"]
            so = r["so_min"] if r["so_min"] is not None else next_so
            cur.execute(
                "UPDATE phase_doc_templates SET sort_order=? WHERE phase=?",
                (so, ph),
            )
            if r["so_min"] is None:
                next_so += 10
        # 4) files / project_overviews：记录文件实际存储根路径，支持多存储路径
        cols = [r[1] for r in cur.execute("PRAGMA table_info(files)")]
        if "storage_root" not in cols:
            cur.execute("ALTER TABLE files ADD COLUMN storage_root TEXT")
        cols = [r[1] for r in cur.execute("PRAGMA table_info(project_overviews)")]
        if "storage_root" not in cols:
            cur.execute("ALTER TABLE project_overviews ADD COLUMN storage_root TEXT")
        # 5) system_config：单条 upload_root 迁移为 upload_roots 列表（幂等）
        import json
        old = cur.execute("SELECT value FROM system_config WHERE key='upload_root'").fetchone()
        new = cur.execute("SELECT value FROM system_config WHERE key='upload_roots'").fetchone()
        if old and old["value"] and not new:
            cur.execute(
                "INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)",
                ("upload_roots", json.dumps([old["value"]])),
            )
        # 6) part_info：零件信息卡（概览图右侧可维护字段），每个零件一条
        has = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='part_info'"
        ).fetchone()
        if not has:
            # 注：NO 是 SQLite 关键字，故列名为 no_
            cur.execute(
                """CREATE TABLE "part_info" (
                    part_id      INTEGER PRIMARY KEY,
                    no_          TEXT DEFAULT '',
                    rev          TEXT DEFAULT '',
                    model        TEXT DEFAULT '',
                    description  TEXT DEFAULT '',
                    process      TEXT DEFAULT '',
                    pd           TEXT DEFAULT '',
                    dqe          TEXT DEFAULT '',
                    stamp_loc    TEXT DEFAULT '',
                    data         TEXT,
                    updated_by   TEXT,
                    updated_at   TEXT
                )"""
            )
        # 早期版本的 part_info 可能缺 no_ / rev / data，按“只增不删”补齐
        cols = [r[1] for r in cur.execute("PRAGMA table_info(part_info)")]
        for _col in ("no_", "rev", "data"):
            if _col not in cols:
                cur.execute("ALTER TABLE part_info ADD COLUMN %s TEXT" % _col)
        # 将旧固定列数据一次性迁移到 JSON data（保留旧列只增不删）
        migrate_rows = cur.execute(
            "SELECT * FROM part_info WHERE data IS NULL OR data=''"
        ).fetchall()
        skip_cols = {"part_id", "updated_by", "updated_at", "data"}
        for row in migrate_rows:
            d = {}
            for k in row.keys():
                if k in skip_cols:
                    continue
                v = row[k]
                if v is not None and v != "":
                    d[k] = str(v)
            if d:
                cur.execute(
                    "UPDATE part_info SET data=? WHERE part_id=?",
                    (json.dumps(d, ensure_ascii=False), row["part_id"]),
                )
        # 6.1) part_info_fields：按项目配置信息卡字段（key/label/sort）
        cur.execute(
            """CREATE TABLE IF NOT EXISTS "part_info_fields" (
                project_id  INTEGER NOT NULL,
                field_key   TEXT NOT NULL,
                label       TEXT NOT NULL,
                sort_order  INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (project_id, field_key)
            )"""
        )
        # 7) projects.hidden：项目隐藏/显示（仅普通用户不可见）
        cols = [r[1] for r in cur.execute("PRAGMA table_info(projects)")]
        if "hidden" not in cols:
            cur.execute("ALTER TABLE projects ADD COLUMN hidden INTEGER DEFAULT 0")
        # 8) part_models：零件 3D 模型（STEP/IGES/STL → GLB 预览）
        #    status: pending(待转换) / ready(已就绪) / failed(转换失败)
        cur.execute(
            """CREATE TABLE IF NOT EXISTS "part_models" (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id     INTEGER NOT NULL,
                part_id        INTEGER NOT NULL,
                storage_name   TEXT NOT NULL,
                glb_name       TEXT DEFAULT '',
                original_name  TEXT NOT NULL,
                src_format     TEXT DEFAULT '',
                size           INTEGER DEFAULT 0,
                status         TEXT DEFAULT 'pending',
                error_msg      TEXT DEFAULT '',
                meta_json      TEXT DEFAULT '',
                uploader_id    TEXT DEFAULT '',
                uploaded_at    TEXT DEFAULT (datetime('now','localtime'))
            )"""
        )
        cur.execute(
            """CREATE INDEX IF NOT EXISTS idx_part_models_part
               ON part_models(project_id, part_id)"""
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        # 已完整库场景无需迁移；异常不阻断启动，仅记录以便排查
        print("[NPI v5.0][WARN] ensure_v5_schema 检查异常(若库已为 v5 结构可忽略): %s" % e)
        return False


def open_conn():
    """新建一个 sqlite 连接（统一配置 row_factory / busy_timeout）。"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def is_closed(conn):
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True
    except Exception:
        return False


def get_db():
    """请求级连接复用：同一请求内多次调用返回同一连接，请求结束自动关闭。"""
    if has_app_context():
        if "db" not in g or g.db is None or is_closed(g.db):
            g.db = open_conn()
        return g.db
    return open_conn()


def close_db_teardown(exc):
    conn = g.pop("db", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
