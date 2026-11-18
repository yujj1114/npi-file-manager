# -*- coding: utf-8 -*-
"""
审计日志
"""
from flask import request
from core.db import get_db

ACTION_LABELS = {
    "login": "登录系统",
    "logout": "退出登录",
    "change_password": "修改密码",
    "upload": "上传文件",
    "download": "下载文件",
    "delete_file": "删除文件",
    "rename": "重命名",
    "create_project": "创建项目",
    "rename_project": "重命名项目",
    "create_part": "创建零件",
    "create_phase": "创建阶段",
    "delete_project": "删除项目",
    "delete_part": "删除零件",
    "delete_phase": "删除阶段",
    "create_user": "创建用户",
    "update_user": "编辑用户",
    "delete_user": "删除用户",
    "update_user_role": "修改用户角色",
    "freeze_user": "冻结/解冻用户",
    "reset_password": "重置密码",
    "grant_perm": "授权项目权限",
    "revoke_perm": "撤销项目权限",
    "update_perm": "更新项目权限",
    "save_folder_perm": "保存文件夹权限",
    "batch_grant": "批量授权",
    "upload_overview": "上传概览图",
    "delete_overview": "删除概览图",
    "create_doc_type": "新增文档类型",
    "delete_doc_type": "删除文档类型",
    "create_position": "新增职位",
    "update_position": "编辑职位",
    "toggle_position": "启用/停用职位",
    "delete_position": "删除职位",
    "system_config": "修改系统配置",
}


def write_audit(uid, action, detail=""):
    try:
        conn = get_db()
        ip = request.remote_addr if request else ""
        conn.execute(
            "INSERT INTO audit_log (uid, action, detail, ip, ts) VALUES (?,?,?,?,datetime('now','localtime'))",
            (uid, action, detail, ip),
        )
        conn.commit()
    except Exception:
        pass


def get_action_label(action):
    return ACTION_LABELS.get(action, action)
