# NPI 工程文件管理系统

基于 Flask + SQLite + 原生 JavaScript 的工程文件管理系统，支持项目/阶段/分类多级目录、细粒度权限、审计日志与 3D 模型预览。

## 技术栈
- 后端：Flask + SQLite
- 前端：原生 JavaScript + three.js
- 部署：PyInstaller 打包，waitress 生产服务

## 启动

```bash
pip install -r requirements.txt
python app.py
```

默认监听 5000 端口，增强服务 5001 端口。

## 目录说明

- `app.py` — 入口与路由
- `core/` — 业务模块（数据库、认证、用户、项目、权限、文件、概览、阶段模板、审计、3D 模型）
- `templates/` — 页面模板
- `static/vendor/three/` — 3D 预览依赖
- `NPI_v5.0_系统文档.html` — 系统文档

## 配置

复制 `npi_secret.key.example` 为 `npi_secret.key` 并生成 64 位随机密钥：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

`npi.db` 含真实用户数据，未随仓库发布；首次运行由 `core/db.py` 自动建表。3D 转换依赖独立 OCCT CLI，需自行构建。
