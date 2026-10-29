"""Remove renderer metadata before forwarding assistant text to chat transports."""
import re

_MEMORY_BLOCK = re.compile(r"<oai-mem-citation>.*?</oai-mem-citation>", re.DOTALL)
_MEMORY_TRAILER = re.compile(r"(?m)^[ \t]*<oai-mem-citation>[ \t]*\n[\s\S]*\Z")


def strip_memory_citation(text: str) -> str:
    # Strip before splitting/chunking; partial chunks cannot identify the block.
    # A lone inline mention of the tag is ordinary explanatory text.
    cleaned = _MEMORY_BLOCK.sub("", text or "")
    return _MEMORY_TRAILER.sub("", cleaned).strip()


# T-261020-002: the Claude bridge's DM decoration set (strip_private_display_emoji),
# shared so Cursor DM answers read like Claude answers.
_DISPLAY_EMOJI_COMPONENT = (
    r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\U00002600-\U000027BF"
    r"\U0000231A\U0000231B\U000023E9-\U000023F3\U000023F8-\U000023FA\U000025B6\U000025C0]"
    r"[\U0000FE0E\U0000FE0F\U0001F3FB-\U0001F3FF]*"
)
_DISPLAY_EMOJI_OR_CODE = re.compile(
    r"(`+).*?\1|" + _DISPLAY_EMOJI_COMPONENT
    + r"(?:\U0000200D" + _DISPLAY_EMOJI_COMPONENT + r")*[ \t]*"
)


def strip_display_emoji(text: str) -> str:
    """Remove DM decoration, preserving code spans and fenced blocks verbatim."""
    lines = []
    fence = ""
    for line in (text or "").splitlines(keepends=True):
        if fence:
            lines.append(line)
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}\s*", line):
                fence = ""
            continue
        opening = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = opening.group(1)
            lines.append(line)
            continue
        lines.append(_DISPLAY_EMOJI_OR_CODE.sub(lambda match: match.group(0) if match.group(1) else "", line))
    cleaned = "".join(lines)
    # An intentional symbol-only answer must not become an empty Telegram message.
    return cleaned if cleaned.strip() else text
