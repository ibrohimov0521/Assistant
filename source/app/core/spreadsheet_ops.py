from __future__ import annotations

import csv
import io
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.core.documents import DocumentError, parse_document


MAX_SPREADSHEET_BYTES = 15 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 500
_UNIT_TO_BASE = {
    "mm": ("length", Decimal("0.001")),
    "cm": ("length", Decimal("0.01")),
    "m": ("length", Decimal("1")),
    "km": ("length", Decimal("1000")),
    "mg": ("mass", Decimal("0.001")),
    "g": ("mass", Decimal("1")),
    "kg": ("mass", Decimal("1000")),
    "ml": ("volume", Decimal("0.001")),
    "l": ("volume", Decimal("1")),
}


class SpreadsheetError(ValueError):
    pass


def _clean_string(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(ch for ch in normalized if ch.isprintable() or ch in "\t\n\r")
    return " ".join(normalized.split())


def _safe_filename(filename: str, suffix: str | None = None) -> str:
    base = PurePosixPath(str(filename).replace("\\", "/")).name
    stem = Path(base).stem[:100]
    stem = re.sub(r"[^\w.-]+", "_", unicodedata.normalize("NFKC", stem)).strip("._-") or "jadval"
    return f"{stem}{suffix or Path(base).suffix.lower()}"


def _parse_csv(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8-sig", errors="strict")
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise SpreadsheetError("CSV fayl UTF-8 formatida o‘qilmadi.") from exc
    if not rows or len(rows) > MAX_ROWS or max(map(len, rows), default=0) > MAX_COLUMNS:
        raise SpreadsheetError("CSV jadval bo‘sh yoki qator/ustun chegarasidan oshgan.")
    return rows


def _open_input(filename: str, mime_type: str | None, content: bytes):
    if len(content) > MAX_SPREADSHEET_BYTES:
        raise SpreadsheetError("Jadval 15 MB dan katta.")
    try:
        parse_document(filename, mime_type, content)
    except DocumentError as exc:
        raise SpreadsheetError(str(exc)) from exc
    suffix = Path(filename).suffix.casefold()
    if suffix == ".csv":
        rows = _parse_csv(content)
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Ma'lumotlar"
        escaped_rows: set[int] = set()
        escaped_cells = 0
        for row_number, row in enumerate(rows, start=1):
            safe_row = []
            row_escaped = False
            for value in row:
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                    try:
                        Decimal(value.strip())
                    except InvalidOperation:
                        value = "'" + value
                        escaped_cells += 1
                        row_escaped = True
                safe_row.append(value)
            sheet.append(safe_row)
            if row_escaped and row_number > 1:
                escaped_rows.add(row_number)
        workbook._assistant_csv_escaped_cells = escaped_cells
        workbook._assistant_csv_escaped_rows = escaped_rows
        return workbook, sheet, True
    if suffix != ".xlsx":
        raise SpreadsheetError("Jadval amallari uchun XLSX yoki CSV faylni yuboring.")
    try:
        workbook = load_workbook(io.BytesIO(content), data_only=False)
    except Exception as exc:
        raise SpreadsheetError("XLSX faylni ocholmadim.") from exc
    visible = [sheet for sheet in workbook.worksheets if sheet.sheet_state == "visible"]
    if not visible:
        workbook.close()
        raise SpreadsheetError("XLSX faylda ko‘rinadigan varaq yo‘q.")
    if workbook.active not in visible:
        workbook.active = workbook.index(visible[0])
    sheet = workbook.active
    if sheet.max_row > MAX_ROWS or sheet.max_column > MAX_COLUMNS:
        workbook.close()
        raise SpreadsheetError("Jadval 10 000 qator yoki 500 ustun chegarasidan oshgan.")
    if sheet.max_row < 1 or sheet.max_column < 1:
        workbook.close()
        raise SpreadsheetError("Faol varaq bo‘sh.")
    return workbook, sheet, False


def _header_map(sheet) -> dict[str, int]:
    if sheet.max_column > MAX_COLUMNS:
        raise SpreadsheetError("Jadvalda 500 tadan ortiq ustun bor.")
    headers: dict[str, int] = {}
    for column in range(1, sheet.max_column + 1):
        value = sheet.cell(1, column).value
        name = _clean_string(str(value)) if value is not None else ""
        if not name:
            name = f"Ustun {get_column_letter(column)}"
        key = name.casefold()
        if key in headers:
            raise SpreadsheetError(f"Ustun sarlavhasi takrorlangan: {name}. Avval sarlavhani aniqlashtiring.")
        headers[key] = column
    if not headers:
        raise SpreadsheetError("Jadvalning birinchi qatorida sarlavhalar bo‘lishi kerak.")
    return headers


def _data_row_count(sheet) -> int:
    return sum(1 for row_number in range(2, sheet.max_row + 1)
               if any(sheet.cell(row_number, column).value not in (None, "")
                      for column in range(1, sheet.max_column + 1)))


def _find_column(headers: dict[str, int], name: Any, *, required: bool = True) -> int | None:
    if name is None or not str(name).strip():
        if required:
            raise SpreadsheetError("Qaysi ustunni o‘zgartirish kerakligi aniqlanmagan.")
        return None
    column = headers.get(_clean_string(str(name)).casefold())
    if column is None:
        raise SpreadsheetError(f"Ustun topilmadi: {name}. Fayldagi sarlavhalarni tekshiring.")
    return column


def _as_decimal(value: Any, header: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SpreadsheetError(f"“{header}” ustunida son bo‘lmagan katak bor.")
    try:
        number = Decimal(str(value).replace(" ", ""))
    except (InvalidOperation, ValueError) as exc:
        raise SpreadsheetError(f"“{header}” ustunidagi “{value}” qiymatini songa aylantirib bo‘lmadi.") from exc
    if not number.is_finite():
        raise SpreadsheetError(f"“{header}” ustunida hisoblab bo‘lmaydigan qiymat bor.")
    return number


def _unit_conversion(item: dict[str, Any]) -> tuple[str, str, Decimal]:
    column = _clean_string(str(item.get("column") or ""))
    source = _clean_string(str(item.get("from_unit") or "")).casefold()
    target = _clean_string(str(item.get("to_unit") or "")).casefold()
    if source not in _UNIT_TO_BASE or target not in _UNIT_TO_BASE:
        raise SpreadsheetError("Birlik almashtirishda mm/cm/m/km, mg/g/kg yoki ml/l birliklaridan foydalaning.")
    source_kind, source_factor = _UNIT_TO_BASE[source]
    target_kind, target_factor = _UNIT_TO_BASE[target]
    if source_kind != target_kind or source == target:
        raise SpreadsheetError(f"{source} dan {target} ga birlik almashtirish yaroqsiz.")
    return column, target, source_factor / target_factor


def _date_value(value: Any, column_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                return parsed.date()
            except ValueError:
                pass
    raise SpreadsheetError(f"“{column_name}” ustunida sana ISO, DD.MM.YYYY yoki DD/MM/YYYY bo‘lishi kerak.")


def _new_sheet_name(workbook, proposed: str) -> str:
    base = proposed[:31]
    name = base
    counter = 2
    while name.casefold() in {item.title.casefold() for item in workbook.worksheets}:
        suffix = f" {counter}"
        name = base[:31 - len(suffix)] + suffix
        counter += 1
    return name


def inspect_spreadsheet(filename: str, mime_type: str | None, content: bytes) -> dict[str, Any]:
    """Validate an upload and expose a small header/type/sample preview for intent routing."""
    workbook, first_sheet, is_csv = _open_input(filename, mime_type, content)
    try:
        sheets = []
        candidates = [first_sheet] if is_csv else [sheet for sheet in workbook.worksheets if sheet.sheet_state == "visible"][:8]
        for sheet in candidates:
            headers = _header_map(sheet)
            selected_headers = list(headers.items())[:30]
            columns = [column for _, column in selected_headers]
            preview_rows = []
            sample_range = range(2, min(sheet.max_row, 4) + 1) if sheet is first_sheet else ()
            for row_number in sample_range:
                values = {}
                for (header, _), column in zip(selected_headers, columns):
                    value = sheet.cell(row_number, column).value
                    if isinstance(value, (datetime, date)):
                        value = value.isoformat()
                    elif value is not None:
                        value = str(value)[:140]
                    values[header] = value
                if any(value is not None for value in values.values()):
                    preview_rows.append(values)
            sheets.append({"name": sheet.title, "headers": [header for header, _ in selected_headers],
                           "column_count": len(headers),
                           "headers_truncated": len(headers) > len(selected_headers),
                           "data_rows": _data_row_count(sheet), "sample": preview_rows})
        return {"filename": _safe_filename(filename), "format": "csv" if is_csv else "xlsx",
                "sheets_truncated": (not is_csv and len([item for item in workbook.worksheets
                                                          if item.sheet_state == "visible"]) > len(candidates)),
                "sheets": sheets}
    finally:
        workbook.close()


def transform_spreadsheet(args: dict[str, Any], *, filename: str, mime_type: str | None,
                          content: bytes) -> dict[str, Any]:
    """Apply a bounded set of explicit, local spreadsheet edits; original bytes remain untouched."""
    workbook, sheet, is_csv = _open_input(filename, mime_type, content)
    try:
        requested_sheet = _clean_string(str(args.get("sheet_name") or ""))
        if requested_sheet:
            match = next((item for item in workbook.worksheets if item.title.casefold() == requested_sheet.casefold()), None)
            if match is None or match.sheet_state != "visible":
                raise SpreadsheetError(f"Ko‘rsatilgan varaq topilmadi: {requested_sheet}.")
            sheet = match
        visible = [item for item in workbook.worksheets if item.sheet_state == "visible"]
        if not requested_sheet and len(visible) > 1:
            raise SpreadsheetError("Faylda bir nechta varaq bor. Qaysi varaqni tahrirlashni ayting: " +
                                   ", ".join(item.title for item in visible[:8]))
        headers = _header_map(sheet)
        rows_before = _data_row_count(sheet)
        operations: list[str] = []
        changed_rows: set[int] = set()
        changed_cells = int(getattr(workbook, "_assistant_csv_escaped_cells", 0))
        changed_rows.update(getattr(workbook, "_assistant_csv_escaped_rows", set()))
        if changed_cells:
            operations.append("CSV formulaga o‘xshash matni xavfsiz oddiy matn sifatida saqlandi")
        removed_rows = 0
        formula_added = False
        summary_rows = 0
        chart_added = False

        clean_all = bool(args.get("clean_all_text"))
        clean_columns = args.get("clean_columns") or []
        if not isinstance(clean_columns, list):
            raise SpreadsheetError("Tozalanadigan ustunlar ro‘yxati noto‘g‘ri.")
        clean_ids = set(range(1, sheet.max_column + 1)) if clean_all else {
            value for name in clean_columns if (value := _find_column(headers, name)) is not None}
        for row_number in range(2, sheet.max_row + 1):
            for column in clean_ids:
                cell = sheet.cell(row_number, column)
                if isinstance(cell.value, str):
                    new_value = _clean_string(cell.value)
                    if new_value != cell.value:
                        cell.value = new_value
                        changed_cells += 1
                        changed_rows.add(row_number)
        if clean_ids:
            operations.append("matn tozalandi")

        conversions = args.get("unit_conversions") or []
        if not isinstance(conversions, list) or len(conversions) > 8:
            raise SpreadsheetError("Bir so‘rovda 8 tagacha birlik almashtirish mumkin.")
        for conversion in conversions:
            if not isinstance(conversion, dict):
                raise SpreadsheetError("Birlik almashtirish parametri noto‘g‘ri.")
            name, target, factor = _unit_conversion(conversion)
            column = _find_column(headers, name)
            for row_number in range(2, sheet.max_row + 1):
                cell = sheet.cell(row_number, column)
                if cell.value is None or (isinstance(cell.value, str) and not cell.value.strip()):
                    continue
                converted = _as_decimal(cell.value, name) * factor
                new_value: int | float = int(converted) if converted == converted.to_integral_value() else float(converted)
                if cell.value != new_value:
                    cell.value = new_value
                    changed_cells += 1
                    changed_rows.add(row_number)
            old_header = _clean_string(str(sheet.cell(1, column).value))
            sheet.cell(1, column).value = f"{old_header} ({target})"
            headers[old_header.casefold()] = column
            headers[f"{old_header} ({target})".casefold()] = column
            operations.append(f"{name}: {target} ga o‘girildi")

        deduplicated = bool(args.get("remove_duplicates"))
        duplicate_columns = args.get("deduplicate_columns") or []
        if not isinstance(duplicate_columns, list):
            raise SpreadsheetError("Takrorni aniqlash ustunlari noto‘g‘ri.")
        if deduplicated:
            columns = ([_find_column(headers, name) for name in duplicate_columns]
                       if duplicate_columns else list(headers.values()))
            seen: set[tuple[Any, ...]] = set()
            duplicates: list[int] = []
            for row_number in range(2, sheet.max_row + 1):
                key = tuple(sheet.cell(row_number, column).value for column in columns)
                if key in seen:
                    duplicates.append(row_number)
                else:
                    seen.add(key)
            for row_number in reversed(duplicates):
                sheet.delete_rows(row_number, 1)
            removed_rows = len(duplicates)
            if removed_rows:
                changed_rows.update(duplicates)
            operations.append("takror qatorlar olib tashlandi")
        data_end_row = sheet.max_row

        formula_operation = str(args.get("formula_operation") or "none")
        if formula_operation != "none":
            if formula_operation not in {"sum", "average", "count"}:
                raise SpreadsheetError("Formula SUM, AVERAGE yoki COUNT bo‘lishi kerak.")
            column_name = str(args.get("formula_column") or "")
            column = _find_column(headers, column_name)
            last_data_row = data_end_row
            if last_data_row < 2:
                raise SpreadsheetError("Formula hisoblash uchun ma’lumot qatori yo‘q.")
            for row_number in range(2, last_data_row + 1):
                value = sheet.cell(row_number, column).value
                if value in (None, ""):
                    continue
                if isinstance(value, str) and value.startswith("="):
                    raise SpreadsheetError("Mavjud formulani formula natijasida ishlata olmayman.")
                _as_decimal(value, column_name)
            label_column = 1 if column != 1 else min(2, sheet.max_column + 1)
            label_cell = sheet.cell(last_data_row + 1, label_column)
            label_cell.value = _clean_string(str(args.get("formula_label") or "Jami"))[:100]
            label_cell.font = Font(bold=True)
            result_cell = sheet.cell(last_data_row + 1, column)
            letter = get_column_letter(column)
            formula = {"sum": "SUM", "average": "AVERAGE", "count": "COUNT"}[formula_operation]
            result_cell.value = f"={formula}({letter}2:{letter}{last_data_row})"
            result_cell.font = Font(bold=True)
            formula_added = True
            changed_rows.add(last_data_row + 1)
            operations.append(f"{formula} formulasi qo‘shildi")

        weekly_operation = str(args.get("weekly_operation") or "none")
        if weekly_operation != "none":
            if weekly_operation not in {"sum", "average", "count"}:
                raise SpreadsheetError("Haftalik jamlanma SUM, AVERAGE yoki COUNT bo‘lishi kerak.")
            date_name = str(args.get("weekly_date_column") or "")
            value_name = str(args.get("weekly_value_column") or "")
            date_column = _find_column(headers, date_name)
            value_column = _find_column(headers, value_name)
            grouped: dict[tuple[int, int, date], list[Decimal]] = {}
            for row_number in range(2, data_end_row + 1):
                raw_date = sheet.cell(row_number, date_column).value
                raw_value = sheet.cell(row_number, value_column).value
                if raw_date in (None, "") and raw_value in (None, ""):
                    continue
                day = _date_value(raw_date, date_name)
                week = day.isocalendar()
                key = (week.year, week.week, day - timedelta(days=day.weekday()))
                grouped.setdefault(key, []).append(_as_decimal(raw_value, value_name))
            if not grouped:
                raise SpreadsheetError("Haftalik jamlanma uchun sana/qiymat topilmadi.")
            summary = workbook.create_sheet(_new_sheet_name(workbook, "Haftalik jamlanma"))
            summary.append(["Hafta boshlanishi", "Yil", "Hafta", f"{value_name} ({weekly_operation})"])
            for cell in summary[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="12304A")
            for (year, week_no, start_day), values in sorted(grouped.items()):
                if weekly_operation == "sum":
                    output = sum(values, Decimal(0))
                elif weekly_operation == "average":
                    output = sum(values, Decimal(0)) / len(values)
                else:
                    output = Decimal(len(values))
                rendered: int | float = int(output) if output == output.to_integral_value() else float(output)
                summary.append([start_day, year, week_no, rendered])
            summary.freeze_panes = "A2"
            summary.auto_filter.ref = summary.dimensions
            summary.column_dimensions["A"].width = 22
            summary.column_dimensions["B"].width = 12
            summary.column_dimensions["C"].width = 12
            summary.column_dimensions["D"].width = 26
            for cell in summary["A"][1:]:
                cell.number_format = "yyyy-mm-dd"
            summary_rows = len(grouped)
            operations.append(f"{summary_rows} haftalik jamlanma qatori qo‘shildi")

        chart_type = str(args.get("chart_type") or "none")
        if chart_type != "none":
            if chart_type not in {"bar", "line", "pie"}:
                raise SpreadsheetError("Grafik turi bar, line yoki pie bo‘lishi kerak.")
            category_name = str(args.get("chart_category_column") or "")
            value_name = str(args.get("chart_value_column") or "")
            category_column = _find_column(headers, category_name)
            value_column = _find_column(headers, value_name)
            if data_end_row < 2:
                raise SpreadsheetError("Grafik yaratish uchun ma’lumot qatori yo‘q.")
            chart_cls = {"bar": BarChart, "line": LineChart, "pie": PieChart}[chart_type]
            chart = chart_cls()
            chart.title = _clean_string(str(args.get("chart_title") or "Jadval grafigi"))[:100]
            if chart_type != "pie":
                chart.y_axis.title = value_name
                chart.x_axis.title = category_name
            chart.add_data(Reference(sheet, min_col=value_column, min_row=1, max_row=data_end_row), titles_from_data=True)
            chart.set_categories(Reference(sheet, min_col=category_column, min_row=2, max_row=data_end_row))
            chart.height = 8
            chart.width = 16
            chart.anchor = f"{get_column_letter(sheet.max_column + 2)}2"
            sheet.add_chart(chart)
            chart_added = True
            operations.append(f"{chart_type} grafigi qo‘shildi")

        if not operations:
            raise SpreadsheetError("Qaysi jadval amalini bajarish kerakligini aniqlashtiring.")
        sheet.freeze_panes = sheet.freeze_panes or "A2"
        if is_csv:
            sheet.auto_filter.ref = sheet.dimensions
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        workbook.calculation.calcMode = "auto"
        output = io.BytesIO()
        workbook.save(output)
        edited = output.getvalue()
        result_name = _safe_filename(filename, suffix="_edited.xlsx")
        if len(edited) > MAX_SPREADSHEET_BYTES:
            raise SpreadsheetError("Tahrirlangan fayl 15 MB dan katta bo‘ldi; kichikroq jadval yuboring.")
        return {"original_filename": _safe_filename(filename), "edited_filename": result_name,
                "original_bytes": bytes(content), "edited_bytes": edited,
                "format": "xlsx", "rows_before": rows_before, "rows_after": rows_before - removed_rows,
                "rows_removed": removed_rows, "rows_changed": len(changed_rows),
                "cells_changed": changed_cells, "formula_added": formula_added,
                "weekly_rows": summary_rows, "chart_added": chart_added,
                "operations": operations, "sheet_name": sheet.title}
    except (DocumentError, SpreadsheetError):
        raise
    except (ValueError, TypeError, KeyError, InvalidOperation) as exc:
        raise SpreadsheetError("Jadvaldagi qiymatlarni shu amal uchun moslay olmadim.") from exc
    finally:
        workbook.close()


__all__ = ["SpreadsheetError", "inspect_spreadsheet", "transform_spreadsheet"]
