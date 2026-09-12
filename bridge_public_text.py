"""Remove renderer metadata before forwarding assistant text to chat transports."""
import re

_MEMORY_BLOCK = re.compile(r"<oai-mem-citation>.*?</oai-mem-citation>", re.DOTALL)
_MEMORY_TRAILER = re.compile(r"(?m)^[ \t]*<oai-mem-citation>[ \t]*\n[\s\S]*\Z")


def strip_memory_citation(text: str) -> str:
    # Strip before splitting/chunking; partial chunks cannot identify the block.
    # A lone inline mention of the tag is ordinary explanatory text.
    cleaned = _MEMORY_BLOCK.sub("", text or "")
    return _MEMORY_TRAILER.sub("", cleaned).strip()
