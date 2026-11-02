from __future__ import annotations

import json
import re
from datetime import datetime
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


FILTER_LABELS = {
    "exclusive_party": "排除其他主体共有",
    "keyword": "关键词", "applicant": "申请主体（原始申请人）", "rightsholder": "当前权利人",
    "inventor": "发明人", "status": "专利状态", "statuses": "专利状态", "title": "专利名称",
    "country": "国家/地区", "application_from": "申请日起", "application_to": "申请日止",
    "grant_from": "授权日起", "grant_to": "授权日止", "co_applicants": "共同申请人",
    "co_applicant_only": "仅匹配原始共同申请人", "unpublished_only": "明确未公开", "publication_missing": "公开日为空",
    "submitted_only": "已有申请日", "count_intent": "统计数量", "overseas_only": "仅海外（排除中国）",
}


def _excel_text(value: Any) -> Any:
    """Keep exported values as inert Excel text, including values beginning with formula markers."""
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _publication_no(item: dict) -> str:
    try:
        raw = json.loads(item.get("raw_json") or "{}")
    except (TypeError, ValueError):
        raw = {}
    for key, value in raw.items():
        compact = re.sub(r"\s+", "", str(key))
        if (("公开" in compact or "公告" in compact) and "号" in compact) and value not in (None, "", "-", "—"):
            return str(value)
    return ""


def _raw_field(item: dict, field_name: str) -> str:
    try:
        raw = json.loads(item.get("raw_json") or "{}")
    except (TypeError, ValueError):
        raw = {}
    target = re.sub(r"\s+", "", field_name)
    for key, value in raw.items():
        if re.sub(r"\s+", "", str(key)) == target and value not in (None, "", "-", "—"):
            return str(value)
    return ""


def build_export_workbook(items: list[dict], filters: dict | None = None) -> bytes:
    filters = filters or {}
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "筛选结果"
    sheet.sheet_view.showGridLines = False

    headers = [
        "序号", "应用领域分类", "业务分类", "申请号", "公开号", "专利名称", "当前状态", "当前权利人", "原始申请主体", "发明人",
        "申请日", "公开日", "授权日", "国家/地区", "专利类型", "简单同族编号", "法律状态/事件", "材料数量",
    ]
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    title = sheet.cell(1, 1, f"专利筛选结果（共 {len(items)} 条）")
    title.font = Font(name="Microsoft YaHei", size=16, bold=True, color="FFFFFF")
    title.fill = PatternFill("solid", fgColor="173D30")
    title.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 34
    sheet.cell(2, 1, f"导出时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
    sheet.cell(2, 1).font = Font(name="Microsoft YaHei", size=10, color="627169")

    header_row = 4
    for column, header in enumerate(headers, 1):
        cell = sheet.cell(header_row, column, header)
        cell.fill = PatternFill("solid", fgColor="D7B96F")
        cell.font = Font(name="Microsoft YaHei", bold=True, color="173D30")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[header_row].height = 26

    for index, item in enumerate(items, 1):
        values = [
            index, _raw_field(item, "应用领域分类"), item.get("demo_category") or _raw_field(item, "业务分类"),
            item.get("application_no"), _publication_no(item), item.get("title_cn") or item.get("title_original"),
            item.get("current_status"), item.get("applicant_current"), item.get("applicant_original"), item.get("inventors"),
            item.get("application_date"), item.get("publication_date"), item.get("grant_date"), item.get("country"),
            item.get("patent_type"), item.get("family_no"), item.get("legal_event"), item.get("material_count") or 0,
        ]
        row_no = header_row + index
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row_no, column, _excel_text(value))
            cell.font = Font(name="Microsoft YaHei", size=10, color="17221D")
            cell.alignment = Alignment(vertical="top", wrap_text=column in (2, 3, 6, 8, 9, 10, 17))
        if index % 2 == 0:
            for column in range(1, len(headers) + 1):
                sheet.cell(row_no, column).fill = PatternFill("solid", fgColor="F5F8F6")

    last_row = max(header_row, header_row + len(items))
    thin = Side(style="thin", color="DDE5E0")
    for row in sheet.iter_rows(min_row=header_row, max_row=last_row, min_col=1, max_col=len(headers)):
        for cell in row:
            cell.border = Border(bottom=thin)
    sheet.freeze_panes = "F5"
    last_column = get_column_letter(len(headers))
    sheet.auto_filter.ref = f"A{header_row}:{last_column}{last_row}"
    widths = [7, 18, 18, 20, 20, 42, 13, 35, 35, 30, 13, 13, 13, 14, 16, 20, 28, 11]
    for column, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.row_dimensions[2].height = 22
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.auto_filter.ref = f"A{header_row}:{last_column}{last_row}"

    conditions = workbook.create_sheet("筛选条件")
    conditions.sheet_view.showGridLines = False
    conditions["A1"] = "本次导出的筛选条件"
    conditions["A1"].font = Font(name="Microsoft YaHei", size=16, bold=True, color="FFFFFF")
    conditions["A1"].fill = PatternFill("solid", fgColor="173D30")
    conditions.merge_cells("A1:B1")
    conditions.append(["条件", "值"])
    for cell in conditions[2]:
        cell.fill = PatternFill("solid", fgColor="D7B96F")
        cell.font = Font(name="Microsoft YaHei", bold=True, color="173D30")
    visible_filters = [(FILTER_LABELS.get(key, key), value) for key, value in filters.items()
                       if key != "interpretation" and value not in (None, "", [], False)]
    if not visible_filters:
        visible_filters = [("筛选范围", "全部专利")]
    for label, value in visible_filters:
        if isinstance(value, list):
            value = "、".join(map(str, value))
        elif isinstance(value, bool):
            value = "是" if value else "否"
        conditions.append([label, _excel_text(value)])
    conditions.column_dimensions["A"].width = 28
    conditions.column_dimensions["B"].width = 60
    conditions.freeze_panes = "A3"
    for row in conditions.iter_rows(min_row=2, max_row=conditions.max_row, min_col=1, max_col=2):
        for cell in row:
            cell.font = Font(name="Microsoft YaHei", size=10, bold=cell.row == 2)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = Border(bottom=thin)

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
