"""Mirror visible, completed /btw replies that have no rollout JSONL.

Only the configured TUI pane is inspected. Never read tool output, another
agent's session, or a streaming answer as a completed reply. Persistent state
contains message IDs and hashes, not prompts, answers, or verification codes.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def prompt_key(text: str) -> str:
    text = re.sub(r"^/(?:btw|side)(?:\s+|$)", "", text.strip(), flags=re.I)
    return fingerprint(re.sub(r"\s+", "", text))


def is_side_screen(screen: str) -> bool:
    # At mobile widths (53/56 columns), Codex clips the shortcut hints.
    # The side identity survives; requiring "to switch" stalls both input
    # routing and replies until a wider client attaches to the same window.
    return any(re.match(r"^\s*Side from main thread(?:\s*[·•]|\s*$)", line)
               for line in screen.rstrip().splitlines()[-6:])


@dataclass(frozen=True)
class SideReply:
    prompt: str
    answer: str
    frame_key: str


def completed_side_reply(screen: str) -> SideReply | None:
    if not is_side_screen(screen):
        return None
    lines = screen.splitlines()
    footer = next((i for i in range(len(lines) - 1, -1, -1)
                   if "Side from main thread" in lines[i]), len(lines))
    lines = lines[:footer]
    while lines and not lines[-1].strip():
        lines.pop()
    # An empty composer is essential: choices, interruptions, drafts, and
    # partially drawn screens must not be mistaken for a completed turn.
    if not lines or lines[-1].strip() != "› Ask a follow-up question":
        return None
    lines.pop()
    users = [i for i, line in enumerate(lines) if line.startswith("› ")]
    if not users:
        return None  # prompt scrolled out; don't guess an answer's owner
    start = users[-1]
    # The pending-input widget follows the transcript and can be arbitrarily
    # tall. It is not an assistant cell and must not hide the Working status.
    if any("esc to interrupt" in line.lower() and line.startswith("• ")
           for line in lines[start + 1:]):
        return None
    preview = next((i for i in range(start + 1, len(lines))
                    if re.match(r"^• (?:Queued follow-up inputs|Messages to be submitted\b)", lines[i])), None)
    if preview is not None:
        lines = lines[:preview]
    cells = [i for i in range(start + 1, len(lines)) if lines[i].startswith("• ")]
    if not cells:
        return None
    last = cells[-1]
    # Older errors and account notices can coexist with a later successful
    # answer. Only a native interruption after the final cell invalidates it;
    # indented symbols can be ordinary assistant text or tool output.
    if any(line.startswith("■") for line in lines[last + 1:]):
        return None
    prompt_lines = [lines[start][2:]]
    for line in lines[start + 1:cells[0]]:
        if not line.strip():
            break
        if not line.startswith("  "):
            return None
        prompt_lines.append(line[2:])
    head = lines[last][2:]
    if re.match(r"^(?:Ran\b|Explored$|Viewed Image$|Updated Plan$|Called\b|Working\b|Thinking\b)", head):
        return None
    answer_lines = [head]
    for line in lines[last + 1:]:
        if re.match(r"^[─━]{2,}|^─ Worked for ", line):
            break
        if line.startswith("⚠"):
            break  # a separate native notice, not part of the assistant cell
        if line.strip() and not line.startswith("  "):
            return None
        if line.lstrip().startswith(("└", "│", "├")):
            return None
        answer_lines.append(line[2:] if line.startswith("  ") else line)
    answer = "\n".join(answer_lines).strip()
    if not answer:
        return None
    # The preceding transcript disambiguates identical consecutive turns.
    # Whitespace is excluded so wrapping/resizing doesn't create a new turn.
    frame = re.sub(r"\s+", "", "\n".join(lines[:last]) + answer)
    return SideReply("\n".join(prompt_lines).strip(), answer, fingerprint(frame))


def latest_completed_side_reply(screen: str) -> SideReply | None:
    """Keep a completed reply observable when a queued turn starts immediately.

    Only an explicit completion boundary permits recovery from the preceding
    transcript. A prior commentary cell alone is never treated as a final.
    """
    current = completed_side_reply(screen)
    if current is not None or not is_side_screen(screen):
        return current
    lines = screen.splitlines()
    users = [i for i, line in enumerate(lines)
             if line.startswith("› ") and line.strip() != "› Ask a follow-up question"]
    for start, end in reversed(list(zip(users, users[1:]))):
        if not any(line.startswith("─ Worked for ") for line in lines[start:end]):
            continue
        previous = "\n".join(lines[:end]) + "\n› Ask a follow-up question\n Side from main thread\n"
        reply = completed_side_reply(previous)
        if reply is not None:
            return reply
    return None


class SideReplyMirror:
    def __init__(self, path: Path, send: Callable[[str, int], bool]) -> None:
        self.path = path
        self.send = send
        try:
            self.state = json.loads(path.read_text())
        except FileNotFoundError:
            self.state = {"pending": [], "sent": []}
        # Fail closed on corrupt state rather than replaying old answers.
        if not isinstance(self.state, dict) or not isinstance(self.state.get("sent"), list):
            raise ValueError("invalid side reply state")
        self.observed_key = ""
        self.observations = 0

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def register(self, prompt: str, message_id: int, before_screen: str = "") -> None:
        if message_id <= 0:
            return
        key = "telegram:" + str(message_id)
        if key in self.state["sent"] or any(r["id"] == message_id for r in self.state["pending"]):
            return
        before = latest_completed_side_reply(before_screen)
        self.state["pending"].append({"id": message_id, "prompt": prompt_key(prompt),
                                      "before": before.frame_key if before else ""})
        self.state["pending"] = self.state["pending"][-100:]
        self.save()

    def cancel(self, message_id: int) -> None:
        self.state["pending"] = [r for r in self.state["pending"] if r["id"] != message_id]
        self.save()

    def poll(self, screen: str, scope: str = "") -> int | None:
        reply = latest_completed_side_reply(screen)
        if reply is None:
            self.observed_key = ""
            self.observations = 0
            return None
        matches = [r for r in self.state["pending"] if r["prompt"] == prompt_key(reply.prompt)]
        # Terminal-origin turns mirror to the configured chat too, without
        # borrowing a pending Telegram message that belongs to another prompt.
        record = matches[0] if matches else None
        display_key = "display:" + fingerprint(scope + prompt_key(reply.prompt)
                                              + re.sub(r"\s+", "", reply.answer))
        if record and record["before"] == reply.frame_key:
            return None
        key = "telegram:" + str(record["id"]) if record else display_key
        observation = key + ":" + reply.frame_key
        if observation != self.observed_key:
            self.observed_key = observation
            self.observations = 1
            return None
        self.observations += 1
        if key in self.state["sent"]:
            return None
        message_id = record["id"] if record else 0
        if not self.send(reply.answer, message_id):
            return None  # not acknowledged: retain the candidate for retry
        self.state["sent"] = (self.state["sent"] + list(dict.fromkeys([key, display_key])))[-500:]
        if record:
            self.state["pending"].remove(record)
        self.save()
        return message_id
