---
kind: error_handling
name: Python 原生异常与日志驱动的错误处理
category: error_handling
scope:
    - '**'
source_files:
    - middle-platform-data-collector-master/config/config_loader.py
    - middle-platform-data-collector-master/models/database.py
    - middle-platform-data-collector-master/scrapers/api_grafana.py
    - middle-platform-data-collector-master/web/app.py
    - middle-platform-data-collector-master/main.py
---

本仓库未定义统一的业务错误类型或中间件，而是采用 Python 标准库的异常体系配合 `logging` 模块进行错误处理。各层职责如下：

1. **配置校验层**（`config/config_loader.py`）
   - 使用 `FileNotFoundError`、`ValueError` 等内置异常表达配置缺失/非法。
   - 通过 `_validate()` 集中校验 `credentials.*` 必填字段，失败即抛出带中文提示的 `ValueError`。
   - 对可选路径读取使用 `try/except Exception: pass` 做静默降级。

2. **数据库持久层**（`models/database.py`）
   - 通过 `@contextmanager get_connection()` 统一事务边界：成功 `commit()`，异常 `rollback()` 后重新 raise。
   - 所有 I/O 操作（文件读写、SQL 执行）均包裹 `try/except Exception as e`，并以 `logger.warning/info` 记录后返回默认值或空结果，保证采集流程不因单点失败中断。

3. **采集器层**（`scrapers/api_grafana.py`、`api_main_site.py` 等）
   - 网络请求、JSON 解析、数值计算分别用 `except Exception` / `except (ValueError, TypeError, ZeroDivisionError)` 捕获，记录 `logger.error/warning/debug` 后返回空字典或兜底值。
   - 数据合理性检查通过设置 `data_anomaly=True` + `anomaly_message` 字符串在返回值中传递，而非抛异常。

4. **Web 应用层**（`web/app.py`）
   - 使用 Flask 的 `@app.before_request` 实现认证拦截，未登录时返回 JSON `{"error": "未登录"}` 或重定向到 `/login`。
   - 全局日志通过 `logging.basicConfig` 同时输出到 `logs/app.log` 和 stdout。
   - 启动入口 `main.py` 对可选依赖 `waitress` 使用 `except ImportError` 回退到 Flask dev server。

5. **约定与约束**
   - 不定义自定义异常类，也不使用 `raise ... from` 链式异常。
   - 对外部不可控调用（HTTP、浏览器、文件系统）一律 try/except 并降级，不在上层冒泡。
   - 业务状态通过返回值中的 `status`、`error_message`、`data_anomaly` 字段表达，而非异常。
   - 无全局错误处理器、无 HTTP 错误码映射、无 panic/recover 等价物。