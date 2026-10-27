# CLAUDE.md

## 커뮤니케이션
- 사용자에게 하는 모든 보고와 답변은 한국어로 작성한다 (진행 상황, 결과 요약, 질문 포함).
- 코드 식별자, 명령어, 파일 경로, 에러 원문은 그대로 둔다.

## 프로젝트: Port Deck
- 내 프로젝트가 점유한 포트 / LLM / 로그를 보여주는 로컬 대시보드
- `server.py` (Python stdlib http.server + psutil), `static/index.html` (글래스모피즘 UI), 설정은 `config.json`
- `metrics.py` (시계열 저장 · LLM 지표 · 측정 프록시), `gpu.py` (Windows PDH / nvidia-smi), `demo.py` (`--demo` 샘플 데이터)
- 공개 저장소용 스크린샷은 실제 프로젝트 정보가 드러나지 않도록 데모 모드로 찍는다
- 실행: `start.bat` 또는 `python server.py` → http://127.0.0.1:7070

## 서버 켜고 끄기 (Claude가 띄울 때)
- Claude Code 백그라운드 작업으로 띄우면 메모리가 부족할 때 Claude Code가 강제로 멈출 수 있다. 세션과 분리해서 띄운다 (PowerShell, 저장소 폴더에서):
  ```powershell
  Start-Process python -ArgumentList 'server.py' -WorkingDirectory $PWD -WindowStyle Hidden -RedirectStandardOutput server.out.log -RedirectStandardError server.err.log
  ```
- 첫 스캔 때문에 응답까지 5~10초 걸린다. 확인: `Get-NetTCPConnection -LocalPort 7070 -State Listen` 후 http://127.0.0.1:7070 접속
- 출력 로그: `server.out.log`, `server.err.log` (`*.log`는 gitignore 대상)
- 끄기: `Stop-Process -Id (Get-NetTCPConnection -LocalPort 7070 -State Listen).OwningProcess`
- 띄우기 전에 7070 포트가 이미 사용 중인지 확인한다 (중복 실행 방지)
