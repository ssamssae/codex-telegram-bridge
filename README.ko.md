# Codex Telegram Bridge

[English](README.md) · [한국어](README.ko.md) · [Web guide · 웹 설명서](https://ssamssae.github.io/codex-telegram-bridge/) · [Web verification](docs/feature-map.md)

이번 GitHub 릴리스 `0.9.16`은 아래 명령으로 설치합니다. PyPI 패키지 버전은
이번 발행에서 변경하지 않습니다.

```bash
pipx install "git+https://github.com/ssamssae/codex-telegram-bridge.git@v0.9.16"
```

컴퓨터에서 실행 중인 Codex CLI를 텔레그램으로 조작하는 브릿지입니다.
휴대폰에서 질문·이미지·영상·음성·파일을 보내고, 같은 Codex 대화의 답변을
텔레그램으로 받습니다. Codex 전용이며 별도 AI 대화를 만들지 않습니다.

## 빠른 시작

Python 3.10 이상과 로그인된 Codex CLI가 필요합니다. macOS·Linux·WSL에서
실행 중인 터미널과 연결하려면 `tmux`도 설치되어 있어야 합니다.

1. 브릿지를 설치합니다.

   ```bash
   pipx install codex-telegram-bridge
   ```

2. 텔레그램의 공식 [@BotFather](https://t.me/BotFather)에서 `/newbot`을
   보내 개인 봇을 만듭니다. 받은 토큰은 비밀번호처럼 보관하고, 다음 단계의
   **로컬 터미널 설정 화면에만** 입력하세요. 봇 채팅이나 저장소에 올리지 마세요.

3. 첫 번째 터미널에서 Codex를 실행합니다.

   ```bash
   tmux -L codex new -s codex
   codex
   ```

4. 두 번째 터미널에서 설정을 시작합니다.

   ```bash
   codex-telegram-bridge setup --language ko
   ```

   설정 안내에 따라 토큰을 입력하고, 새 봇과의 채팅에서 `/start`를 보내세요.
   설정 도구가 허용할 채팅을 확인하고 백그라운드 서비스를 설치합니다.
   설정 도구의 터미널 안내는 영어이며 `--language ko`는 브릿지의 텔레그램
   안내 언어를 지정합니다.

5. `codex-telegram-bridge doctor`로 설정을 점검한 뒤 봇에 `/ping`을 보냅니다.
   이어서 짧은 질문을 보내 컴퓨터의 같은 Codex 창으로 전달되는지, 최종 답변이
   텔레그램으로 돌아오는지 확인합니다.

### Windows

네이티브 Windows에서는 `tmux` 없이 텍스트 전용 `exec` 모드를 쓸 수 있습니다.

```powershell
codex-telegram-bridge setup --mode exec --language ko
```

실행 중인 터미널 대화와 미디어를 함께 쓰려면 WSL과 `tmux`를 사용하세요.
설치 명령과 확인 방법은 [Windows 상세 안내](README.md#windows-quickstart-5-min)에 있습니다.

## 언어 변경

연결한 텔레그램 봇에서 다음 명령을 보냅니다.

| 명령 | 동작 |
| --- | --- |
| `/language` | 현재 언어와 변경 방법 표시 |
| `/language ko` | 한국어 안내 사용 |
| `/language en` | 영어 안내 사용 |

언어 선택은 브릿지가 재시작되어도 유지됩니다. 새 설치의 기본값은 영어입니다.
수동 설정에서는 `TAB_LANGUAGE=ko`를 사용할 수 있고, `CRB_LANGUAGE`가 함께
있으면 그 값이 우선합니다. 채팅에서 저장한 선택이 환경설정의 기본값보다 우선합니다.
지원하지 않는 언어를 명령으로 입력하면 기존 선택을 유지하고 사용법을 안내합니다.

번역하는 항목은 브릿지가 작성하는 연결 안내, 승인·선택 카드 안내, 질문 복구
버튼과 제출 상태 등입니다. **사용자의 질문, Codex 답변, 코드, 실제 질문·선택지,
외부 도구의 오류 내용은 원문 그대로 유지합니다.** 언어 선택이 Codex의 모델이나
답변 언어를 바꾸지는 않습니다. 별도 번역 서비스·API 키·번역 요금은 없습니다.

## 자주 쓰는 기능

| 기능 | 사용 방법 |
| --- | --- |
| 연결 확인 | `/ping` |
| Codex 상태 확인 | `repl` 모드에서 `/status` |
| 선택 질문 복구 | `repl` 모드에서 `/choices` |
| 승인·선택 답변 | 현재 질문 카드의 버튼 선택 |
| 직접 답변 | 카드가 직접 입력을 요청한 경우 해당 메시지에 답장 |
| 이미지·음성·파일 | `repl` 모드에서 봇으로 전송 |

POSIX `tmux` 연결에서 `CRB_FLOW_MIRROR=1`을 설정하면 진행 카드에 **중지**
버튼이 표시될 수 있습니다. 버튼은 현재 세션과 작업을 다시 확인한 뒤 중단을
요청합니다. 오래된 카드, 입력 중인 초안, 승인 대기 화면에서는 실행하지 않습니다.

Codex가 대기 입력과 `press esc to interrupt and send immediately` 안내를
10초 이상 표시하면 **지금 반영** 버튼을 제공합니다. 이 버튼은 Codex의 기본
동작으로 해당 대기 입력을 반영합니다. 일반 Tab 대기열에는 적용하지 않습니다.
이 버튼들은 기본 Windows와 일회성 `exec` 모드에서 제공하지 않습니다.
WSL과 `tmux` 연결에서는 사용할 수 있습니다.

질문 카드에서 지원하지 않는 자유 답변이나 만료된 질문에 대한 답장을 보내도
일반 Codex 입력으로 자동 전환하지 않습니다. 제출 여부가 불명확할 때도
답변을 자동 재전송하지 않습니다. 화면 안내에 따라 최신 질문을 확인하세요.

## 문제 해결과 상세 문서

- 연결이 안 되면 `codex-telegram-bridge doctor`를 실행하고, `repl` 모드에서는
  설정한 `tmux` 세션에 Codex가 실행 중인지 확인하세요.
- 언어가 바뀌지 않으면 `/language`로 저장된 선택을 확인한 뒤 다시 선택하세요.
  저장에 실패하면 기존 언어를 유지하고 실패 안내를 보냅니다.
- [언어 설정·검증 경로](docs/i18n.md)
- [전체 설정 항목](README.md#configuration)
- [미디어 지원](README.md#repl-mode-media-support)
- [설치·서비스 관리](README.md#setup-commands)
- [영문 전체 문서](README.md)

추가 언어는 저장소의 `bridge_i18n.py` 번역표와 검사를 확장해 제공할 수 있습니다.
이 문서는 한국어 시작 안내이며 고급 운영·진행 보고와 전체 기술 문서는 영어 문서를 참고하세요.
