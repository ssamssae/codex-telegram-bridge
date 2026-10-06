# Web guide verification

The guide is static. To check a local copy without running the bridge:

```sh
python3 -m http.server 0 --bind 127.0.0.1 --directory docs
```

Open the printed localhost address. The page header contains the English / 한국어 menu.

1. Select each language. The title, headings, instructions, navigation and footer change immediately.
2. Reload and confirm the selected language stays active. Open `?lang=ko` or `?lang=en` to override a stored choice.
3. Follow the section navigation and check that related guide links carry the selected language.
4. At a 390px viewport, confirm the language menu is visible and the text fits. Code examples may scroll horizontally.
5. With browser storage blocked, confirm immediate switching still works. Persistence is not expected in this mode.

The guide does not log in, run AI actions or change Telegram settings. Website language and `/language` commands are separate.

Initial local verification: 2026-10-06, shared generator and Aside desktop/390×844 previews.
Public hosting is a separate verification step: after publishing, repeat the language checks at the actual HTTPS URL and confirm the JS/CSS assets load.

## 한국어 확인 경로

로컬 설명서를 열어 상단 언어 메뉴 → 본문·목차 즉시 변경 → 새로고침 뒤 선택 유지 → `?lang=ko` 직접 진입을 확인합니다.
390px 화면에서 메뉴·본문이 잘리지 않는지 확인하고, 공개 게시 후에도 실제 HTTPS 주소에서 같은 순서로 확인합니다.
로컬 확인만으로 공개 게시나 설치된 브릿지 동작을 완료로 판단하지 않습니다.
