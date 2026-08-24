"""
Eval scoring script for the Finance Spend Intelligence assistant.

Usage:
    python evals/score.py [--dataset evals/golden_dataset.json]
                          [--api http://localhost:8000]
                          [--threshold 0.7]

Exit code 0 if pass rate >= threshold, else 1 (for CI).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import httpx


# ── Scoring ────────────────────────────────────────────────────────────────────

def score_response(item: dict, response: dict) -> dict:
    answer: str = (response.get("answer") or "").lower()
    evidence: dict = response.get("evidence") or {}
    confidence: str = response.get("confidence", "low")

    # 1. Keyword match against prose answer
    keywords: list[str] = item.get("expected_answer_contains") or []
    hits = [kw for kw in keywords if kw.lower() in answer]
    keyword_score = len(hits) / len(keywords) if keywords else 1.0

    # 2. Policy section cited in evidence
    # Sources have format "§5 5. Currency & Payment Terms" — match on section prefix only
    policy_section: Optional[str] = item.get("policy_section")
    if policy_section:
        section_prefix = policy_section.split(".")[0]  # "5.2" → "5", "4.1" → "4"
        policy_sources: list[str] = evidence.get("policy_sources") or []
        policy_cited = any(section_prefix in src for src in policy_sources)
    else:
        policy_cited = True  # data-only questions — not applicable

    # 3. SQL result count for numeric questions
    # SQL agent returns COUNT(*) as one row {"count_star()": N} — read the value, don't count rows
    expected_row_count: Optional[int] = item.get("expected_row_count")
    if expected_row_count is not None:
        sql_results: list = evidence.get("sql_results") or []
        if sql_results:
            first_row = sql_results[0]
            actual_count = next(iter(first_row.values())) if len(first_row) == 1 else len(sql_results)
        else:
            actual_count = 0
        count_correct: Optional[bool] = (actual_count == expected_row_count)
    else:
        count_correct = None

    # 4. Passed gate
    #    Numeric questions: must also have the right row count
    #    Policy questions: must cite the section
    passed = keyword_score >= 0.5 and policy_cited
    if count_correct is not None:
        passed = passed and count_correct

    return {
        "id": item["id"],
        "question": item["question"],
        "confidence": confidence,
        "keyword_score": round(keyword_score, 2),
        "keywords_hit": hits,
        "keywords_missed": [kw for kw in keywords if kw.lower() not in answer],
        "policy_cited": policy_cited,
        "expected_row_count": expected_row_count,
        "actual_row_count": actual_count if expected_row_count is not None else None,
        "count_correct": count_correct,
        "passed": passed,
    }


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_eval(dataset_path: str, api_base: str, threshold: float) -> int:
    dataset = json.loads(Path(dataset_path).read_text())

    results = []
    print(f"Running {len(dataset)} questions against {api_base}/ask\n")

    for item in dataset:
        qid = item["id"]
        print(f"  {qid}  {item['question'][:60]}…", end=" ", flush=True)
        try:
            r = httpx.post(
                f"{api_base}/ask",
                json={"question": item["question"]},
                timeout=60,
            )
            r.raise_for_status()
            result = score_response(item, r.json())
            status = "✅" if result["passed"] else "❌"
            print(status)
        except Exception as exc:
            print(f"ERROR: {exc}")
            result = {
                "id": qid,
                "question": item["question"],
                "passed": False,
                "error": str(exc),
            }
        results.append(result)

    # ── Summary ────────────────────────────────────────────────────────────────
    passed = [r for r in results if r.get("passed")]
    pass_rate = len(passed) / len(results)

    print(f"\n{'─'*60}")
    print(f"Pass rate: {len(passed)}/{len(results)} ({pass_rate:.0%})")
    print(f"CI gate:   {threshold:.0%}  →  {'PASS' if pass_rate >= threshold else 'FAIL'}")
    print(f"{'─'*60}\n")

    # Detailed failures
    failures = [r for r in results if not r.get("passed")]
    if failures:
        print("Failures:")
        for r in failures:
            if "error" in r:
                print(f"  {r['id']}  [API error] {r['error']}")
                continue
            missed = r.get("keywords_missed") or []
            count_note = ""
            if r.get("expected_row_count") is not None:
                count_note = f"  rows: expected={r['expected_row_count']} got={r['actual_row_count']}"
            print(
                f"  {r['id']}  kw={r['keyword_score']:.0%}"
                f"  policy_cited={r.get('policy_cited')}"
                f"{count_note}"
                f"  missed={missed}"
            )

    # Write JSON report next to dataset
    report_path = Path(dataset_path).with_name("score_report.json")
    report_path.write_text(json.dumps({"pass_rate": pass_rate, "results": results}, indent=2))
    print(f"\nReport written to {report_path}")

    return 0 if pass_rate >= threshold else 1


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score the RAG assistant against golden dataset")
    parser.add_argument("--dataset",   default="evals/golden_dataset.json")
    parser.add_argument("--api",       default="http://localhost:8000")
    parser.add_argument("--threshold", default=0.7, type=float)
    args = parser.parse_args()

    sys.exit(run_eval(args.dataset, args.api, args.threshold))
