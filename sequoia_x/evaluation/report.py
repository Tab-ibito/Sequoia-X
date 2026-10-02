"""将结构化评估装配成两部分报告，并保存完整输入与输出以便追溯。"""

from pathlib import Path
from uuid import uuid4

from sequoia_x.evaluation.models import EvaluationReport

STATUS_LABELS = {
    "complete": "评估完成",
    "partial": "部分评估完成",
    "failed": "评估失败",
    "disabled": "模型接口未启用",
    "no_candidates": "硬规则无候选",
}
DECISION_LABELS = {"buy": "条件式购入推荐", "watch": "观察", "reject": "剔除"}


def build_report_sections(report: EvaluationReport) -> tuple[str, str]:
    """第一部分提供决策与计划，第二部分单独列内部/外部依据及原始引用。

    不使用模型自由生成的整篇 Markdown，避免来源、候选日期和运行状态被改写。
    没有推荐/模型未启用也输出明确的两部分报告。
    """
    buys = [a.symbol for a in report.assessments if a.decision == "buy"]
    first = [
        "**一、今日推荐购入与短线/长线策略**",
        f"评估时间：{report.generated_at.isoformat(timespec='seconds')}",
        f"最新候选行情日期：{report.market_date or '无'}；状态：{STATUS_LABELS[report.status]}",
        f"模型：{report.model or '未配置'}；候选 {len(report.candidates)} 只，"
        f"已评估 {len(report.assessments)} 只，未评估 {len(report.unevaluated_symbols)} 只。",
        "今日购入推荐：" + ("、".join(buys) if buys else "无"),
        "等级：A 优先、B 条件确认、C 观察、D 剔除。等级不是收益概率。",
        "收盘后推荐供后续交易时段复核；计划使用相对条件，后复权价格不能直接下单。",
    ]
    contexts = {c.symbol: c for c in report.candidates}
    for assessment in report.assessments:
        candidate = contexts[assessment.symbol]
        first.extend(
            [
                f"\n**{assessment.symbol}｜{assessment.grade}级｜"
                f"{DECISION_LABELS[assessment.decision]}**",
                f"行情日期：{candidate.market_date or '无'}；"
                f"命中：{'、'.join(candidate.matched_strategies)}",
                assessment.summary,
                "短线（1至10个交易日）："
                f"入场 {assessment.short_term.entry_condition}；"
                f"退出 {assessment.short_term.exit_condition}；"
                f"失效 {assessment.short_term.invalidation}",
                "长线（1至6个月）："
                f"入场 {assessment.long_term.entry_condition}；"
                f"退出 {assessment.long_term.exit_condition}；"
                f"失效 {assessment.long_term.invalidation}",
                "风险：" + "；".join(assessment.risks),
            ]
        )
        if candidate.data_issues:
            first.append("数据限制：" + "；".join(candidate.data_issues))
    if report.unevaluated_symbols:
        first.append("\n未完成评估（不作为购入推荐）：" + "、".join(report.unevaluated_symbols))
    if report.notices:
        first.append("\n运行说明：" + "；".join(report.notices))

    second = [
        "**二、决策依据：项目内部与外部其他依据**",
        "内部依据来自本次规则命中与本地行情；外部依据来自配置的数据提供方，"
        "下列来源链接和时间用于人工核对，本项目未自动核实网页原文。",
    ]
    for assessment in report.assessments:
        candidate = contexts[assessment.symbol]
        second.extend(
            [
                f"\n**{assessment.symbol}｜{assessment.grade}级｜"
                f"{DECISION_LABELS[assessment.decision]}的依据**",
                f"内部判断：{assessment.internal_reason}",
            ]
        )
        internal = {e.evidence_id: e for e in candidate.internal_evidence}
        for evidence_id in dict.fromkeys(assessment.internal_evidence_ids):
            item = internal[evidence_id]
            second.append(f"- [{item.evidence_id}] {item.source}：{item.description}")
        second.append(f"外部判断：{assessment.external_reason}")
        external = {e.evidence_id: e for e in candidate.external_evidence}
        for evidence_id in dict.fromkeys(assessment.external_evidence_ids):
            item = external[evidence_id]
            second.extend(
                [
                    f"- [{item.evidence_id}] {item.source}｜{item.title}",
                    f"  发布：{item.published_at.isoformat()}；"
                    f"可用：{item.available_at.isoformat()}；来源：{item.url}",
                    f"  原始摘要：{item.summary}",
                ]
            )
    for symbol in report.unevaluated_symbols:
        candidate = contexts[symbol]
        second.append(f"\n**{symbol}｜仅有硬规则候选，尚无模型决策**")
        second.extend(f"- {item.description}" for item in candidate.internal_evidence)
        if candidate.data_issues:
            second.append("数据限制：" + "；".join(candidate.data_issues))
    if not report.candidates:
        second.append("本次没有可供评估的候选，未生成个股决策依据。")
    return "\n".join(first), "\n".join(second)


def save_report(report: EvaluationReport, report_dir: str) -> tuple[Path, Path]:
    """保存 JSON 快照和可读 Markdown，唯一文件名防止同日重跑覆盖证据。

    JSON 只保存业务模型，不保存 Settings，因此不会写入模型密钥或机器人 URL。
    飞书发送失败时文件仍保留，可用于查看结果与手工补发。
    """
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"report-{report.generated_at:%Y%m%d-%H%M%S}-{uuid4().hex[:8]}"
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    first, second = build_report_sections(report)
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(f"# Sequoia-X 评估报告\n\n{first}\n\n{second}\n", encoding="utf-8")
    return json_path, markdown_path
