"""Run every prompt iteration against every test case and write the results.

    uv run cli-agent-eval --shell cmd
    uv run cli-agent-eval --versions v1 v5 --shell bash --sandbox

Outputs, under results/:
    iteration_1_v1_naive_cmd.csv     one row per test case, one file per iteration
    summary.csv                      one row per iteration
    prompt_engineering_results.xlsx  the same data, one sheet per iteration
                                     -> this is the file you import to Google Sheets

and docs/RESULTS.md, a written report of the trend plus every failing case.

Nothing here fabricates data. If GEMINI_API_KEY is unset the run stops before the
first call, because a results file that was not produced by a real model is worse
than no results file at all.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..agent import AgentResult, Decision, evaluate_output, generate
from ..config import RESULTS_DIR, SHELLS, DOCS_DIR, settings
from ..llm import GeminiLLM, LLMConfigError, LLMError, LLMQuotaExhausted, set_rate_limit
from ..prompts import PROMPT_VERSIONS, ordered_versions
from ..safety import SafetyLevel, SafetyVerdict
from ..sandbox import SandboxResult, run_in_sandbox
from .metrics import VERDICT_PASS, CaseScore, IterationSummary, score_case, summarize
from .test_cases import BY_ID, TEST_CASES, TestCase, cases_for

COLUMNS = [
    "case_id", "category", "instruction",
    "expected_decision", "expected_safety",
    "raw_output", "command",
    "model_decision", "final_decision",
    "guard_level", "guard_reason", "guard_overrode_model",
    "format_ok", "syntax_ok", "correct", "safety_ok",
    "score", "verdict",
    "unsafe_leak", "guard_saved", "over_blocked",
    "notes", "format_issues", "syntax_issues",
    "latency_ms", "prompt_version", "model", "shell", "timestamp",
]
SANDBOX_COLUMNS = ["sandbox_ran", "sandbox_exit_code", "sandbox_ok", "sandbox_output"]


@dataclass
class CaseRun:
    case: TestCase
    result: AgentResult
    score: CaseScore
    sandbox: SandboxResult | None = None

    def as_row(self) -> dict:
        r, s, c = self.result, self.score, self.case
        row = {
            "case_id": c.id,
            "category": c.category,
            "instruction": c.instruction,
            "expected_decision": c.expected_decision.value,
            "expected_safety": c.expected_safety.value if c.expected_safety else "",
            "raw_output": r.raw_output,
            "command": r.command,
            "model_decision": r.model_decision.value,
            "final_decision": r.decision.value,
            "guard_level": r.safety.level.value,
            "guard_reason": r.safety.reason,
            "guard_overrode_model": r.guard_overrode_model,
            "format_ok": s.format_ok,
            "syntax_ok": s.syntax_ok,
            "correct": s.correct,
            "safety_ok": s.safety_ok,
            "score": s.score,
            "verdict": s.verdict,
            "unsafe_leak": s.unsafe_leak,
            "guard_saved": s.guard_saved,
            "over_blocked": s.over_blocked,
            "notes": s.notes,
            "format_issues": ", ".join(r.format_issues),
            "syntax_issues": ", ".join(r.syntax_issues),
            "latency_ms": r.latency_ms,
            "prompt_version": r.prompt_version,
            "model": r.model,
            "shell": r.shell_key,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if self.sandbox is not None:
            output = (self.sandbox.stdout or self.sandbox.stderr or self.sandbox.skipped_reason)
            row |= {
                "sandbox_ran": self.sandbox.ran,
                "sandbox_exit_code": self.sandbox.exit_code,
                "sandbox_ok": self.sandbox.ok,
                "sandbox_output": output[:300].replace("\n", " | "),
            }
        return row


def _error_result(case: TestCase, version: str, shell_key: str, exc: Exception) -> AgentResult:
    return AgentResult(
        instruction=case.instruction,
        prompt_version=version,
        shell_key=shell_key,
        raw_output="",
        decision=Decision.UNSUPPORTED,
        model_decision=Decision.UNSUPPORTED,
        safety=SafetyVerdict(SafetyLevel.SAFE, "", "n/a"),
        # The 429 payload is a 900-character quota essay. The first line says it.
        error=" ".join(str(exc).split())[:180],
    )


def run_case(
    case: TestCase,
    version: str,
    shell_key: str,
    llm: GeminiLLM,
    use_sandbox: bool,
) -> CaseRun:
    try:
        result = generate(case.instruction, prompt_version=version, shell_key=shell_key, llm=llm)
    except LLMQuotaExhausted:
        # Not this case's problem - the run is over. Let it reach main() so that
        # nothing partial is written to results/.
        raise
    except LLMError as exc:
        result = _error_result(case, version, shell_key, exc)

    sandbox: SandboxResult | None = None
    if use_sandbox:
        if result.decision is Decision.COMMAND:
            # `confirmed=True` is honest here: the harness is the user, and it has
            # already decided that RISKY commands may run inside a throwaway container.
            sandbox = run_in_sandbox(result.command, shell_key=shell_key, confirmed=True)
        else:
            sandbox = SandboxResult(False, skipped_reason=f"no command ({result.decision.value})")

    return CaseRun(case=case, result=result, score=score_case(case, result), sandbox=sandbox)


def run_iteration(
    version: str,
    shell_key: str,
    cases: list[TestCase],
    llm: GeminiLLM,
    workers: int,
    use_sandbox: bool,
) -> list[CaseRun]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(run_case, case, version, shell_key, llm, use_sandbox) for case in cases
        ]
        return [f.result() for f in futures]


# ---------------------------------------------------------------------------
# Rescoring a finished experiment, without calling the model again.
#
# Every number in results/ except `raw_output` is a pure function of
# `raw_output`. When a metric turns out to be wrong - and one did, see
# docs/FAILURES.md #2 - the honest fix is to recompute, not to re-run (which
# would silently mix two samples) and certainly not to hand-edit a CSV.
# ---------------------------------------------------------------------------
def load_previous_run(xlsx_path: Path, shell_key: str) -> dict[str, list[CaseRun]]:
    import pandas as pd

    if not xlsx_path.exists():
        raise FileNotFoundError(f"{xlsx_path} does not exist; run the experiment first.")

    # Read every sheet up front and close the file: the report we are about to
    # write goes back to this same path.
    with pd.ExcelFile(xlsx_path) as book:
        sheets = {name: book.parse(name).fillna("") for name in book.sheet_names}

    runs: dict[str, list[CaseRun]] = {}

    for version, meta in sorted(PROMPT_VERSIONS.items(), key=lambda kv: kv[1].order):
        sheet = f"iter{meta.order}_{version}"[:31]
        if sheet not in sheets:
            continue

        iteration: list[CaseRun] = []
        for row in sheets[sheet].to_dict("records"):
            case = BY_ID.get(str(row["case_id"]))
            if case is None:
                continue  # a case that has since been renamed or removed
            raw = str(row.get("raw_output", ""))
            result = evaluate_output(
                instruction=case.instruction,
                raw=raw,
                prompt_version=version,
                shell_key=shell_key,
                model=str(row.get("model", "")),
                latency_ms=int(row.get("latency_ms") or 0),
            )
            if not raw.strip():
                result.error = str(row.get("notes", "")) or "no output recorded"
            iteration.append(CaseRun(case=case, result=result, score=score_case(case, result)))

        if iteration:
            runs[version] = iteration
    return runs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _write_csv(rows: list[dict], path: Path, columns: list[str]) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False, encoding="utf-8-sig")


def _write_xlsx(
    per_iteration: dict[str, list[dict]],
    summaries: list[IterationSummary],
    path: Path,
    columns: list[str],
) -> None:
    import pandas as pd

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame([s.as_row() for s in summaries]).to_excel(
            writer, sheet_name="Summary", index=False
        )
        for version, rows in per_iteration.items():
            order = PROMPT_VERSIONS[version].order
            sheet = f"iter{order}_{version}"[:31]
            pd.DataFrame(rows, columns=columns).to_excel(writer, sheet_name=sheet, index=False)


def _write_results_md(
    summaries: list[IterationSummary],
    runs: dict[str, list[CaseRun]],
    shell_key: str,
    path: Path,
    rescored_from: Path | None = None,
) -> None:
    models = sorted({r.result.model for it in runs.values() for r in it if r.result.model})
    lines: list[str] = [
        "# Results",
        "",
        f"Auto-generated by `cli-agent-eval` on {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        "Do not edit by hand - rerun the harness.",
        "",
        f"- Model: `{', '.join(models) or settings.gemini_model}`, "
        f"temperature `{settings.gemini_temperature}`",
        f"- Target shell: `{shell_key}` ({SHELLS[shell_key].display})",
        f"- Test cases: {len(TEST_CASES)}",
    ]
    if rescored_from is not None:
        lines += [
            "",
            f"> **Scores recomputed offline** from the raw model outputs recorded in "
            f"`{rescored_from.name}`. No new model calls were made. The `raw_output` column "
            "is exactly what the model returned during that run; every other column is a "
            "pure function of it, recomputed with the current metric definitions.",
        ]
    lines += [
        "",
        "## Trend across iterations",
        "",
        "| iteration | title | mean score | pass rate | format | syntax | correct | safety | unsafe leaks | guard saves | over-blocks | malformed | API errors |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in summaries:
        lines.append(
            f"| `{s.prompt_version}` | {s.title} | **{s.mean_score:.2f}** | {s.pass_rate:.0%} | "
            f"{s.format_ok:.2f} | {s.syntax_ok:.2f} | {s.correct:.2f} | {s.safety_ok:.2f} | "
            f"{s.unsafe_leaks} | {s.guard_saves} | {s.over_blocks} | {s.malformed} | {s.errors} |"
        )

    if any(s.errors for s in summaries):
        lines += [
            "",
            "> ⚠️ **This run contains API errors.** An errored case scores 0, which drags the "
            "mean down for reasons that have nothing to do with the prompt. Rerun with a lower "
            "`--rpm` before drawing conclusions from these numbers.",
        ]

    lines += ["", "## Score by category", ""]
    categories = sorted({c for s in summaries for c in s.by_category})
    lines.append("| category | " + " | ".join(f"`{s.prompt_version}`" for s in summaries) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in summaries) + " |")
    for cat in categories:
        cells = " | ".join(f"{s.by_category.get(cat, 0):.2f}" for s in summaries)
        lines.append(f"| {cat} | {cells} |")

    lines += ["", "## Failures, per iteration", ""]
    for summary in summaries:
        version = summary.prompt_version
        failures = [r for r in runs[version] if r.score.verdict != VERDICT_PASS]
        lines.append(f"### `{version}` - {summary.title} ({len(failures)} failing)")
        lines.append("")
        if not failures:
            lines += ["No failures.", ""]
            continue
        for run in failures:
            flag = " 🔴 UNSAFE LEAK" if run.score.unsafe_leak else ""
            instruction = run.case.instruction[:90].replace("\n", " ")
            output = (run.result.raw_output or "(empty)")[:120].replace("\n", " ⏎ ")
            lines += [
                f"- **{run.case.id}** ({run.case.category}, score {run.score.score:.2f}){flag}",
                f"  - in:  `{instruction}`",
                f"  - out: `{output}`",
                f"  - why: {run.score.notes or '-'}",
            ]
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the prompt-iteration experiment.")
    parser.add_argument(
        "--versions", nargs="*", default=None,
        help="Prompt versions to run (default: all, in order).",
    )
    parser.add_argument("--shell", default=settings.default_shell, choices=sorted(SHELLS))
    parser.add_argument("--workers", type=int, default=settings.eval_workers)
    parser.add_argument(
        "--model", default=settings.gemini_model,
        help="Override GEMINI_MODEL for this run. The model used is recorded in every row.",
    )
    parser.add_argument(
        "--rpm", type=int, default=settings.gemini_rpm,
        help="Requests per minute, paced process-wide. Free tiers cap this per model.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N cases.")
    parser.add_argument(
        "--sandbox", action="store_true",
        help="Also execute each generated command in the Docker sandbox (bash only).",
    )
    parser.add_argument(
        "--rescore", nargs="?", type=Path, default=None,
        const=RESULTS_DIR / "prompt_engineering_results.xlsx",
        help="Recompute every metric from a finished run's raw outputs. Makes no model calls.",
    )
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    versions = args.versions or [v.id for v in ordered_versions()]
    unknown = [v for v in versions if v not in PROMPT_VERSIONS]
    if unknown:
        parser.error(f"unknown prompt version(s): {unknown}")

    if args.sandbox and args.rescore:
        parser.error("--sandbox executes commands, so it cannot be combined with --rescore.")
    if args.sandbox and not SHELLS[args.shell].sandbox_capable:
        parser.error(
            f"--sandbox needs a Linux shell; {args.shell!r} commands cannot run in the "
            "sandbox image. Use --shell bash."
        )

    per_iteration: dict[str, list[dict]] = {}
    runs: dict[str, list[CaseRun]] = {}
    summaries: list[IterationSummary] = []
    columns = COLUMNS + (SANDBOX_COLUMNS if args.sandbox else [])

    if args.rescore:
        try:
            runs = load_previous_run(args.rescore, args.shell)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"rescoring {args.rescore.name} offline - no model calls\n", flush=True)
    else:
        set_rate_limit(args.rpm)
        try:
            llm = GeminiLLM(model=args.model)
        except LLMConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        cases = cases_for(args.shell)[: args.limit]
        total_calls = len(versions) * len(cases)
        print(
            f"{args.model}: {total_calls} calls at {args.rpm} rpm -> about "
            f"{total_calls / max(args.rpm, 1):.0f} min\n",
            flush=True,
        )
        for version in versions:
            meta = PROMPT_VERSIONS[version]
            print(f"[{version}] {meta.title} - {len(cases)} cases on {args.shell} ...", flush=True)
            try:
                runs[version] = run_iteration(
                    version, args.shell, cases, llm, args.workers, args.sandbox
                )
            except LLMQuotaExhausted as exc:
                # Write nothing. A results/ directory holding one fresh iteration
                # and two stale ones is worse than one holding neither.
                print(f"\nerror: {exc}", file=sys.stderr)
                print(
                    "Nothing was written. Rerun when the quota resets, or:\n"
                    "  uv run cli-agent-eval --model gemini-3.1-flash-lite\n"
                    "  uv run cli-agent-eval --rescore     (recomputes from a previous run)",
                    file=sys.stderr,
                )
                return 3

    for version in [v for v in versions if v in runs]:
        meta = PROMPT_VERSIONS[version]
        iteration_runs = runs[version]
        per_iteration[version] = [r.as_row() for r in iteration_runs]

        summary = summarize(
            version,
            meta.title,
            [r.score for r in iteration_runs],
            [r.result.latency_ms for r in iteration_runs],
        )
        summaries.append(summary)

        csv_path = args.out / f"iteration_{meta.order}_{version}_{args.shell}.csv"
        _write_csv(per_iteration[version], csv_path, columns)
        errors = f" | ⚠️ {summary.errors} API errors" if summary.errors else ""
        print(
            f"[{version}] mean {summary.mean_score:.2f} | pass {summary.pass_rate:.0%} | "
            f"unsafe leaks {summary.unsafe_leaks} | guard saves {summary.guard_saves} | "
            f"malformed {summary.malformed}{errors} -> {csv_path.name}",
            flush=True,
        )

    if not summaries:
        print("error: no iterations to report", file=sys.stderr)
        return 2

    _write_csv([s.as_row() for s in summaries], args.out / "summary.csv", list(summaries[0].as_row()))
    _write_xlsx(per_iteration, summaries, args.out / "prompt_engineering_results.xlsx", columns)
    _write_results_md(
        summaries, runs, args.shell, DOCS_DIR / "RESULTS.md", rescored_from=args.rescore
    )

    print(f"\nwrote {args.out / 'prompt_engineering_results.xlsx'}")
    print(f"wrote {DOCS_DIR / 'RESULTS.md'}")

    total_leaks = sum(s.unsafe_leaks for s in summaries)
    if total_leaks:
        print(f"\n{total_leaks} unsafe leak(s) across all iterations - see docs/RESULTS.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
