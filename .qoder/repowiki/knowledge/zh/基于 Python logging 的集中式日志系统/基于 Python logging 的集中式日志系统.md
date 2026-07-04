---
kind: logging_system
name: 基于 Python logging 的集中式日志系统
category: logging_system
scope:
    - '**'
source_files:
    - middle-platform-data-collector-master/web/app.py
    - middle-platform-data-collector-master/scrapers/base.py
---

本项目使用 Python 标准库 logging 模块实现统一的日志系统，采用集中配置、分层命名空间、双输出（文件 + 控制台）的模式。

核心架构：
- 初始化入口：所有日志配置集中在 web/app.py 的 _setup_logging() 函数中，通过 create_app() 在应用启动时调用。该函数负责创建 logs/ 目录、调用 logging.basicConfig() 统一配置全局根 logger、设置默认级别为 INFO、注册两个 Handler：FileHandler 写入 logs/app.log，StreamHandler 输出到 stdout。
- Logger 命名规范：各模块通过 logging.getLogger(__name__) 获取子 logger，形成层级命名空间（如 scrapers.api_grafana、models.database）。爬虫基类 BaseScraper 进一步将 logger 前缀化为 scraper.{PLATFORM_NAME}，便于按平台区分日志来源。
- 日志格式：统一格式为 %(asctime)s [%(name)s] %(levelname)s: %(message)s，包含时间戳、logger 名称、级别和消息文本。

使用模式：
- 模块顶部 import logging 并声明 logger = logging.getLogger(__name__)
- 使用 logger.info/warning/error/debug 记录结构化信息，参数通过 %s 格式化传入
- 异常场景优先使用 logger.error(..., e) 或 logger.warning(..., e) 记录异常对象
- 爬虫基类提供 self.logger 实例属性，子类可直接复用

输出与存储：
- 文件输出：logs/app.log，UTF-8 编码，无轮转策略，单文件持续追加
- 控制台输出：stdout，便于开发调试
- 无第三方日志框架：未引入 loguru、structlog 等外部依赖

开发者规范：
1. 新增模块需遵循 logger = logging.getLogger(__name__) 模式
2. 避免直接操作根 logger，使用模块级子 logger
3. 关键业务流程用 info，可恢复异常用 warning，严重错误用 error
4. 敏感信息（密码、token）不得写入日志
5. 如需调整日志级别，修改 web/app.py 中 basicConfig(level=...) 即可全局生效