"""飞书通知模块：将选股结果通过 Webhook 推送至飞书群。"""

import json
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.evaluation.models import EvaluationReport
from sequoia_x.evaluation.report import build_report_sections

logger = get_logger(__name__)

# 采用保守的 UTF-8 请求体上限。长度按 JSON 字节计算，不能按中文字数估算。
# 两部分在通常情况下放在同一张卡片；较长报告按同一编号分卡，发到同一机器人。
_REPORT_CARD_MAX_BYTES = 18_000


class FeishuNotifier:
    """飞书 Webhook 推送器。

    根据策略的 webhook_key 路由到对应的飞书机器人。
    若 webhook_key 未在 Settings.strategy_webhooks 中配置，
    则 fallback 到 Settings.feishu_webhook_url。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 FeishuNotifier。

        Args:
            settings: Settings 实例，提供 Webhook URL 配置。
        """
        self.settings = settings

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    @staticmethod
    def _get_stock_names(symbols: list[str]) -> dict[str, str]:
        """通过 baostock 批量查询股票名称，返回 {code: name} 映射。"""
        import baostock as bs

        bs.login()
        mapping = {}
        for code in symbols:
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            rs = bs.query_stock_basic(code=f"{prefix}.{code}")
            while rs.next():
                row = rs.get_row_data()
                mapping[code] = row[1]  # 第2个字段是股票名称
        bs.logout()
        return mapping

    def _build_card(self, symbols: list[str], strategy_name: str) -> dict:
        today = date.today().strftime("%Y-%m-%d")
        names = self._get_stock_names(symbols)

        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            name = names.get(code, xq_code)
            links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")

        symbol_text = " ".join(links) if links else "（无选股结果）"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📈 Sequoia-X 选股播报 | {strategy_name}",
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**日期：** {today}\n**策略：** {strategy_name}\n"
                                f"**选股数量：** {len(symbols)}"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**选股列表：**\n{symbol_text}",
                        },
                    },
                ],
            },
        }

    def send(
        self,
        symbols: list[str],
        strategy_name: str,
        webhook_key: str = "default",
    ) -> None:
        """
        将选股结果格式化为飞书卡片消息并 POST 至对应 Webhook。

        根据 webhook_key 从 Settings 中查找专属 URL；
        若未配置，则 fallback 到 feishu_webhook_url。

        Args:
            symbols: 选股结果代码列表。
            strategy_name: 策略名称，用于卡片标题。
            webhook_key: 策略标识，用于路由到对应飞书机器人。

        Raises:
            不抛出异常，HTTP 失败时记录 ERROR 日志。
        """
        url = self.settings.get_webhook_url(webhook_key)
        payload = self._build_card(symbols, strategy_name)

        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            # 解析飞书真正的返回体
            resp_json = resp.json()

            # 飞书真正的成功标志是内部的 code == 0
            if resp.status_code != 200 or resp_json.get("code") != 0:
                logger.error(
                    f"飞书推送失败 [{webhook_key}] HTTP状态={resp.status_code} 飞书响应={resp.text}"
                )
            else:
                logger.info(f"飞书推送成功 [{webhook_key}]，共 {len(symbols)} 只股票")

        except requests.RequestException as exc:
            logger.error(f"飞书推送请求异常 [{webhook_key}]：{exc}")

    @staticmethod
    def _split_text(text: str, max_bytes: int = 6000) -> list[str]:
        """优先按行拆分；超长单行按字符拆，保持 Unicode 完整且不丢内容。"""
        chunks: list[str] = []
        current = ""
        current_bytes = 0
        for line in text.splitlines(keepends=True):
            # JSON 会把换行、引号、控制字符转义，因此按序列化成本计算片段大小。
            # 减去外层双引号的两个字节，片段拼接后的转义成本可以直接相加。
            line_bytes = len(json.dumps(line, ensure_ascii=False).encode("utf-8")) - 2
            if line_bytes <= max_bytes:
                if current_bytes + line_bytes > max_bytes:
                    chunks.append(current)
                    current, current_bytes = "", 0
                current += line
                current_bytes += line_bytes
                continue
            for character in line:
                size = len(json.dumps(character, ensure_ascii=False).encode("utf-8")) - 2
                if current_bytes + size > max_bytes:
                    chunks.append(current)
                    current, current_bytes = "", 0
                current += character
                current_bytes += size
        if current:
            chunks.append(current)
        return chunks

    def _build_report_cards(self, report: EvaluationReport) -> list[dict]:
        """组合两部分全文；每个片段都有报告编号和部分标识，便于连续阅读。"""
        first, second = build_report_sections(report)
        report_id = report.generated_at.isoformat(timespec="microseconds")
        title = f"Sequoia-X 综合评估 | {report_id}"

        def make_card(elements: list[dict], part: str = "") -> dict:
            return {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "title": {"tag": "plain_text", "content": title + part},
                        "template": "blue",
                    },
                    "elements": elements,
                },
            }

        cards: list[dict] = []
        elements: list[dict] = []
        for section_index, text in enumerate((first, second), start=1):
            for chunk in self._split_text(text):
                element = {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"（第{section_index}部分）\n{chunk}",
                    },
                }
                trial = make_card(elements + [element], " | 999999/999999")
                size = len(json.dumps(trial, ensure_ascii=False).encode("utf-8"))
                if elements and size > _REPORT_CARD_MAX_BYTES:
                    cards.append(make_card(elements))
                    elements = []
                elements.append(element)
        if elements:
            cards.append(make_card(elements))
        total = len(cards)
        for index, card in enumerate(cards, start=1):
            card["card"]["header"]["title"]["content"] = f"{title} | {index}/{total}"
        return cards

    def send_report(self, report: EvaluationReport) -> bool:
        """将完整评估统一发到 report 机器人，未配置则使用默认机器人。

        新主流程只调用本方法，不再逐策略调用 send()。保留 send() 供旧调用方使用。
        发送前不再查询股票名称，避免报告已生成却因名称 API 故障无法推送。
        发送失败返回 False；主程序以非零退出码提示调度器，报告文件仍可追溯。
        """
        url = self.settings.get_webhook_url("report")
        cards = self._build_report_cards(report)
        for index, card in enumerate(cards, start=1):
            if index > 1:
                # 大报告顺序发送，避免在短时间内集中请求同一个机器人。
                import time

                time.sleep(1)
            try:
                response = requests.post(
                    url,
                    data=json.dumps(card, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                body = response.json()
                if (
                    response.status_code != 200
                    or not isinstance(body, dict)
                    or body.get("code") != 0
                ):
                    logger.error(f"综合报告第 {index}/{len(cards)} 张卡片推送失败")
                    return False
            except (requests.RequestException, ValueError):
                logger.error(f"综合报告第 {index}/{len(cards)} 张卡片请求或响应解析失败")
                return False
        logger.info(f"综合报告推送成功，共 {len(cards)} 张卡片")
        return True
