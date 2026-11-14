from __future__ import annotations

import re
import unicodedata


def classify_business_message(text: str) -> tuple[str, str]:
    """Cheap local topic and urgency labels; message text is never sent to a classifier."""
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("’", "'").replace("ʻ", "'")
    words = set(re.findall(r"[\w']+", normalized, flags=re.UNICODE))
    groups = (
        ("To‘lov va narx", {"to'lov", "tolov", "narx", "pul", "karta", "chek", "payment", "price", "оплата", "цена"}),
        ("Buyurtma va mahsulot", {"buyurtma", "zakaz", "mahsulot", "tovar", "sotib", "order", "заказ", "товар"}),
        ("Yordam va muammo", {"yordam", "muammo", "xato", "ishlamayapti", "qaytarish", "support", "help", "ошибка", "неработает"}),
        ("Uchrashuv va vaqt", {"uchrashuv", "qabul", "bron", "vaqt", "booking", "appointment", "запись"}),
    )
    matches = lambda tokens: any(word.startswith(token) for word in words for token in tokens)
    topic = next((label for label, tokens in groups if matches(tokens)), "Boshqa murojaat")
    urgency = {"shoshilinch", "zudlik", "tezroq", "darhol", "urgent", "asap", "срочно", "немедленно"}
    priority = "yuqori" if matches(urgency) or "!!!" in text else "oddiy"
    return topic, priority

