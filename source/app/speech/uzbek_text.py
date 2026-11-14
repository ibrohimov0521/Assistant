from __future__ import annotations

import re
from datetime import date
from typing import Mapping

_PUNCTUATION = str.maketrans({"‘": "'", "’": "'", "ʻ": "'", "ʼ": "'", "`": "'", "−": "-", "‑": "-"})
_ONES = ("nol", "bir", "ikki", "uch", "to'rt", "besh", "olti", "yetti", "sakkiz", "to'qqiz")
_TENS = ("", "o'n", "yigirma", "o'ttiz", "qirq", "ellik", "oltmish", "yetmish", "sakson", "to'qson")
_MONTHS = ("yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr")
_ABBREVIATION_READINGS = (
    ("OpenAI", "Open ey ay"),
    ("ChatGPT", "chat ji pi ti"),
    ("GPT", "ji pi ti"),
    ("SQL", "es kyu el"),
    ("API", "ey pi ay"),
    ("CPU", "si pi yu"),
    ("GPU", "ji pi yu"),
    ("URL", "yu ar el"),
    ("AI", "ey ay"),
)


def _integer_words(value: int) -> str:
    for size, name in ((10**9, "milliard"), (10**6, "million"), (1000, "ming"), (100, "yuz")):
        if value >= size:
            leading, rest = divmod(value, size)
            words = f"{_integer_words(leading)} {name}"
            return f"{words} {_integer_words(rest)}" if rest else words
    if value < 10:
        return _ONES[value]
    tens, ones = divmod(value, 10)
    return f"{_TENS[tens]} {_ONES[ones]}" if ones else _TENS[tens]


def _digit_words(digits: str) -> str:
    if len(digits) > 12 or (len(digits) > 1 and digits.startswith("0")):
        return " ".join(_ONES[int(digit)] for digit in digits)
    return _integer_words(int(digits))


def _ordinal(value: int) -> str:
    words = _integer_words(value)
    return words + ("nchi" if words[-1] in "aeiou" else "inchi")


def _spoken_date(match: re.Match[str]) -> str:
    try:
        day = date.fromisoformat(match[0])
    except ValueError:
        return match[0]
    return f"{_ordinal(day.day)} {_MONTHS[day.month - 1]}, {_ordinal(day.year)} yil"


def _spoken_time(match: re.Match[str]) -> str:
    hours = _integer_words(int(match[1]))
    minutes = int(match[2])
    suffix = match[3] or ""
    if not minutes:
        return hours + suffix
    connector = "yu" if hours[-1] in "aeiou" else "u"
    return f"{hours}{connector} {_integer_words(minutes)}{suffix}"


def _spoken_number(match: re.Match[str]) -> str:
    sign, digits, fraction = match.groups()
    digits = re.sub(r"[ \u00a0,]", "", digits)
    words = {"-": "minus ", "+": "plyus ", "": ""}[sign] + _digit_words(digits)
    return f"{words} vergul {_digit_words(fraction)}" if fraction else words


_CURRENCY_WORDS = {
    "UZS": "so'm", "USD": "AQSh dollari", "EUR": "yevro", "RUB": "rubl",
    "GBP": "funt sterling", "JPY": "iyena", "KZT": "tenge", "TRY": "lira",
}


def _spoken_currency(match: re.Match[str]) -> str:
    sign, digits, fraction, currency = match.groups()
    digits = re.sub(r"[ \u00a0,]", "", digits)
    amount = {"-": "minus ", "+": "plyus ", "": ""}[sign] + _digit_words(digits)
    if fraction:
        amount += " vergul " + _digit_words(fraction)
    return f"{amount} {_CURRENCY_WORDS[currency.upper()]}"


def _apply_pronunciations(text: str, pronunciations: Mapping[str, str] | None) -> str:
    if not pronunciations:
        return text
    # Longer names and phrases must win before their contained words.
    suffix = r"(?:ning|dagi|dan|tan|gacha|ga|ka|qa|da|ta|ni|lar|lik|siz|li|cha)"
    for written, spoken in sorted(pronunciations.items(), key=lambda item: len(item[0]), reverse=True):
        written = str(written).strip()
        spoken = str(spoken).strip()
        if written and spoken:
            pattern = rf"(?<!\w){re.escape(written)}(?:(?!\w)|(?={suffix}\b))"
            text = re.sub(pattern, lambda _: spoken,
                          text, flags=re.IGNORECASE)
    return text


def _expand_abbreviations(text: str) -> str:
    for written, spoken in _ABBREVIATION_READINGS:
        text = re.sub(rf"(?<!\w){re.escape(written)}(?!\w)",
                      lambda _: spoken, text, flags=re.IGNORECASE)
    return text


def prepare_uzbek_speech(text: str, pronunciations: Mapping[str, str] | None = None) -> str:
    """Clean assistant text and expand numbers without changing Uzbek Latin script."""
    text = text.translate(_PUNCTUATION)
    text = _apply_pronunciations(text, pronunciations)
    text = _expand_abbreviations(text)
    text = re.sub(r"\[([^\]\n]+)\]\(https?://[^)\n]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]*·[ \t]*@[\w.]+|(?<!\w)@[\w.]+", "", text)
    text = re.sub(r"(?m)^[ \t]*(?:[•–-]|[0-9]+[.)])[ \t]+", "", text)
    text = re.sub(r"[*_#]", "", text)
    text = re.sub(r"[ \t]*·[ \t]*", ", ", text)
    text = re.sub(r"\bo'qilmagan\s+([0-9]+)\s+ta\b",
                  lambda match: f"{_digit_words(match[1])}ta o'qilmagan xabar", text, flags=re.IGNORECASE)
    text = re.sub(r"\b([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}:[0-9]{2}):[0-9]{2}(?:\+00:00|Z)\b",
                  r"\1, soat \2, jahon vaqti", text)
    text = re.sub(r"\b[0-9]{4}-[0-9]{2}-[0-9]{2}\b", _spoken_date, text)
    text = re.sub(r"\b([0-9]{1,2})-(" + "|".join(_MONTHS) + r")\b",
                  lambda match: f"{_ordinal(int(match[1]))} {match[2]}", text, flags=re.IGNORECASE)
    text = re.sub(r"\b([0-9]{4})-yil\b", lambda match: f"{_ordinal(int(match[1]))} yil", text)
    text = re.sub(r"(?<![\w:])([01]?[0-9]|2[0-3]):([0-5][0-9])(?:[ \t]*-?[ \t]*(da|dan|ga|gacha))?(?![\w:])",
                  _spoken_time, text)
    text = re.sub(r"(?<=[0-9])[ \t]*(?:℃|°[ \t]*[CcСс]?)(?!\w)", " daraja", text)
    text = re.sub(r"(?<=[0-9])[ \t]*%", " foiz", text)
    text = re.sub(r"\bID(?=\s|:):?", "raqami", text)
    currency_pattern = r"(?<!\w)([+-]?)([0-9]{1,3}(?:[ \u00a0,][0-9]{3})+|[0-9]+)(?:[.,]([0-9]+))?[ \t]*(UZS|USD|EUR|RUB|GBP|JPY|KZT|TRY)\b"
    text = re.sub(currency_pattern, _spoken_currency, text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\w)([+-]?)([0-9]{1,3}(?:[ \u00a0,][0-9]{3})+|[0-9]+)(?:[.,]([0-9]+))?\b", _spoken_number, text)

    sentences = []
    for line in text.splitlines():
        line = " ".join(line.split()).strip(" ,:;–—-")
        if re.search(r"[a-zA-Zа-яА-ЯёўғқҳЁЎҒҚҲ]", line):
            sentences.append(line if line.endswith((".", "!", "?")) else line + ".")
    spoken = " ".join(sentences)
    if len(spoken) > 1600:
        spoken = spoken[:1600].rsplit(" ", 1)[0].rstrip(" ,:;–—-.") + "."
    return spoken
