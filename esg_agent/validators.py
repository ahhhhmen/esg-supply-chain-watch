"""
esg_agent.validators — Pydantic 配置验证
═══════════════════════════════════════════════════════════════════════════════
使用 Pydantic 对 config.yaml 进行结构化验证，在加载阶段即捕获配置错误。
"""

from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class CompanyConfig(BaseModel):
    """企业配置"""
    name_zh: str
    name_en: str
    ticker: str = ""
    aliases: list[str] = Field(default_factory=list)


class GeographicalTrack(BaseModel):
    """地缘多语种跟踪轨道"""
    lang: str
    lang_label: str = ""
    url_template: str


class PremiumCompanyTrack(BaseModel):
    """定向高风险机构预警轨道"""
    source: str = ""
    company_name_field: str = "name_en"
    url_template: str
    track_label: str = ""


class PremiumGlobalTrack(BaseModel):
    """全球宏观合规政策前沿轨道"""
    source: str = ""
    url: str
    track_label: str = ""


class TopicKeyword(BaseModel):
    """主题关键词（多语种）"""
    category: str
    keywords: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("keywords")
    @classmethod
    def at_least_one_lang(cls, v: dict) -> dict:
        if not v:
            raise ValueError("Topic must have at least one language key")
        return v


class IntelligenceTracks(BaseModel):
    """情报轨道配置"""
    geographical_tracks: list[GeographicalTrack] = Field(default_factory=list)
    premium_company_tracks: list[PremiumCompanyTrack] = Field(default_factory=list)
    premium_global_tracks: list[PremiumGlobalTrack] = Field(default_factory=list)


class Topics(BaseModel):
    """双频主题矩阵"""
    daily: list[TopicKeyword] = Field(default_factory=list)
    weekly: list[TopicKeyword] = Field(default_factory=list)


class ConfigSchema(BaseModel):
    """
    config.yaml 的完整 Pydantic 模式。

    用法:
        from esg_agent.validators import ConfigSchema
        validated = ConfigSchema.model_validate(raw_yaml_data)
    """
    companies: list[CompanyConfig] = Field(min_length=1)
    intelligence_tracks: IntelligenceTracks
    topics: Topics
    days_limit: int = Field(default=14, ge=1, le=60)

    @field_validator("companies")
    @classmethod
    def at_least_one_company(cls, v: list) -> list:
        if not v:
            raise ValueError("At least one company must be configured")
        return v


def validate_config(config_path: str) -> ConfigSchema:
    """
    加载并验证 YAML 配置文件。

    Args:
        config_path: config.yaml 路径

    Returns:
        验证后的 ConfigSchema 对象

    Raises:
        ValidationError: 当配置不符合预期结构时
    """
    import yaml
    from pathlib import Path

    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    return ConfigSchema.model_validate(raw)


def validate_config_or_warn(config_path: str = None) -> Optional[ConfigSchema]:
    """
    验证配置，失败时打印警告而非抛出异常。
    用于运行时友好错误提示。

    Returns:
        验证成功返回 ConfigSchema，失败返回 None
    """
    import logging
    logger = logging.getLogger("esg_agent")

    if config_path is None:
        from pathlib import Path
        config_path = str(Path(__file__).parent.parent / "config.yaml")

    try:
        return validate_config(config_path)
    except Exception as e:
        logger.warning(f"Config validation failed: {e}")
        return None
