"""Evaluate the answering pipeline against the golden set.

    docker compose run --rm eval                         # all questions, OpenAI models from .env
    docker compose run --rm eval python -m eval.run_eval --ids home-01,motor-11 --workers 1

Runs the same code path as the chat API (app.rag.answer.answer_question) against the documents ingested in
the database, as a user with each item's role. Nothing is written to the database. Results are scored with
simple, explainable checks (see eval/README.md) and written to eval/reports/<timestamp>.{json,md}.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import get_sessionmaker
from app.log import configure_logging
from app.rag.answer import AnswerResult, answer_question
from app.rag.grounding import DECISION_LANGUAGE
from app.rag.scope import Selection
from app.security import Principal

EVAL_DIR = Path(__file__).resolve().parent
GOLDEN_SET = EVAL_DIR / "golden_set.jsonl"
REPORTS = EVAL_DIR / "reports"
# The golden set describes the documents as of this date (which wording versions and endorsements are in force).
DEFAULT_TODAY = date(2026, 10, 12)


def load_items(path: Path = GOLDEN_SET) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _norm(text: str) -> str:
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text.lower())  # "£1,000" == "£1000"
    return re.sub(r"\s+", " ", text.replace("’", "'"))


@dataclass
class ItemResult:
    id: str
    category: str
    expect: str
    status: str
    passed: bool
    checks: dict[str, bool | None] = field(default_factory=dict)
    answer: str = ""
    error: str | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0
    claims_checked: int = 0
    claims_removed: int = 0


def score(item: dict[str, Any], result: AnswerResult) -> ItemResult:
    a = result.answer
    text = " ".join([a.get("summary", ""), *(c["text"] for c in a.get("claims", [])), a.get("message", "")])
    checks: dict[str, bool | None] = {}

    expect = item["expect"]
    checks["outcome"] = result.status != "error" and (expect == "any" or result.status == expect)
    if expect == "answered" and result.status == "answered":
        answer = _norm(text)
        checks["facts"] = all(
            any(_norm(alt) in answer for alt in options) for options in item.get("facts", [])
        )
        evidence = _norm(item["evidence"]) if item.get("evidence") else None
        if evidence:
            checks["citation"] = any(evidence in _norm(s["excerpt"]) for s in a["sources"].values())
        wording = next((d for d in a["documents"] if d["kind"] == "wording"), None)
        if item.get("version") and wording:
            checks["version"] = wording["version_label"] == item["version"] and wording["region"] == item.get(
                "region", "UK"
            )
    if item.get("evidence") and result.retrieved:
        evidence = _norm(item["evidence"])
        checks["retrieval"] = any(evidence in _norm(d.page_content) for d in result.retrieved)
    if item.get("no_decision") or result.status == "answered":
        checks["no_decision"] = not DECISION_LANGUAGE.search(text)

    verification = a.get("verification", {})
    return ItemResult(
        id=item["id"],
        category=item["category"],
        expect=expect,
        status=result.status,
        passed=all(v for k, v in checks.items() if k != "retrieval" and v is not None),
        checks=checks,
        answer=text.strip()[:600],
        error=a.get("code") if result.status == "error" else None,
        latency_ms=result.latency_ms,
        cost_usd=round(result.usage.cost_usd, 6),
        claims_checked=verification.get("claims_checked", 0),
        claims_removed=verification.get("claims_removed", 0),
    )


def run_item(item: dict[str, Any], today: date) -> ItemResult:
    principal = Principal(
        uuid.uuid4(),
        f"eval-{item['role']}@jigsaw-insurance.com",
        "Evaluation",
        item["role"],
        uuid.uuid4(),
        False,
    )
    db = get_sessionmaker()()
    try:
        result = answer_question(db, principal, item["question"], Selection(**item["selection"]), today=today)
        return score(item, result)
    finally:
        db.rollback()
        db.close()


def _rate(values: list[bool]) -> dict[str, Any]:
    return {"rate": round(sum(values) / len(values), 3) if values else None, "n": len(values)}


def summarise(results: list[ItemResult]) -> dict[str, Any]:
    def checked(name: str, subset: list[ItemResult] | None = None) -> dict[str, Any]:
        return _rate([r.checks[name] for r in (subset or results) if r.checks.get(name) is not None])

    answerable = [r for r in results if r.expect == "answered"]
    unanswerable = [r for r in results if r.expect == "abstained"]
    latencies = sorted(r.latency_ms for r in results if r.status != "error")
    by_category: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_category[r.category].append(r.passed)
    return {
        "items": len(results),
        "passed": _rate([r.passed for r in results]),
        "answerable_answered": _rate([r.status == "answered" for r in answerable]),
        "fact_accuracy": checked("facts"),
        "retrieval_hit": checked("retrieval"),
        "citation_hit": checked("citation"),
        "correct_version": checked("version"),
        "unanswerable_abstained": _rate([r.status == "abstained" for r in unanswerable]),
        "clarification_correct": _rate(
            [r.status == "clarification" for r in results if r.expect == "clarification"]
        ),
        "no_coverage_decisions": checked("no_decision"),
        "errors": sum(r.status == "error" for r in results),
        "claims_removed_by_verification": f"{sum(r.claims_removed for r in results)} of {sum(r.claims_checked for r in results)}",
        "latency_ms": {
            "p50": statistics.median(latencies) if latencies else None,
            "p95": latencies[int(0.95 * (len(latencies) - 1))] if latencies else None,
        },
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
        "by_category": {k: _rate(v) for k, v in sorted(by_category.items())},
    }


def markdown(summary: dict[str, Any], results: list[ItemResult], config: dict[str, Any]) -> str:
    def pct(m: dict[str, Any]) -> str:
        return "n/a" if m["rate"] is None else f"{m['rate']:.0%} (n={m['n']})"

    reranker = f"`{config['reranker']}`" if config.get("reranker") else "off"
    lines = [
        f"# Evaluation report - {config['run_at']}",
        "",
        f"Chat model `{config['chat_model']}` (reasoning effort {config['reasoning_effort']}), embeddings "
        f"`{config['embedding_model']}`, top-k {config['top_k']}, "
        f"reranker {reranker}, "
        f"LLM judge {'on' if config['judge'] else 'off'}, documents as of {config['today']}.",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Items passed (all checks) | {pct(summary['passed'])} |",
        f"| Answerable questions answered | {pct(summary['answerable_answered'])} |",
        f"| Fact accuracy (answered) | {pct(summary['fact_accuracy'])} |",
        f"| Retrieval hit (evidence in top {config['top_k']}) | {pct(summary['retrieval_hit'])} |",
        f"| Citation hit (evidence in a cited source) | {pct(summary['citation_hit'])} |",
        f"| Correct policy version / region | {pct(summary['correct_version'])} |",
        f"| Unanswerable questions abstained | {pct(summary['unanswerable_abstained'])} |",
        f"| Clarification when ambiguous | {pct(summary['clarification_correct'])} |",
        f"| No coverage decisions in answers | {pct(summary['no_coverage_decisions'])} |",
        f"| Errors | {summary['errors']} |",
        f"| Claims removed by verification | {summary['claims_removed_by_verification']} |",
        f"| Latency p50 / p95 | {summary['latency_ms']['p50']} ms / {summary['latency_ms']['p95']} ms |",
        f"| Estimated model cost | ${summary['cost_usd']} |",
        "",
        "## By category",
        "",
        "| Category | Passed |",
        "|---|---|",
        *[f"| {k} | {pct(v)} |" for k, v in summary["by_category"].items()],
        "",
        "## Items that did not pass",
        "",
    ]
    failed = [r for r in results if not r.passed]
    if not failed:
        lines.append("None.")
    for r in failed:
        failing = ", ".join(k for k, v in r.checks.items() if v is False) or r.error or "outcome"
        lines.append(
            f"- **{r.id}** (expected {r.expect}, got {r.status}; failed: {failing}): {r.answer[:250]}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ids", help="comma-separated item ids")
    parser.add_argument("--category")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--today", type=date.fromisoformat, default=DEFAULT_TODAY)
    parser.add_argument("--no-report", action="store_true", help="print the summary only")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging("WARNING")

    items = load_items()
    if args.ids:
        wanted = set(args.ids.split(","))
        items = [i for i in items if i["id"] in wanted]
    if args.category:
        items = [i for i in items if i["category"] == args.category]
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(lambda i: run_item(i, args.today), items))

    summary = summarise(results)
    config = {
        "run_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "chat_model": settings.openai_chat_model,
        "reasoning_effort": settings.openai_reasoning_effort,
        "embedding_model": settings.openai_embedding_model,
        "top_k": settings.retrieval_top_k,
        "reranker": settings.reranker_model if settings.reranker_enabled else None,
        "judge": settings.grounding_judge,
        "today": args.today.isoformat(),
    }
    report = markdown(summary, results, config)
    print(report)
    if not args.no_report:
        REPORTS.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        (REPORTS / f"{stamp}.md").write_text(report, encoding="utf-8")
        (REPORTS / f"{stamp}.json").write_text(
            json.dumps(
                {"config": config, "summary": summary, "results": [asdict(r) for r in results]}, indent=2
            ),
            encoding="utf-8",
        )
        print(f"Report written to eval/reports/{stamp}.md")
    return summary


if __name__ == "__main__":
    main()
