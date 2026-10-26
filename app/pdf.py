"""PDF 공지문 글자 읽기. 동아리 공지나 수업 안내 PDF를 보내면 글자를 꺼내 대화 입력창에 넣어 준다.

Python 라이브러리 없이 운영체제의 pdftotext 프로그램(poppler)을 직접 부른다. 노트북에는 직접 설치하고,
서버에서는 실행 환경(이미지)에 들어 있어야 한다. 글자가 든 PDF만 읽고, 스캔한 그림 PDF는 읽지 못한다.
"""
import subprocess
import tempfile

from app.logs import log

MAX_BYTES = 5 * 1024 * 1024  # PDF 한 개 최대 5MB
MAX_PAGES = 5  # 앞쪽 몇 쪽만 읽는다. 공지는 보통 한두 쪽이다


class PdfError(Exception):
    pass


def read_text(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise PdfError("PDF가 5MB보다 큽니다.")
    if not data.startswith(b"%PDF"):
        raise PdfError("PDF 파일이 아닙니다.")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        try:
            # -layout: 표처럼 늘어선 글자의 줄 맞춤을 살린다. 마지막 '-' 는 결과를 화면(stdout)으로 내보내라는 뜻
            done = subprocess.run(["pdftotext", "-layout", "-l", str(MAX_PAGES), "-enc", "UTF-8", f.name, "-"],
                                  capture_output=True, timeout=20)
        except FileNotFoundError as error:
            log("ERROR", "pdftotext 프로그램을 찾을 수 없습니다. 실행 환경에 poppler 가 설치되어 있지 않습니다.")
            raise PdfError("PDF에서 글자를 읽는 프로그램이 서버에 없습니다.") from error
    if done.returncode != 0:
        log("WARNING", "PDF 글자 읽기 실패", code=done.returncode)
        raise PdfError("PDF 파일을 읽을 수 없습니다.")
    text = done.stdout.decode("utf-8", "replace")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        raise PdfError("PDF에서 글자를 찾지 못했습니다. 스캔한 그림 PDF는 사진 버튼으로 보내 주세요.")
    log("INFO", "PDF 글자 읽기", chars=len(text))
    return text
