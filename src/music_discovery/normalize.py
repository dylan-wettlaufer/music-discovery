"""Strip the differences that hide a song the user has already heard.

The exclusion key is ``normalize(primary artist) + normalize(title)``. Featuring
credits, remaster tags, and punctuation are removed so a deluxe or remastered
copy of a heard song still matches.
"""

import re
import unicodedata

_REMASTER_RE = re.compile(
    r"(?i)\s*(?:[-–—]\s*)?(?:[\(\[]\s*)?(?:\d{4}\s+)?re-?master(?:ed)?(?:\s+\d{4})?\s*[\)\]]?"
)
_FEAT_RE = re.compile(
    r"(?i)\s*(?:[\(\[]\s*)?(?:feat\.?|ft\.?|featuring)\s+[^\)\]]+(?:[\)\]])?"
)
_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)

# Unit separator. It cannot appear in normalized text, which has collapsed whitespace.
_KEY_SEPARATOR = "\x1f"


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _REMASTER_RE.sub(" ", text)
    text = _FEAT_RE.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    return text


def normalized_key(artist: str, title: str) -> str:
    return f"{normalize(artist)}{_KEY_SEPARATOR}{normalize(title)}"
