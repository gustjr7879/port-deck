# CLAUDE.md

## 커뮤니케이션
- 사용자에게 하는 모든 보고와 답변은 한국어로 작성한다 (진행 상황, 결과 요약, 질문 포함).
- 코드 식별자, 명령어, 파일 경로, 에러 원문은 그대로 둔다.

## 프로젝트: Port Deck
- 내 프로젝트가 점유한 포트 / LLM / 로그를 보여주는 로컬 대시보드
- `server.py` (Python stdlib http.server + psutil), `static/index.html` (글래스모피즘 UI), 설정은 `config.json`
- 실행: `start.bat` 또는 `python server.py` → http://127.0.0.1:7070
