from __future__ import annotations


_CYRILLIC_TO_LATIN = str.maketrans({
    "А": "A", "а": "a", "Б": "B", "б": "b", "В": "V", "в": "v",
    "Г": "G", "г": "g", "Д": "D", "д": "d", "Е": "E", "е": "e",
    "Ё": "Yo", "ё": "yo", "Ж": "J", "ж": "j", "З": "Z", "з": "z",
    "И": "I", "и": "i", "Й": "Y", "й": "y", "К": "K", "к": "k",
    "Л": "L", "л": "l", "М": "M", "м": "m", "Н": "N", "н": "n",
    "О": "O", "о": "o", "П": "P", "п": "p", "Р": "R", "р": "r",
    "С": "S", "с": "s", "Т": "T", "т": "t", "У": "U", "у": "u",
    "Ф": "F", "ф": "f", "Х": "X", "х": "x", "Ц": "Ts", "ц": "ts",
    "Ч": "Ch", "ч": "ch", "Ш": "Sh", "ш": "sh", "Щ": "Shch", "щ": "shch",
    "Ъ": "'", "ъ": "'", "Ы": "I", "ы": "i", "Ь": "", "ь": "",
    "Э": "E", "э": "e", "Ю": "Yu", "ю": "yu", "Я": "Ya", "я": "ya",
    "Ў": "O'", "ў": "o'", "Қ": "Q", "қ": "q", "Ғ": "G'", "ғ": "g'",
    "Ҳ": "H", "ҳ": "h",
})


def latinize_uzbek(text: str) -> str:
    """Transliterate Uzbek Cyrillic into Latin for intent matching."""
    return text.translate(_CYRILLIC_TO_LATIN)
