from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.core.document_generation import DocumentGenerationError, generate_document


_FIELD = re.compile(r"\[\[([a-z][a-z0-9_]{0,39})\]\]")


def render_document_template(template: dict[str, Any], values_json: str, *,
                             now: datetime, format_override: str | None = None,
                             title_override: str | None = None) -> dict[str, Any]:
    try:
        values = json.loads(values_json or "{}")
    except (TypeError, ValueError) as exc:
        raise DocumentGenerationError("Shablon qiymatlari JSON obyekt bo‘lishi kerak.") from exc
    if not isinstance(values, dict) or len(values) > 20:
        raise DocumentGenerationError("Shablon qiymatlari 20 tagacha maydondan iborat obyekt bo‘lishi kerak.")
    fields = list(dict.fromkeys(str(item) for item in template.get("fields") or []))
    if not fields:
        fields = list(dict.fromkeys(_FIELD.findall(str(template.get("template_text") or ""))))
    unknown = set(values) - set(fields)
    if unknown:
        raise DocumentGenerationError("Qiymat shablonda mavjud bo‘lmagan maydonga berildi: " + ", ".join(sorted(unknown)))
    normalized_values: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise DocumentGenerationError("Shablon qiymati matn yoki raqam bo‘lishi kerak.")
        clean = " ".join(str(value).split())[:2000]
        if clean:
            normalized_values[key] = clean

    content = str(template.get("template_text") or "")
    missing: list[str] = []
    for field in fields:
        marker = f"[[{field}]]"
        if field in normalized_values:
            content = content.replace(marker, normalized_values[field])
        elif marker in content:
            missing.append(field)
    if not content.strip():
        raise DocumentGenerationError("Shablon ichida chiqariladigan matn yo‘q.")
    fmt = format_override or str(template.get("format") or "docx")
    title = (title_override or str(template.get("title") or template.get("name") or "Hisobot")).strip()
    return generate_document({
        "format": fmt,
        "document_type": str(template.get("document_type") or "general"),
        "title": title[:180],
        "sections": [{"heading": "", "body": content[:20_000]}],
        "table_headers": [],
        "table_rows": [],
        "missing_fields": missing,
        "style": str(template.get("style") or "formal"),
    }, now=now)


__all__ = ["render_document_template"]
