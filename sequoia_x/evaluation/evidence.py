"""外部依据接口：接入新闻、公告、财报或市场数据聚合服务。

接口负责提供事实和来源，模型负责解释事实。默认不进行无来源的模型联网，
也不把模型训练记忆当作今日新闻。可继承 BaseEvidenceProvider 对接其他平台；
配置实现同时支持本地 JSON 和 HTTP 服务，输入格式详见 README。
"""

import json
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
from pydantic import ValidationError

from sequoia_x.core.config import Settings
from sequoia_x.evaluation.models import ExternalEvidence


class BaseEvidenceProvider(ABC):
    """外部数据提供者。错误说明不得包含凭据或完整 API 响应。"""

    @abstractmethod
    def fetch(
        self,
        symbols: list[str],
        as_of: datetime,
    ) -> tuple[list[ExternalEvidence], list[str]]:
        """返回证据与数据缺失说明；证据必须含时区、来源和 HTTP(S) URL。"""
        ...


class ConfiguredEvidenceProvider(BaseEvidenceProvider):
    """根据配置获取 JSON 证据；一个来源失败不影响另一个来源。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def fetch(
        self,
        symbols: list[str],
        as_of: datetime,
    ) -> tuple[list[ExternalEvidence], list[str]]:
        evidence: list[ExternalEvidence] = []
        notices: list[str] = []
        if not self.settings.external_evidence_path and not self.settings.external_evidence_url:
            return [], ["未配置外部证据来源，本次只能依据项目内的规则与行情评估。"]

        if self.settings.external_evidence_path:
            try:
                raw = json.loads(Path(self.settings.external_evidence_path).read_text("utf-8"))
                evidence.extend(self._parse(raw, notices))
            except (OSError, ValueError):
                notices.append("本地外部证据文件读取失败。")

        if self.settings.external_evidence_url:
            try:
                url = self.settings.external_evidence_url
                if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                    raise ValueError("外部证据地址必须为 HTTP(S) URL")
                headers = {}
                key = self.settings.external_evidence_api_key.get_secret_value()
                if key:
                    headers["Authorization"] = f"Bearer {key}"
                # API 接收全部候选代码和本次评估截止时刻，响应为 {"evidence": [...]}。
                response = requests.post(
                    url,
                    json={"symbols": symbols, "as_of": as_of.isoformat()},
                    headers=headers,
                    timeout=self.settings.external_evidence_timeout,
                    allow_redirects=False,
                )
                if response.status_code != 200:
                    raise ValueError("外部证据服务未成功返回")
                evidence.extend(self._parse(response.json(), notices))
            except (requests.RequestException, ValueError):
                notices.append("外部证据 API 获取失败。")
        return evidence, notices

    @staticmethod
    def _parse(raw: object, notices: list[str]) -> list[ExternalEvidence]:
        """逐条校验：一条格式错误不能使整个来源的有效资料丢失。"""
        if not isinstance(raw, dict) or not isinstance(raw.get("evidence"), list):
            raise ValueError("证据根对象必须包含 evidence 数组")
        items: list[ExternalEvidence] = []
        invalid = 0
        for item in raw["evidence"]:
            try:
                items.append(ExternalEvidence.model_validate(item))
            except ValidationError:
                invalid += 1
        if invalid:
            notices.append(f"已忽略 {invalid} 条格式或时间字段无效的外部证据。")
        return items


def usable_evidence(
    evidence: list[ExternalEvidence],
    symbols: list[str],
    as_of: datetime,
    settings: Settings,
) -> tuple[list[ExternalEvidence], list[str]]:
    """统一过滤未来资料、过期资料和不属于候选的资料。

    截止时刻取模型调用前的快照时间；外部接口不能用调用期间新出现的资料改变
    这份快照。发生重复 ID 时全部丢弃该 ID，避免同一引用指向不同事实。
    """
    cutoff = as_of - timedelta(days=settings.external_evidence_max_age_days)
    counts: dict[str, int] = {}
    for item in evidence:
        counts[item.evidence_id] = counts.get(item.evidence_id, 0) + 1
    selected = [
        item
        for item in evidence
        if (item.symbol in symbols or item.symbol == "*")
        and (cutoff <= item.published_at <= item.available_at <= as_of)
        and counts[item.evidence_id] == 1
    ]
    ignored = len(evidence) - len(selected)
    notices = [f"已忽略 {ignored} 条未来、过期、无关或 ID 重复的外部证据。"] if ignored else []
    return sorted(selected, key=lambda item: item.published_at, reverse=True), notices
