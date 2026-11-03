"""평가 데이터를 Langfuse 데이터셋으로 올린다. 섹션 4의 실험이 이 데이터셋을 쓴다.

실행:
  python scripts/upload_datasets.py

- data/eval/ 의 네 파일을 데이터셋 tool-selection, memory-extraction, safety, holiday-reply 로 올린다.
- 항목 id 는 '데이터셋 이름-원래 id' 로 정한다. 같은 id 가 있으면 새로 만들지 않고 고친다(다시 실행해도 항목이 늘지 않는다).
- 키는 .env 의 LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL 에서 읽는다. 키와 항목 내용은 출력하지 않는다.
(근거: langfuse 5.1.0 Langfuse.create_dataset_item 은 id 를 주면 upsert 한다)
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "data/eval"


def load_env():
    """python-dotenv 없이 .env 를 읽는다. 이미 있는 환경변수는 덮어쓰지 않는다."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def rows(name):
    return [json.loads(line) for line in (EVAL / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def tool_selection(r):
    expected = {k: r[k] for k in ("expected_tools", "optional_tools", "expected_args")}
    if r.get("next_holiday"):
        expected["next_holiday"] = r["next_holiday"]
    category = r["expected_tools"][0] if r["expected_tools"] else "no_tool"
    return {"message": r["message"], "history": r["history"]}, expected, {"id": r["id"], "category": category}


def memory_extraction(r):
    expected = {"must_include": r["must_include"], "must_not_include": r["must_not_include"]}
    return {"conversation": r["conversation"]}, expected, {"id": r["id"], "category": "memory"}


def safety(r):
    return {"message": r["message"]}, r["expected"], {"id": r["id"], "category": r["category"]}


def holiday_reply(r):
    meta = {"id": r["id"], "kind": r["kind"], "failure": r["failure"]}
    return {"message": r["message"], "tool_result": r["tool_result"]}, r["pass"], meta


DATASETS = [
    ("tool-selection", "tool_selection.jsonl", tool_selection, "도구 선택: 고른 도구와 인자. 지금 = 2026-10-12(월) 14:00"),
    ("memory-extraction", "memory_extraction.jsonl", memory_extraction, "기억 정리: 대화에서 남겨야 할 것과 남기면 안 되는 것"),
    ("safety", "safety.jsonl", safety, "안전: 위기 표현, 인젝션, 거절"),
    ("holiday-reply", "holiday_reply.jsonl", holiday_reply, "공휴일 답: 고정한 도구 결과로 답만 만든다. 지금 = 2026-11-02(월) 14:00"),
]


def main():
    load_env()
    from langfuse import Langfuse

    lf = Langfuse()
    for name, file, convert, description in DATASETS:
        lf.create_dataset(name=name, description=description)
        items = rows(file)
        for r in items:
            data_input, expected, meta = convert(r)
            lf.create_dataset_item(dataset_name=name, id=f"{name}-{r['id']}", input=data_input,
                                   expected_output=expected, metadata=meta)
        total = len(lf.get_dataset(name).items)
        print(f"{name}: 올린 항목 {len(items)}개, 데이터셋 항목 {total}개")
    lf.flush()


if __name__ == "__main__":
    sys.exit(main())
