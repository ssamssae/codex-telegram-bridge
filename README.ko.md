# Codex Telegram Bridge — 한국어 사용 안내

[English README](https://github.com/ssamssae/codex-telegram-bridge#readme) · [언어 전환 웹 설명서](https://ssamssae.github.io/codex-telegram-bridge/?lang=ko)

컴퓨터에서 브릿지를 실행하고, 본인 전용 텔레그램 봇에 요청을 보내 휴대전화로 결과를 받습니다.

## 시작하기

먼저 Codex CLI를 설치하고 로그인하세요. 운영체제와 터미널 준비 사항은 설치한 버전의 전체 README를 확인하세요.

macOS·Linux·WSL에서는 첫 번째 터미널에서 아래와 같이 Codex를 켜 둡니다. 설치·설정 명령은 두 번째 터미널에서 실행하세요. 기본 Windows 터미널은 별도 모드를 사용하므로 README를 따르세요.

```sh
tmux -L codex new -s codex
codex
```

```sh
pipx install codex-telegram-bridge
codex-telegram-bridge setup
codex-telegram-bridge doctor
```

컴퓨터의 로컬 터미널에서 실행하세요. 설정 도구가 봇을 연결하고 브릿지 서비스를 준비합니다.

공식 [@BotFather](https://t.me/BotFather)에 `/newbot`을 보내 본인 전용 봇을 만드세요.
토큰은 로컬 설정 도구나 비공개 설정 파일에만 입력하고, 설정 안내에 따라 새 봇에 `/start`를 보냅니다.

현재 프로젝트를 설명해 달라는 작은 요청부터 보내세요. 연결된 터미널과 텔레그램 답변을 확인합니다. CLI의 기존 권한과 승인 규칙이 그대로 적용됩니다.

## 언어 설정

웹 설명서 상단에서 한국어 또는 English를 선택하면 페이지 전체가 즉시 바뀌고 브라우저에 선택이 저장됩니다.
언어 설정을 지원하는 브릿지 버전에서는 봇에 `/language ko`, `/language en`, `/language`를 보냅니다.
명령이 없는 버전은 설치 버전의 README와 릴리스 노트에서 지원 여부를 확인하세요.

웹 설명서와 브릿지 언어는 각각 선택합니다. 요청, AI 답변, 코드, 질문 선택지를 번역하거나 모델·계정을 바꾸지는 않습니다. 답변 언어는 AI에게 직접 요청하세요.

## 문제 해결

CLI 로그인, 브릿지 실행 상태, 본인 봇 대화의 채팅 ID 설정을 확인하세요. 연결 설정을 바꾸기 전에 README의 진단 절차를 따르세요.

비공개 브라우징이나 브라우저 저장소 차단 상태에서는 웹 언어가 저장되지 않을 수 있습니다. 현재 페이지의 언어 선택은 사용할 수 있고, 공유 링크에는 ?lang=ko 또는 ?lang=en을 붙이면 됩니다.

[전체 설치·진단 안내](https://github.com/ssamssae/codex-telegram-bridge#readme) · [릴리스 노트](https://github.com/ssamssae/codex-telegram-bridge/releases)
