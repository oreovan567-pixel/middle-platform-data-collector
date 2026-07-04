---
kind: configuration_system
name: YAML + 环境变量配置系统
category: configuration_system
scope:
    - '**'
source_files:
    - middle-platform-data-collector-master/config/config_loader.py
    - middle-platform-data-collector-master/config/config.yaml
    - middle-platform-data-collector-master/config/config.yaml.example
    - middle-platform-data-collector-master/config/api_endpoints.json
---

## 配置系统概述

本项目采用 **YAML 配置文件 + 环境变量覆盖** 的轻量级配置方案，由 `config/config_loader.py` 统一加载、校验并提供访问接口。

### 核心架构

- **配置文件**: `config/config.yaml`（被 `.gitignore` 忽略），模板在 `config/config.yaml.example`
- **加载器**: `config/config_loader.py` 提供单例缓存、字段校验、默认值填充
- **运行时覆盖**: 通过 `set_user_creds_override()` 支持会话级凭证覆盖；部分路径支持环境变量（如 `METABASE_DB_PATH`）
- **动态数据**: 学校列表已从 YAML 迁移至数据库（`models/school.py`），YAML 仅保留首次导入用途

### 配置分层与优先级

1. **用户覆盖层** — `config_loader.set_user_creds_override()` 设置的凭据优先于文件
2. **环境变量层** — 如 `METABASE_DB_PATH` 可覆盖 `database.metabase_db_path`
3. **YAML 文件层** — `config/config.yaml` 中的实际配置
4. **默认值层** — 加载器对缺失字段提供安全默认值（如 `browser.headless=True`, `slow_mo=0`）

### 关键设计决策

- **单例缓存**: `_config_cache` 全局缓存避免重复 I/O，支持 `force_reload=True` 强制刷新
- **严格校验**: `_validate()` 强制要求 `credentials.lida/grafana/main_site` 存在且包含必要字段，metabase 为可选
- **安全分离**: 浏览器无头模式、平台 URL、用户名密码等敏感信息全部外置到 YAML，不在代码中硬编码
- **多源适配**: 同一学校在不同平台使用不同名称（`lida_name`/`grafana_name`/`main_site_name`），通过配置映射解决
- **API 端点发现结果持久化**: `config/api_endpoints.json` 存储自动发现的 Grafana 面板结构（9000+ 行），作为运行时只读数据

### 开发者约定

- 新增配置项需在 `_validate()` 中添加默认值或必填校验
- 新增平台需同步更新 `credentials` 校验逻辑和 `get_credentials(platform)` 调用处
- 敏感配置（密码、token）禁止提交到版本库，应通过环境变量或部署时注入
- 学校相关配置已迁移至数据库，不要在 YAML 中维护学校列表