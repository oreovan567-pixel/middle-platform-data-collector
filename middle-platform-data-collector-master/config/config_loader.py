"""配置文件加载与校验"""
from __future__ import annotations
import os
from pathlib import Path

import yaml


_CONFIG_DIR = Path(__file__).parent
_CONFIG_PATH = _CONFIG_DIR / "config.yaml"
_EXAMPLE_PATH = _CONFIG_DIR / "config.yaml.example"

_config_cache: dict | None = None


def _load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(force_reload: bool = False) -> dict:
    """加载配置文件，优先使用缓存"""
    global _config_cache
    if _config_cache is not None and not force_reload:
        return _config_cache

    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {_CONFIG_PATH}\n"
            f"请复制 {_EXAMPLE_PATH} 为 {_CONFIG_PATH} 并填入真实信息"
        )

    raw = _load_yaml(_CONFIG_PATH)
    _validate(raw)
    _config_cache = raw
    return raw


def _validate(cfg: dict) -> None:
    """校验必填字段"""
    # browser
    browser = cfg.get("browser", {})
    cfg["browser"] = {
        "headless": browser.get("headless", True),
        "slow_mo": browser.get("slow_mo", 0),
        "default_timeout": browser.get("default_timeout", 30000),
    }

    # schools — 学校配置已迁移到数据库，YAML 中仅用于首次导入
    # 不再强制要求 schools 字段

    # credentials
    creds = cfg.get("credentials", {})
    for platform in ("lida", "grafana", "main_site"):
        if platform not in creds:
            raise ValueError(f"配置错误: credentials 缺少平台 '{platform}'")
        p = creds[platform]
        if not p.get("url"):
            raise ValueError(f"配置错误: credentials.{platform}.url 不能为空")
        # api_token 可以为空（grafana 可选）
        if platform != "grafana":
            if not p.get("username") or not p.get("password"):
                raise ValueError(
                    f"配置错误: credentials.{platform} 必须包含 username 和 password"
                )

    # metabase 可选校验（存在时检查必填字段）
    if "metabase" in creds:
        mb = creds["metabase"]
        if not mb.get("url"):
            raise ValueError("配置错误: credentials.metabase.url 不能为空")
        if not mb.get("username") or not mb.get("password"):
            raise ValueError("配置错误: credentials.metabase 必须包含 username 和 password")


def get_schools() -> list[dict]:
    """获取所有学校配置（从数据库读取）"""
    from models.school import School
    return [s.to_dict() for s in School.get_all()]


def get_school(name: str) -> dict | None:
    """按名称获取单个学校配置（从数据库读取）"""
    from models.school import School
    school = School.get_by_name(name)
    return school.to_dict() if school else None


def _get_credentials_base(platform: str) -> dict:
    """获取指定平台的凭证配置"""
    return load_config()["credentials"][platform]


def get_browser_config() -> dict:
    """获取浏览器配置"""
    return load_config()["browser"]


# ── 用户凭证覆盖机制 ──
_user_creds_override: dict | None = None


def set_user_creds_override(creds: dict | None):
    """设置用户级别的凭证覆盖"""
    global _user_creds_override
    _user_creds_override = creds


def get_credentials(platform: str) -> dict:
    """获取凭证：优先使用用户覆盖，回退到 config.yaml"""
    base = _get_credentials_base(platform)
    if _user_creds_override and platform in _user_creds_override:
        user_creds = _user_creds_override[platform]
        if user_creds.get("username") and user_creds.get("password"):
            result = dict(base)
            result["username"] = user_creds["username"]
            result["password"] = user_creds["password"]
            return result
    return base


def get_metabase_db_path() -> Path:
    """获取 metabase.db 路径

    优先级：
    1. 环境变量 METABASE_DB_PATH
    2. config.yaml 中的 database.metabase_db_path
    3. 默认值（data/metabase.db）
    """
    # 环境变量优先
    env_path = os.environ.get("METABASE_DB_PATH")
    if env_path:
        return Path(env_path)

    # config.yaml
    try:
        cfg = load_config()
        db_cfg = cfg.get("database", {})
        cfg_path = db_cfg.get("metabase_db_path")
        if cfg_path:
            return Path(cfg_path)
    except Exception:
        pass

    # 默认：项目 data 目录下
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return Path(data_dir) / "metabase.db"
    return Path(__file__).parent.parent / "data" / "metabase.db"
