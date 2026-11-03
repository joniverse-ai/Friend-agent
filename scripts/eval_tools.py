"""도구 선택 평가. data/eval/tool_selection.jsonl 의 메시지마다 고른 도구와 핵심 인자가 정답과 맞는지 센다.

실행:
  python scripts/eval_tools.py            # .env 의 LLM 으로 모델 선택기를 평가 (호출 40번, 비용 발생)
  python scripts/eval_tools.py --rule     # 키워드 규칙 선택기를 평가 (비용 없음)
  python scripts/eval_tools.py --label staging                       # Langfuse 프롬프트 router 의 staging 라벨로 평가
  python scripts/eval_tools.py --label staging --experiment v2-1     # 같은 평가를 데이터셋 tool-selection 의 실험 v2-1 로 남김

  python scripts/eval_tools.py --min-tools 35 --min-args 32          # 평가 관문: 기준에 못 미치면 종료 코드 1 (CI 의 eval.yml)

--label 을 주지 않으면 서비스와 같은 규칙(ENV=production 이면 production, 그 밖은 staging)으로 고른다.
Langfuse 키가 없거나 가져오지 못하면 코드에 남긴 지시문(code)으로 평가한다. 결과 첫 줄에 쓴 지시문이 나온다.

정답의 날짜와 시각은 '2026-10-12(월) 14:00 한국 시간'을 지금으로 보고 만들었다. 평가도 그 시각을 지금으로 쓴다.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import tracing  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.llm import make_llm  # noqa: E402
from app.router import model_choose, rule_choose  # noqa: E402

NOW = datetime.fromisoformat("2026-10-12T14:00:00+09:00")
LOCATION = "서울"


def args_match(expected, actual):
    """정답에 적힌 인자만 비교한다. due_at 은 분 단위까지 같은 시각이면 맞다."""
    wrong = []
    for key, want in expected.items():
        got = actual.get(key)
        if key == "due_at":
            try:
                same = datetime.fromisoformat(str(got)).replace(tzinfo=None, second=0) == datetime.fromisoformat(want)
            except ValueError:
                same = False
        elif key in ("from", "to"):
            same = str(got or "")[:7] == want[:7]   # 월까지만 비교
        elif key == "day":   # 정답은 '오늘로부터 며칠 뒤', 결과는 date(YYYY-MM-DD)
            try:
                same = (datetime.fromisoformat(str(actual.get("date"))).date() - NOW.date()).days == want
            except ValueError:
                same = False
            got = actual.get("date")
        else:
            same = want in str(got or "")
        if not same:
            wrong.append(f"{key}: 정답 {want}, 결과 {got}")
    return wrong


def covers_next_holiday(next_holiday, calls):
    """'다음 공휴일' 질문은 기간을 정답으로 적지 않는다. 인자를 비우면 도구가 오늘부터 1년을 찾으므로 그것도 맞다.
    get_holidays 호출 가운데 하나라도 그 기간에 다음 공휴일 날짜가 들어가면 맞다."""
    target = datetime.fromisoformat(next_holiday).date()
    for args in calls:
        try:
            start = datetime.fromisoformat(args.get("from") or NOW.date().isoformat()).date()
            end = datetime.fromisoformat(args["to"]).date() if args.get("to") else start.replace(year=start.year + 1)
        except ValueError:
            continue
        if start <= target <= end:
            return []
    return [f"기간: 다음 공휴일 {next_holiday} 이 들어가야 함, 결과 {calls}"]


def judge(expected, tools):
    """고른 도구가 맞는지와, 맞으면 틀린 인자 목록을 돌려준다. expected 는 jsonl 한 줄(또는 데이터셋의 expected_output)."""
    picked = {t["name"] for t in tools}
    required, optional = set(expected["expected_tools"]), set(expected["optional_tools"])
    if not required <= picked <= required | optional:
        return False, []
    wrong = []
    got = {t["name"]: t["args"] for t in tools}
    for name, want in expected["expected_args"].items():
        wrong += args_match(want, got.get(name, {}))
    if expected.get("next_holiday"):
        wrong += covers_next_holiday(expected["next_holiday"], [t["args"] for t in tools if t["name"] == "get_holidays"])
    return True, wrong


def option(name):
    """--label staging 처럼 이름 뒤에 오는 값. 없으면 None."""
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv[:-1] else None


def report(title, results):
    """results: (id, message, expected, tools, confidence) 목록. 맞힌 수와 틀린 항목을 출력한다."""
    tool_ok = args_ok = 0
    lines = []
    for item_id, message, expected, tools, conf in results:
        tools_right, wrong = judge(expected, tools)
        tool_ok += tools_right
        args_ok += tools_right and not wrong
        if not tools_right or wrong:
            picked = sorted(t["name"] for t in tools)
            detail = "; ".join(wrong) if wrong else f"정답 {expected['expected_tools'] or '도구 없음'}, 결과 {picked or '도구 없음'}"
            lines.append(f"  {item_id} {message}  →  {detail}  (확신도 {conf:.2f})")
    n = len(results)
    print(f"[{title}] 도구 정답 {tool_ok}/{n}, 도구와 인자 모두 정답 {args_ok}/{n}")
    print("\n".join(lines))
    return tool_ok, args_ok


def gate(tool_ok, args_ok):
    """--min-tools, --min-args 를 주면 평가 관문으로 쓴다. 기준에 못 미치면 종료 코드 1(CI 실패)."""
    failed = [f"{label} {got}개 < 기준 {want}개" for label, got, want in
              (("도구 정답", tool_ok, option("--min-tools")), ("도구와 인자 정답", args_ok, option("--min-args")))
              if want is not None and got < int(want)]
    if failed:
        print("평가 관문 실패: " + ", ".join(failed))
        sys.exit(1)
    if option("--min-tools") or option("--min-args"):
        print("평가 관문 통과")


def run_experiment(run_name, label, llm):
    """데이터셋 tool-selection 으로 Langfuse 실험을 돌린다. 화면의 Datasets > tool-selection > Runs 에서 실행끼리 비교할 수 있다.
    근거: langfuse 5.1.0 DatasetClient.run_experiment(name, task, evaluators), Evaluation(name, value, comment, data_type)"""
    from langfuse import get_client
    from langfuse.experiment import Evaluation

    def task(*, item, **kwargs):
        with tracing.observation("choose-tools", "generation", input=item.input["message"]):
            choice = model_choose(item.input["message"], item.input["history"], NOW, LOCATION, llm, label=label)
        return {"tools": choice.tools, "confidence": choice.confidence, "reason": choice.reason, "prompt": choice.prompt}

    def tools_evaluator(*, output, expected_output, **kwargs):
        tools_right, wrong = judge(expected_output, output["tools"])
        return [Evaluation(name="tools", value=tools_right, data_type="BOOLEAN"),
                Evaluation(name="tools_and_args", value=tools_right and not wrong, data_type="BOOLEAN",
                           comment="; ".join(wrong) or None)]

    dataset = get_client().get_dataset("tool-selection")
    result = dataset.run_experiment(name=run_name, run_name=run_name, task=task, evaluators=[tools_evaluator],
                                    max_concurrency=5, metadata={"label": label or "", "model": llm.model})
    get_client().flush()
    prompts = sorted({r.output["prompt"] for r in result.item_results if r.output})
    counts = report(f"모델 {llm.model}, {', '.join(prompts)}, 실험 {run_name}",
                    [(r.item.metadata["id"], r.item.input["message"], r.item.expected_output, r.output["tools"],
                      r.output["confidence"]) for r in sorted(result.item_results, key=lambda r: r.item.metadata["id"]) if r.output])
    print(f"  Langfuse: {result.experiment_url}")
    return counts


def main():
    use_rule = "--rule" in sys.argv
    label, experiment = option("--label"), option("--experiment")
    llm = None if use_rule else make_llm(load_settings())
    if llm is not None and not llm.can_reason:
        print("LLM_PROVIDER=fake 에서는 모델 평가를 할 수 없습니다. --rule 로 실행하세요.")
        return
    if experiment:
        tracing.setup()
        gate(*run_experiment(experiment, label, llm))
        return

    rows = [json.loads(line) for line in (ROOT / "data/eval/tool_selection.jsonl").read_text(encoding="utf-8").splitlines()]
    results, prompts = [], set()
    for row in rows:
        if use_rule:
            choice = rule_choose(row["message"], NOW, LOCATION)
        else:
            choice = model_choose(row["message"], row["history"], NOW, LOCATION, llm, label=label)
        prompts.add(choice.prompt)
        results.append((row["id"], row["message"], row, choice.tools, choice.confidence))
    gate(*report("키워드 규칙" if use_rule else f"모델 {llm.model}, {', '.join(sorted(prompts))}", results))


if __name__ == "__main__":
    main()
