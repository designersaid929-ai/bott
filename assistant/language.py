"""Cheap language hint (uz / ru / en) for the latest incoming message.

It is only a hint for Claude, so simple heuristics are good enough.
"""
from __future__ import annotations

import re
from typing import Optional

APOSTROPHES = "'ʻ’‘`ʼ"

_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
_LATIN_RE = re.compile(r"[a-zA-Z]")
_UZ_CYRILLIC_LETTERS_RE = re.compile(r"[ўқғҳЎҚҒҲ]")
_CYRILLIC_WORD_RE = re.compile(r"[Ѐ-ӿ]+")
_LATIN_WORD_RE = re.compile(r"[a-z]+")

_APOS_CLASS = "[" + re.escape(APOSTROPHES) + "]"
# English possessives / contractions ("dog's", "it's") and "o'clock" are not Uzbek.
_ENGLISH_APOSTROPHE_RE = re.compile(r"o" + _APOS_CLASS + r"clock|[a-z]" + _APOS_CLASS + r"s\b")
_UZ_OG_RE = re.compile(r"[og]" + _APOS_CLASS + r"[a-z]")

UZ_CYRILLIC_WORDS = {
    "салом", "ассалому", "алейкум", "алайкум", "рахмат", "раҳмат", "яхши", "нима", "нимага",
    "қалесан", "калесан", "йўқ", "йук", "бугун", "эртага", "кеча", "керак", "хоп", "бопти",
    "зўр", "зор", "ҳозир", "хозир", "кейин", "мен", "сен", "сиз", "биз", "қаерда", "каерда",
    "қаердасан", "яхшимисиз", "яхшимисан", "қандай", "кандай", "борми", "йуқ", "ака", "опа",
}

UZ_LATIN_WORDS = {
    "salom", "assalomu", "alaykum", "aleykum", "rahmat", "yaxshi", "nima", "nimaga", "qalesan",
    "qalaysan", "qalaysiz", "qayerda", "qayerdasan", "bugun", "ertaga", "kecha", "kerak", "xop",
    "bopti", "zor", "hozir", "keyin", "men", "sen", "siz", "biz", "yoq", "bor", "qachon",
    "qanday", "nega", "ham", "lekin", "endi", "aka", "opa", "uka", "mayli", "boldi", "yaxshimisiz",
}

UZ_SUFFIXES = (
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti", "yapmiz",
    "asizmi", "ganman", "dingmi",
)


def detect_language(text: str) -> Optional[str]:
    """Return "uz", "ru", "en" or None when the text has no letters."""
    if not text:
        return None
    cyrillic = len(_CYRILLIC_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if cyrillic == 0 and latin == 0:
        return None

    lowered = text.lower()
    if cyrillic > latin:
        if _UZ_CYRILLIC_LETTERS_RE.search(text):
            return "uz"
        if any(word in UZ_CYRILLIC_WORDS for word in _CYRILLIC_WORD_RE.findall(lowered)):
            return "uz"
        return "ru"

    cleaned = _ENGLISH_APOSTROPHE_RE.sub(" ", lowered)
    if _UZ_OG_RE.search(cleaned):
        return "uz"
    without_apostrophes = re.sub(_APOS_CLASS, "", cleaned)
    for word in _LATIN_WORD_RE.findall(without_apostrophes):
        if word in UZ_LATIN_WORDS or word.endswith(UZ_SUFFIXES):
            return "uz"
    return "en"
