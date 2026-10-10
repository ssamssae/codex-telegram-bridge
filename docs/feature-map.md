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
6. Confirm the tab icon and the company banner display the current −β company mark. Click the banner to open https://kangdaejong.com/ in the same tab. Repeat with the other language.

Company icons use the canonical https://kangdaejong.com/brand/current/ URLs. They are not copied into this guide. The content policy permits images from that host while scripts and styles remain local. The stylesheet URL contains its content hash so an updated banner does not reuse stale CSS.

The guide does not log in, run AI actions or change Telegram settings. Website language and `/language` commands are separate.

Initial local verification: 2026-10-06, shared generator and Aside desktop/390×844 previews.
Public hosting is a separate verification step: after publishing, repeat the language checks at the actual HTTPS URL and confirm the JS/CSS assets load.

## 한국어 확인 경로

로컬 설명서를 열어 상단 언어 메뉴 → 본문·목차 즉시 변경 → 새로고침 뒤 선택 유지 → `?lang=ko` 직접 진입을 확인합니다.
390px 화면에서 메뉴·본문이 잘리지 않는지 확인하고, 공개 게시 후에도 실제 HTTPS 주소에서 같은 순서로 확인합니다.
로컬 확인만으로 공개 게시나 설치된 브릿지 동작을 완료로 판단하지 않습니다.
탭 아이콘과 상단 회사마크 배너가 현재 −β 로고인지 확인합니다. 배너를 클릭하면 같은 탭에서 https://kangdaejong.com/ 홈페이지로 이동해야 합니다.

## Release 0.9.16 runtime controls

Prerequisites: an allowlisted private bot chat, a live Codex session in POSIX
tmux, and flow cards enabled with `CRB_FLOW_MIRROR=1`. Use a disposable session
for controls that interrupt work. Native Windows and `exec` mode do not provide
these buttons.

| Entry point | Action | Expected result |
| --- | --- | --- |
| Bot chat | Send `/language ko`, `/language en`, then `/language` | Bridge instructions switch language and report the saved choice; user/model content is unchanged. |
| Current progress card | Press Stop during a running turn | One interrupt request for that live session/turn; stale cards, drafts, and pending approvals are rejected. |
| Native pending-input section | Wait at least ten seconds with the native Esc interrupt-and-send hint visible, then press Apply now | One native action for the matching pending messages; their receipt removes only confirmed queue entries. Ordinary Tab queues remain ineligible. |
| Running native turn | Wait through quiet tool activity, then let the turn complete | Typing continues during the active turn and stops after completion or interruption. |

Release preparation on 2026-10-09: the sanitized export passed 285 tests on
macOS/Python 3.14. Source control and typing regressions passed another 50 tests.
These checks use fake Telegram/terminal clients. This release task did not
restart an installed bridge or perform a live Telegram round trip.

## Release 0.9.17 verification

Public export and regression suite: 299 tests. Run `python3 -m unittest discover -s tests` in a clean checkout. Tests use isolated state and captured senders; they do not send Telegram messages or reset a live session. Source assets are verified against the release tag, and release downloads are checked against `SHA256SUMS`.
GitHub Pages rebuilds the existing `main:/docs` guide after merge. This static publication does not run a bridge service.
