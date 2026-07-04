---
kind: dependency_management
name: Python依赖管理：requirements.txt与虚拟环境
category: dependency_management
scope:
    - '**'
source_files:
    - middle-platform-data-collector-master/requirements.txt
    - middle-platform-data-collector-master/.gitignore
    - middle-platform-data-collector-master/start.bat
    - middle-platform-data-collector-master/start_debug.bat
---

该项目采用标准的 Python `pip` + `requirements.txt` 方式进行依赖管理，并配合本地虚拟环境（venv）进行隔离。

### 1. 依赖声明
- **核心文件**：`middle-platform-data-collector-master/requirements.txt`
- **主要依赖**：
  - `playwright>=1.40`：用于浏览器自动化采集。
  - `flask>=3.0`：Web 框架。
  - `pyyaml>=6.0`：配置文件解析。
  - `openpyxl>=3.1`：Excel 文件处理。
  - `aiohttp>=3.9`：异步 HTTP 客户端。
  - `waitress>=3.0`：生产环境 WSGI 服务器。
- **版本策略**：使用最小版本约束（`>=`），允许安装兼容的更高版本，但未使用锁文件（如 `requirements.lock` 或 `Pipfile.lock`）来固定精确版本，这可能导致不同环境下的构建不一致。

### 2. 环境隔离
- **虚拟环境**：项目通过 `.gitignore` 明确排除了 `venv/`、`.venv/` 和 `env/` 目录，表明开发者应在本地创建虚拟环境。
- **启动脚本**：`start.bat` 和 `start_debug.bat` 中包含了 `call venv\Scripts\activate.bat` 命令，自动化了虚拟环境的激活过程，确保依赖在隔离环境中运行。

### 3. 缺失的高级管理特性
- **无锁文件**：未发现 `Pipfile`、`poetry.lock` 或 `conda` 环境文件，说明项目未采用更严格的依赖锁定机制。
- **无私有源配置**：未发现 `pip.conf` 或 `setup.cfg` 中关于私有 PyPI 源的配置，所有依赖均从公共 PyPI 获取。

### 4. 开发建议
- **固定版本**：建议在稳定后生成精确的版本锁文件（例如通过 `pip freeze > requirements.lock`），以确保生产环境的一致性。
- **环境初始化**：新开发者需手动执行 `python -m venv venv` 和 `pip install -r requirements.txt` 来初始化环境。