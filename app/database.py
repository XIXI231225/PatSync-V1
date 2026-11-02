from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from core import COMPANY_NAME_ALIASES, DEMO_GROUP_ALIASES, DEMO_GROUP_SEARCH_TERMS, clean, derive_current_status, is_blank, json_text, normalize_application_no


def patent_state_hash(conn):
    rows = [dict(row) for row in conn.execute("SELECT * FROM patents ORDER BY app_key")]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def exclusive_party_match(value, target):
    def normalized(text):
        return re.sub(r"\s+", "", str(text or "")).replace("（", "(").replace("）", ")")
    allowed = DEMO_GROUP_SEARCH_TERMS if target in DEMO_GROUP_ALIASES else (COMPANY_NAME_ALIASES.get(target, target),)
    allowed = {normalized(item) for item in allowed}
    parties = {normalized(item) for item in re.split(r"[|｜;；、,，\n\r]+", str(value or "")) if normalized(item)}
    return int(bool(parties) and parties.issubset(allowed))


def publication_no_from_raw(raw_json: Any) -> str:
    try:
        raw = json.loads(raw_json or "{}") if not isinstance(raw_json, dict) else raw_json
    except (TypeError, ValueError):
        raw = {}
    for key, value in raw.items():
        compact = re.sub(r"\s+", "", str(key))
        if (("公开" in compact or "公告" in compact) and "号" in compact
                and value not in (None, "", "-", "—")):
            return str(value)
    return ""


def demo_category_from_raw(raw_json: Any) -> str:
    try:
        raw = json.loads(raw_json or "{}") if not isinstance(raw_json, dict) else raw_json
    except (TypeError, ValueError):
        raw = {}
    value = raw.get("业务分类")
    return "" if value in (None, "", "-", "—") else str(value)


def normalize_family_id(value: Any) -> str:
    """Return the numeric PatSnap FAM_ID used by the simple-family query."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = re.sub(r"\s+", "", str(value)).strip()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    return text if re.fullmatch(r"\d+", text) else ""


class PatentDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.create_function("norm_id", 1, normalize_application_no, deterministic=True)
        conn.create_function("publication_id", 1, publication_no_from_raw, deterministic=True)
        conn.create_function("exclusive_party", 2, exclusive_party_match, deterministic=True)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS patents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, app_key TEXT NOT NULL UNIQUE, application_no TEXT NOT NULL,
              title_cn TEXT, title_original TEXT, current_status TEXT, legal_event TEXT, applicant_current TEXT,
              applicant_original TEXT, inventors TEXT, application_date TEXT, grant_date TEXT, publication_date TEXT,
              country TEXT, family_no TEXT, patent_type TEXT, raw_json TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_patent_status ON patents(current_status);
            CREATE INDEX IF NOT EXISTS idx_patent_dates ON patents(application_date, grant_date);
            CREATE INDEX IF NOT EXISTS idx_patent_family ON patents(family_no);
            CREATE TABLE IF NOT EXISTS materials (
              id INTEGER PRIMARY KEY AUTOINCREMENT, patent_id INTEGER NOT NULL REFERENCES patents(id) ON DELETE CASCADE,
              category TEXT NOT NULL, original_name TEXT NOT NULL, stored_path TEXT NOT NULL, sha256 TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_material_patent ON materials(patent_id);
            CREATE TABLE IF NOT EXISTS operations (
              id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, operation TEXT NOT NULL,
              application_no TEXT, target TEXT, result TEXT NOT NULL, message TEXT
            );
            CREATE TABLE IF NOT EXISTS source_documents (
              id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE, original_name TEXT NOT NULL,
              stored_path TEXT NOT NULL, content_type TEXT, file_size INTEGER NOT NULL DEFAULT 0,
              page_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS archive_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, source_path TEXT NOT NULL,
              source_name TEXT NOT NULL, intake_source TEXT NOT NULL, status TEXT NOT NULL,
              stage TEXT NOT NULL, source_sha256 TEXT, source_document_id INTEGER,
              category TEXT, document_date TEXT, summary TEXT, action_items_json TEXT NOT NULL DEFAULT '[]',
              evidence_json TEXT NOT NULL DEFAULT '[]', match_method TEXT, match_json TEXT NOT NULL DEFAULT '[]',
              analysis_json TEXT NOT NULL DEFAULT '{}', error_message TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
              auto_archive INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(source_path, fingerprint)
            );
            CREATE TABLE IF NOT EXISTS archive_job_patents (
              job_id INTEGER NOT NULL REFERENCES archive_jobs(id) ON DELETE CASCADE,
              patent_id INTEGER NOT NULL REFERENCES patents(id) ON DELETE CASCADE,
              selected INTEGER NOT NULL DEFAULT 1, matched_identifier TEXT, reason TEXT,
              PRIMARY KEY(job_id, patent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_source_documents_sha256 ON source_documents(sha256);
            CREATE INDEX IF NOT EXISTS idx_archive_jobs_status ON archive_jobs(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_archive_job_patents_job ON archive_job_patents(job_id);
            """)
            existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(materials)")}
            material_columns = {
                "source_document_id": "INTEGER",
                "standard_name": "TEXT",
                "material_date": "TEXT",
                "summary": "TEXT",
                "action_items_json": "TEXT NOT NULL DEFAULT '[]'",
                "match_method": "TEXT",
                "evidence_json": "TEXT NOT NULL DEFAULT '[]'",
                "review_status": "TEXT NOT NULL DEFAULT '已确认'",
            }
            for name, definition in material_columns.items():
                if name not in existing_columns:
                    conn.execute(f"ALTER TABLE materials ADD COLUMN {name} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_material_source_document ON materials(source_document_id)")
            conn.execute("PRAGMA optimize")

    @staticmethod
    def _canonical(row: dict) -> dict:
        return {
            "application_no": clean(row.get("申请号")),
            "title_cn": clean(row.get("标题(译)(简体中文)")),
            "title_original": clean(row.get("标题")),
            "legal_event": clean(row.get("法律状态/事件")),
            "applicant_current": clean(row.get("[标]当前申请(专利权)人") or row.get("当前申请(专利权)人")),
            "applicant_original": clean(row.get("[标]原始申请(专利权)人") or row.get("原始申请(专利权)人")),
            "inventors": clean(row.get("发明人")),
            "application_date": clean(row.get("申请日") or row.get("申请年份")),
            "grant_date": clean(row.get("授权日")),
            "publication_date": clean(row.get("公开(公告)日")),
            "country": clean(row.get("受理局")),
            "family_no": normalize_family_id(row.get("简单同族编号")) or clean(row.get("简单同族编号")),
            "patent_type": clean(row.get("专利类型")),
        }

    def existing_index(self) -> dict[str, dict]:
        with self.connect() as conn:
            return {row["app_key"]: dict(row) for row in conn.execute("SELECT * FROM patents")}

    def family_query_info(self) -> dict:
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT application_no,family_no FROM patents ORDER BY application_no"
            )]
        values = [row["family_no"] for row in rows]
        family_ids = sorted({family_id for value in values if (family_id := normalize_family_id(value))}, key=int)
        invalid_values = sorted({clean(value) for value in values if clean(value) and not normalize_family_id(value)})
        covered_patents = sum(bool(normalize_family_id(value)) for value in values)
        missing_application_numbers = [
            clean(row["application_no"]) for row in rows
            if not normalize_family_id(row["family_no"]) and clean(row["application_no"])
        ]
        return {
            "query": f"FAM_ID:({' OR '.join(family_ids)})" if family_ids else "",
            "family_ids": family_ids,
            "family_count": len(family_ids),
            "covered_patents": covered_patents,
            "missing_patents": len(values) - covered_patents,
            "invalid_values": invalid_values,
            "missing_application_numbers": missing_application_numbers,
            "missing_application_text": "\n".join(missing_application_numbers),
        }

    def import_quality(self, rows: list[dict], preview: list[dict]) -> dict:
        """Summarize one import without changing data."""
        existing = self.existing_index()
        existing_family_ids = {
            family_id for item in existing.values()
            if (family_id := normalize_family_id(item.get("family_no")))
        }
        upload_family_ids = {
            family_id for row in rows
            if (family_id := normalize_family_id(row.get("简单同族编号")))
        }
        invalid_values = sorted({
            clean(row.get("简单同族编号")) for row in rows
            if clean(row.get("简单同族编号")) and not normalize_family_id(row.get("简单同族编号"))
        })
        newly_linked = []
        changed_family = []
        for item in preview:
            if item.get("action") not in {"新增", "更新", "不变"}:
                continue
            incoming = normalize_family_id((item.get("canonical") or {}).get("family_no"))
            current = existing.get(item.get("app_key") or "")
            before = normalize_family_id(current.get("family_no")) if current else ""
            if current and incoming and not before:
                newly_linked.append(item.get("application_no") or "")
            elif current and incoming and before and incoming != before:
                changed_family.append({"application_no": item.get("application_no") or "", "before": before, "after": incoming})
        return {
            "upload_rows": len(rows),
            "upload_family_count": len(upload_family_ids),
            "upload_without_family": sum(not normalize_family_id(row.get("简单同族编号")) for row in rows),
            "new_family_ids": sorted(upload_family_ids - existing_family_ids, key=int),
            "existing_family_ids_not_in_upload": sorted(existing_family_ids - upload_family_ids, key=int),
            "new_family_members": sum(
                item.get("action") == "新增" and bool(normalize_family_id((item.get("canonical") or {}).get("family_no")))
                for item in preview
            ),
            "newly_linked_applications": newly_linked,
            "changed_family_applications": changed_family,
            "classification_inherited": sum("继承" in str(item.get("message") or "") for item in preview),
            "classification_conflicts": sum("分类冲突" in str(item.get("message") or "") for item in preview),
            "duplicate_applications": sum("申请号重复" in str(item.get("message") or "") for item in preview),
            "invalid_family_values": invalid_values,
        }

    def health_report(self) -> dict:
        """Read-only database and material-file health report for the local administrator."""
        with self.connect() as conn:
            patents = [dict(row) for row in conn.execute("SELECT * FROM patents ORDER BY id")]
            materials = [dict(row) for row in conn.execute("SELECT * FROM materials ORDER BY id")]
            pending_jobs = [dict(row) for row in conn.execute(
                "SELECT id,source_name,status,stage,error_message,updated_at FROM archive_jobs "
                "WHERE status IN ('待确认','失败') ORDER BY updated_at DESC"
            )]
        invalid_dates = []
        for patent in patents:
            for field, label in (("application_date", "申请日"), ("grant_date", "授权日"),
                                 ("publication_date", "公开日")):
                value = clean(patent.get(field))
                if not value:
                    continue
                try:
                    date.fromisoformat(value.replace("/", "-"))
                except ValueError:
                    invalid_dates.append({"patent_id": patent["id"], "application_no": patent["application_no"],
                                          "field": label, "value": value})
        missing_files = []
        hash_groups: dict[str, list[dict]] = defaultdict(list)
        patent_by_id = {patent["id"]: patent for patent in patents}
        certificate_patent_ids = {
            material["patent_id"] for material in materials
            if clean(material.get("category")) == "专利证书"
        }
        for material in materials:
            path = self.path.parent / str(material.get("stored_path") or "")
            if not path.is_file():
                missing_files.append({"material_id": material["id"], "patent_id": material["patent_id"],
                                      "category": material["category"], "name": material["original_name"],
                                      "stored_path": material["stored_path"]})
            if material.get("sha256"):
                hash_groups[str(material["sha256"])].append(material)
        same_case_duplicates = []
        cross_patent_copies = []
        for digest, items in hash_groups.items():
            case_counts = Counter((item["patent_id"], item["category"]) for item in items)
            duplicate_ids = [
                item["id"] for item in items
                if case_counts[(item["patent_id"], item["category"])] > 1
            ]
            if duplicate_ids:
                duplicate_patent_ids = sorted({
                    item["patent_id"] for item in items if item["id"] in duplicate_ids
                })
                same_case_duplicates.append({"sha256": digest, "material_ids": duplicate_ids,
                                             "patent_ids": duplicate_patent_ids,
                                             "patents": [{
                                                 "patent_id": patent_id,
                                                 "application_no": patent_by_id.get(patent_id, {}).get("application_no") or "",
                                                 "title": patent_by_id.get(patent_id, {}).get("title_cn")
                                                          or patent_by_id.get(patent_id, {}).get("title_original") or "",
                                             } for patent_id in duplicate_patent_ids],
                                             "names": sorted({item["original_name"] for item in items if item["id"] in duplicate_ids})})
            if len({item["patent_id"] for item in items}) > 1:
                cross_patent_copies.append({"sha256": digest, "material_ids": [item["id"] for item in items],
                                            "patent_count": len({item["patent_id"] for item in items})})
        family_categories: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        invalid_family_values = Counter()
        missing_family = 0
        missing_publication = 0
        pending_status = []
        authorized_without_certificate = []
        for patent in patents:
            try:
                raw = json.loads(patent.get("raw_json") or "{}")
            except (TypeError, ValueError):
                raw = {}
            family_id = normalize_family_id(patent.get("family_no"))
            if not family_id:
                missing_family += 1
                if clean(patent.get("family_no")):
                    invalid_family_values[clean(patent.get("family_no"))] += 1
            else:
                for field in ("应用领域分类", "业务分类"):
                    if not is_blank(raw.get(field)):
                        family_categories[family_id][field].add(clean(raw.get(field)))
            if not publication_no_from_raw(raw):
                missing_publication += 1
            if patent.get("current_status") == "状态待确认":
                pending_status.append({"patent_id": patent["id"], "application_no": patent["application_no"],
                                       "legal_event": patent.get("legal_event") or ""})
            if patent.get("current_status") == "已授权" and patent["id"] not in certificate_patent_ids:
                authorized_without_certificate.append({
                    "patent_id": patent["id"],
                    "application_no": patent.get("application_no") or "",
                    "publication_no": publication_no_from_raw(raw),
                    "title": patent.get("title_cn") or patent.get("title_original") or "",
                    "grant_date": patent.get("grant_date") or "",
                })
        family_conflicts = []
        for family_id, fields in family_categories.items():
            conflicts = {field: sorted(values) for field, values in fields.items() if len(values) > 1}
            if conflicts:
                family_conflicts.append({"family_id": family_id, "conflicts": conflicts})
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": {"patents": len(patents), "materials": len(materials),
                        "hard_errors": len(invalid_dates) + len(missing_files),
                        "review_items": len(same_case_duplicates) + len(family_conflicts) + len(pending_status)
                                        + len(authorized_without_certificate),
                        "pending_archive": sum(item["status"] == "待确认" for item in pending_jobs),
                        "failed_archive": sum(item["status"] == "失败" for item in pending_jobs)},
            "invalid_dates": invalid_dates,
            "missing_files": missing_files,
            "same_case_duplicates": same_case_duplicates,
            "cross_patent_copies": cross_patent_copies,
            "missing_publication": missing_publication,
            "missing_family": missing_family,
            "invalid_family_values": dict(invalid_family_values),
            "family_conflicts": family_conflicts,
            "pending_status": pending_status,
            "authorized_without_certificate": authorized_without_certificate,
            "archive_attention": pending_jobs,
        }

    @staticmethod
    def _prepare_family_classifications(rows: list[dict], existing: dict[str, dict]) -> list[dict]:
        fields = ("应用领域分类", "业务分类")
        seeds: dict[str, dict[str, set[str]]] = {}
        prepared = [dict(row) for row in rows]
        for row in prepared:
            family_id = normalize_family_id(row.get("简单同族编号"))
            if not family_id:
                continue
            row["简单同族编号"] = family_id
            family_seed = seeds.setdefault(family_id, {field: set() for field in fields})
            current = existing.get(normalize_application_no(clean(row.get("申请号"))))
            current_raw = json.loads(current["raw_json"]) if current else {}
            for field in fields:
                for value in (row.get(field), current_raw.get(field)):
                    if not is_blank(value):
                        family_seed[field].add(clean(value))
        conflicts = {
            family_id: {field: sorted(values) for field, values in field_values.items() if len(values) > 1}
            for family_id, field_values in seeds.items()
        }
        conflicts = {family_id: values for family_id, values in conflicts.items() if values}
        for row in prepared:
            family_id = normalize_family_id(row.get("简单同族编号"))
            if not family_id:
                continue
            if family_id in conflicts:
                details = "；".join(f"{field}={' / '.join(values)}" for field, values in conflicts[family_id].items())
                row["__family_conflict"] = f"简单同族编号 {family_id} 分类冲突：{details}"
                continue
            inherited = []
            for field in fields:
                values = seeds[family_id][field]
                if is_blank(row.get(field)) and len(values) == 1:
                    row[field] = next(iter(values))
                    inherited.append(field)
            if inherited:
                row["__family_inherited"] = f"按简单同族编号 {family_id} 继承{'、'.join(inherited)}"
        return prepared

    def build_import_preview(self, rows: list[dict], *, inherit_family=True) -> list[dict]:
        existing = self.existing_index()
        if inherit_family:
            rows = self._prepare_family_classifications(rows, existing)
        groups: dict[str, list[dict]] = {}
        output = []
        for row in rows:
            raw_no = clean(row.get("申请号")); key = normalize_application_no(raw_no)
            if row.get("__family_conflict"):
                output.append({"row": row["__row__"], "application_no": raw_no, "action": "跳过", "changes": [], "message": row["__family_conflict"]})
            elif not key:
                output.append({"row": row["__row__"], "application_no": raw_no, "action": "跳过", "changes": [], "message": "申请号为空"})
            else:
                groups.setdefault(key, []).append(row)
        for key, items in groups.items():
            if len(items) > 1:
                for row in items:
                    output.append({"row": row["__row__"], "application_no": clean(row.get("申请号")), "action": "跳过", "changes": [], "message": "新版总表申请号重复"})
                continue
            row = items[0]; canonical = self._canonical(row); current = existing.get(key)
            if current:
                for name in tuple(canonical):
                    if is_blank(canonical[name]) and name in current:
                        canonical[name] = current[name]
            current_raw = json.loads(current["raw_json"]) if current else {}
            merged_raw = dict(current_raw)
            changes = []
            for name, value in row.items():
                if name.startswith("__") or is_blank(value):
                    continue
                value = clean(value)
                if clean(current_raw.get(name)) != value:
                    changes.append(name)
                merged_raw[name] = value
            status = derive_current_status(canonical["patent_type"], canonical["legal_event"])
            if current and current_raw.get("状态锁定") in (True, "是", "锁定", 1, "1"):
                status = current["current_status"]
            elif not current or current["current_status"] != status:
                changes.append("当前状态")
            output.append({"row": row["__row__"], "application_no": canonical["application_no"], "app_key": key,
                           "action": "新增" if not current else ("更新" if changes else "不变"),
                           "changes": sorted(set(changes)), "message": row.get("__family_inherited", ""), "record": row,
                           "merged_raw": merged_raw, "canonical": canonical, "status": status})
        return sorted(output, key=lambda x: x["row"])

    def execute_import(self, preview: list[dict], operation: str = "更新总表", *, expected_state=None) -> dict:
        added = updated = skipped = 0; now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            if expected_state is not None:
                conn.execute("BEGIN IMMEDIATE")
                if patent_state_hash(conn) != expected_state:
                    raise ValueError("总表在预览后已有变化，请重新获取并预览")
            for item in preview:
                if item["action"] not in ("新增", "更新"):
                    skipped += item["action"] == "跳过"; continue
                c = item["canonical"]
                values = (item["app_key"], c["application_no"], c["title_cn"], c["title_original"], item["status"],
                          c["legal_event"], c["applicant_current"], c["applicant_original"], c["inventors"],
                          c["application_date"], c["grant_date"], c["publication_date"], c["country"], c["family_no"],
                          c["patent_type"], json_text(item["merged_raw"]), now)
                conn.execute("""INSERT INTO patents(app_key,application_no,title_cn,title_original,current_status,legal_event,
                  applicant_current,applicant_original,inventors,application_date,grant_date,publication_date,country,family_no,
                  patent_type,raw_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(app_key) DO UPDATE SET application_no=excluded.application_no,title_cn=excluded.title_cn,
                  title_original=excluded.title_original,current_status=excluded.current_status,legal_event=excluded.legal_event,
                  applicant_current=excluded.applicant_current,applicant_original=excluded.applicant_original,
                  inventors=excluded.inventors,application_date=excluded.application_date,grant_date=excluded.grant_date,
                  publication_date=excluded.publication_date,country=excluded.country,family_no=excluded.family_no,
                  patent_type=excluded.patent_type,raw_json=excluded.raw_json,updated_at=excluded.updated_at""", values)
                added += item["action"] == "新增"; updated += item["action"] == "更新"
            conn.execute("INSERT INTO operations(created_at,operation,result,message) VALUES(?,?,?,?)",
                         (now, operation, "成功", f"新增 {added}，更新 {updated}，跳过 {skipped}"))
        return {"added": added, "updated": updated, "skipped": skipped}

    def add_from_acceptance_notice(self, record: dict, source_name: str) -> dict:
        """Insert one strongly evidenced new application through the normal merge rules."""
        row = {"__row__": 1, **record}
        preview = self.build_import_preview([row])
        if len(preview) != 1:
            raise ValueError("受理通知书未形成唯一专利记录")
        item = preview[0]
        if item["action"] == "跳过":
            raise ValueError(item.get("message") or "受理通知书中的申请号无法新增")
        if item["action"] == "新增":
            self.execute_import(preview, operation="受理通知书自动新增")
        patent = self.patent_by_key(str(item.get("app_key") or ""))
        if not patent:
            raise ValueError("受理通知书专利记录创建失败")
        return {**patent, "was_added": item["action"] == "新增", "source_name": source_name}

    def search(self, filters: dict, limit=300) -> list[dict]:
        clauses, params = [], []
        if filters.get("unpublished_only"):
            clauses.append("legal_event LIKE ?")
            params.append("%未公开%")
        def party_expression(column: str) -> str:
            return f"replace(replace(replace(replace({column},'（','('),'）',')'),' ',''),'　','')"

        def normalized_party(value: str) -> str:
            return value.replace("（", "(").replace("）", ")").replace(" ", "").replace("　", "")

        mapping = {"status": "current_status", "title": "title_cn"}
        for key, column in mapping.items():
            value = str(filters.get(key, "")).strip()
            if value:
                clauses.append(f"{column} LIKE ?"); params.append(f"%{value}%")
        country = str(filters.get("country", "")).strip()
        overseas_terms = {"海外", "境外", "国外", "非中国", "海外专利"}
        if filters.get("overseas_only") or country in overseas_terms:
            clauses.append("(country IS NOT NULL AND trim(country)<>'' AND trim(country) NOT IN ('中国','中华人民共和国','中国国家知识产权局'))")
        elif country:
            clauses.append("country LIKE ?"); params.append(f"%{country}%")
        inventor = str(filters.get("inventor", "")).strip()
        if inventor:
            clauses.append("""(inventors LIKE ? OR (
                family_no IS NOT NULL AND trim(family_no)<>'' AND family_no NOT IN ('-', '—') AND
                family_no IN (SELECT family_no FROM patents WHERE inventors LIKE ? AND
                    family_no IS NOT NULL AND trim(family_no)<>'' AND family_no NOT IN ('-', '—'))
            ))""")
            params.extend([f"%{inventor}%", f"%{inventor}%"])
        applicant = str(filters.get("applicant", "")).strip()
        if filters.get("exclusive_party") and applicant:
            clauses.append("exclusive_party(applicant_original, ?)=1")
            params.append(applicant)
        if applicant:
            if applicant in DEMO_GROUP_ALIASES:
                choices = []
                for term in DEMO_GROUP_SEARCH_TERMS:
                    choices.append("applicant_original LIKE ?")
                    params.append(f"%{term}%")
                clauses.append(("(" + " OR ".join(choices) + ")") if choices else "0")
            else:
                clauses.append(party_expression("applicant_original") + " LIKE ?")
                params.append(f"%{normalized_party(applicant)}%")
        rightsholder = str(filters.get("rightsholder", "")).strip()
        if filters.get("exclusive_party") and rightsholder:
            clauses.append("exclusive_party(applicant_current, ?)=1")
            params.append(rightsholder)
        if rightsholder:
            if rightsholder in DEMO_GROUP_ALIASES:
                choices = []
                for term in DEMO_GROUP_SEARCH_TERMS:
                    choices.append("applicant_current LIKE ?")
                    params.append(f"%{term}%")
                clauses.append(("(" + " OR ".join(choices) + ")") if choices else "0")
            else:
                clauses.append(party_expression("applicant_current") + " LIKE ?")
                params.append(f"%{normalized_party(rightsholder)}%")
        statuses = filters.get("statuses") or []
        if isinstance(statuses, str):
            statuses = [x.strip() for x in statuses.split("|") if x.strip()]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"current_status IN ({placeholders})")
            params.extend(statuses)
        co_applicants = filters.get("co_applicants") or []
        if isinstance(co_applicants, str):
            co_applicants = [x.strip() for x in co_applicants.split("|") if x.strip()]
        for term in co_applicants:
            if term in DEMO_GROUP_ALIASES:
                column_names = ("applicant_original",) if filters.get("co_applicant_only") else ("applicant_current", "applicant_original")
                choices = []
                for company in DEMO_GROUP_SEARCH_TERMS:
                    for column in column_names:
                        choices.append(f"{column} LIKE ?")
                        params.append(f"%{company}%")
                clauses.append(("(" + " OR ".join(choices) + ")") if choices else "0")
            elif filters.get("co_applicant_only"):
                clauses.append("applicant_original LIKE ?")
                params.append(f"%{term}%")
            else:
                clauses.append("(applicant_current LIKE ? OR applicant_original LIKE ?)")
                params.extend([f"%{term}%", f"%{term}%"])
        if filters.get("publication_missing"):
            clauses.append("(publication_date IS NULL OR trim(publication_date)='' OR trim(publication_date) IN ('-', '—'))")
        if filters.get("submitted_only"):
            clauses.append("(application_date IS NOT NULL AND trim(application_date)<>'' AND trim(application_date) NOT IN ('-', '—'))")
        for key, column, op in (("application_from", "application_date", ">="), ("application_to", "application_date", "<="),
                                ("grant_from", "grant_date", ">="), ("grant_to", "grant_date", "<=")):
            value = str(filters.get(key, "")).strip()
            if value: clauses.append(f"{column} {op} ?"); params.append(value)
        keyword = str(filters.get("keyword", "")).strip()
        material_category = str(filters.get("material_category", "")).strip()
        if material_category:
            clauses.append("EXISTS (SELECT 1 FROM materials mc WHERE mc.patent_id=p.id AND mc.category=?)")
            params.append(material_category)
        if keyword:
            tokens = [x for x in re_split(keyword) if x]
            for token in tokens:
                normalized = normalize_application_no(token)
                if re.fullmatch(r"[A-Za-z0-9./_\-．]+", token) and sum(c.isdigit() for c in normalized) >= 7:
                    clauses.append("(norm_id(application_no) LIKE ? OR norm_id(publication_id(raw_json)) LIKE ?)")
                    params.extend([f"%{normalized}%"] * 2)
                    continue
                variants = [token]
                for round_no, chinese in ((1, "一"), (2, "二"), (3, "三")):
                    if token in (f"第{chinese}次审查意见通知书", f"第{chinese}次审查意见", f"{chinese}通", f"OA{round_no}"):
                        variants = [f"第{chinese}次审查意见", f"{chinese}通", f"OA{round_no}"]
                keyword_clauses = []
                for variant in variants:
                    keyword_clauses.append("""(application_no LIKE ? OR title_cn LIKE ? OR title_original LIKE ? OR applicant_current LIKE ?
                    OR applicant_original LIKE ? OR inventors LIKE ? OR legal_event LIKE ? OR raw_json LIKE ?
                    OR EXISTS (SELECT 1 FROM materials mk WHERE mk.patent_id=p.id
                        AND (mk.category LIKE ? OR mk.summary LIKE ? OR mk.action_items_json LIKE ? OR mk.original_name LIKE ?)))""")
                    params.extend([f"%{variant}%"] * 12)
                clauses.append("(" + " OR ".join(keyword_clauses) + ")")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            rows = conn.execute("SELECT p.*, (SELECT COUNT(*) FROM materials m WHERE m.patent_id=p.id) material_count FROM patents p" + where + " ORDER BY application_date DESC, id DESC LIMIT ?", (*params, limit)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["publication_no"] = publication_no_from_raw(item.get("raw_json"))
                item["demo_category"] = demo_category_from_raw(item.get("raw_json"))
                results.append(item)
            return results

    def patent_detail(self, patent_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM patents WHERE id=?", (patent_id,)).fetchone()
            if not row: return None
            result = dict(row); result["raw"] = json.loads(result.pop("raw_json"))
            result["publication_no"] = publication_no_from_raw(result["raw"])
            result["demo_category"] = demo_category_from_raw(result["raw"])
            result["materials"] = []
            for material in conn.execute("SELECT * FROM materials WHERE patent_id=? ORDER BY category,created_at DESC", (patent_id,)):
                item = dict(material)
                for field in ("action_items_json", "evidence_json"):
                    try:
                        item[field.removesuffix("_json")] = json.loads(item.get(field) or "[]")
                    except (TypeError, ValueError):
                        item[field.removesuffix("_json")] = []
                path = self.path.parent / str(item.get("stored_path") or "")
                try:
                    item["file_size"] = path.stat().st_size if path.is_file() else 0
                except OSError:
                    item["file_size"] = 0
                result["materials"].append(item)
            duplicate_groups: dict[str, list[dict]] = {}
            for item in result["materials"]:
                digest = str(item.get("sha256") or "")
                if digest:
                    duplicate_groups.setdefault(digest, []).append(item)
            for group in duplicate_groups.values():
                if len(group) < 2:
                    continue
                primary_id = min(int(item["id"]) for item in group)
                for item in group:
                    item["duplicate_count"] = len(group)
                    item["duplicate_primary_id"] = primary_id
            return result

    def materials_for_patents(self, patent_ids: list[int]) -> list[dict]:
        if not patent_ids:
            return []
        output = []
        with self.connect() as conn:
            for start in range(0, len(patent_ids), 800):
                chunk = patent_ids[start:start + 800]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(f"""SELECT m.*, p.application_no, p.title_cn, p.title_original
                    FROM materials m JOIN patents p ON p.id=m.patent_id
                    WHERE m.patent_id IN ({placeholders})
                    ORDER BY m.patent_id, m.category, m.created_at DESC, m.id DESC""", chunk).fetchall()
                output.extend(dict(row) for row in rows)
        return output

    def patent_by_key(self, key: str):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM patents WHERE app_key=?", (key,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _identifier_variants(value: Any) -> set[str]:
        normalized = normalize_application_no(value)
        if len(normalized) < 8:
            return set()
        variants = {normalized}
        # Keep the country/office prefix when stripping a publication kind code.
        # This lets a filename such as ``Register Extract DEMO-PUBLICATION.pdf`` select
        # the EP record itself instead of every family member sharing 3260569.
        without_kind_full = re.sub(r"[A-Z]{1,2}\d?$", "", normalized)
        if len(without_kind_full) >= 8:
            variants.add(without_kind_full)
        without_country = re.sub(r"^[A-Z]{2}(?=\d)", "", normalized)
        if len(without_country) >= 8:
            variants.add(without_country)
        without_kind = re.sub(r"[A-Z]{1,2}\d?$", "", without_country)
        if len(without_kind) >= 8:
            variants.add(without_kind)
        # Japanese granted patent numbers are officially seven digits and are
        # often listed after one shared "JP Patents" heading. Index the bare
        # number only for JP grant publications, keeping other seven-digit
        # dates, matter numbers, and family references out of the match index.
        if re.fullmatch(r"JP\d{7}B\d?", normalized) and re.fullmatch(r"\d{7}", without_kind):
            variants.add(without_kind)
        return variants

    def material_identifier_index(self) -> dict[str, dict[int, dict]]:
        index: dict[str, dict[int, dict]] = {}
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM patents")]
        for patent in rows:
            values = [patent["application_no"]]
            raw = json.loads(patent.get("raw_json") or "{}")
            for field, value in raw.items():
                compact = re.sub(r"\s+", "", str(field))
                if "申请号" in compact or (("公开" in compact or "公告" in compact) and "号" in compact):
                    values.extend(re.split(r"[|,，;；\s]+", str(value or "")))
            for value in values:
                for identifier in self._identifier_variants(value):
                    index.setdefault(identifier, {})[patent["id"]] = patent
        return index

    @staticmethod
    def match_material_filename(filename: str, index: dict[str, dict[int, dict]]) -> tuple[dict | None, str, bool]:
        normalized_name = normalize_application_no(Path(filename).stem)
        matches: dict[int, dict] = {}
        matched_identifiers = []
        for identifier, patents in index.items():
            if identifier in normalized_name:
                matches.update(patents)
                matched_identifiers.append(identifier)
        if len(matches) == 1:
            return next(iter(matches.values())), max(matched_identifiers, key=len), False
        return None, "", len(matches) > 1

    def material_name_exists(self, patent_id: int, category: str, name: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM materials WHERE patent_id=? AND category=? AND lower(original_name)=lower(?)", (patent_id, category, name)).fetchone() is not None

    def add_material(self, patent_id: int, category: str, name: str, stored_path: str, digest: str,
                     application_no: str, *, source_document_id: int | None = None, standard_name: str = "",
                     material_date: str = "", summary: str = "", action_items: list | None = None,
                     match_method: str = "文件名编号", evidence: list | None = None,
                     review_status: str = "已确认"):
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute("""INSERT INTO materials(
                patent_id,category,original_name,stored_path,sha256,created_at,source_document_id,standard_name,
                material_date,summary,action_items_json,match_method,evidence_json,review_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (patent_id, category, name, stored_path, digest, now, source_document_id, standard_name or name,
                 material_date, summary, json_text(action_items or []), match_method, json_text(evidence or []), review_status))
            conn.execute("INSERT INTO operations(created_at,operation,application_no,target,result,message) VALUES(?,?,?,?,?,?)",
                         (now, "上传材料", application_no, name, "成功", category))

    def material_hash_exists(self, patent_id: int, category: str, digest: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM materials WHERE patent_id=? AND category=? AND sha256=?",
                (patent_id, category, digest),
            ).fetchone() is not None

    def patent_candidates(self, text: str, filename: str = "", limit: int = 8) -> list[dict]:
        """Return bounded local candidates for AI suggestions; never performs an automatic match."""
        source = f"{filename}\n{text}".lower()
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM patents")]
        ranked = []
        for patent in rows:
            fields = {
                "title": patent.get("title_cn") or patent.get("title_original") or "",
                "current_applicant": patent.get("applicant_current") or "",
                "original_applicant": patent.get("applicant_original") or "",
                "inventors": patent.get("inventors") or "",
            }
            score, reasons = 0, []
            for label, value in fields.items():
                value = str(value).strip()
                if not value:
                    continue
                tokens = [x for x in re.split(r"[|,，、;；\s]+", value) if len(x) >= 2]
                hits = [token for token in tokens if token.lower() in source]
                if hits:
                    weight = 5 if label == "title" else 3 if "applicant" in label else 2
                    score += min(len(hits), 4) * weight
                    reasons.append(f"{label}:{'、'.join(hits[:3])}")
            if score:
                ranked.append((score, patent, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
        output = []
        for score, patent, reasons in ranked[:limit]:
            output.append({
                "id": patent["id"], "application_no": patent["application_no"],
                "publication_no": publication_no_from_raw(patent.get("raw_json")),
                "title": patent.get("title_cn") or patent.get("title_original") or "",
                "applicant_current": patent.get("applicant_current") or "",
                "applicant_original": patent.get("applicant_original") or "",
                "inventors": patent.get("inventors") or "", "score": score, "reason": "；".join(reasons),
            })
        return output

    def material(self, material_id: int):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
            return dict(row) if row else None

    def stats(self):
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM patents").fetchone()[0]
            materials = conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0]
            statuses = {row[0]: row[1] for row in conn.execute("SELECT current_status,COUNT(*) FROM patents GROUP BY current_status")}
            group_conditions, group_params = [], []
            for company in DEMO_GROUP_SEARCH_TERMS:
                group_conditions.append("applicant_current LIKE ?")
                group_params.append(f"%{company}%")
            group_total = conn.execute(
                "SELECT COUNT(*) FROM patents WHERE " + (" OR ".join(group_conditions) or "0"), group_params
            ).fetchone()[0]
            return {"total": total, "group_total": group_total, "materials": materials, "authorized": statuses.get("已授权", 0),
                    "pending": statuses.get("申请中", 0), "statuses": statuses}

    def logs(self, limit=500):
        with self.connect() as conn:
            return [dict(x) for x in conn.execute("SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,))]


def re_split(text: str):
    stop = {"的", "专利", "查询", "查找", "找到", "一下", "有哪些", "以及", "和", "清单", "列表"}
    return [token for token in __import__("re").split(r"[\s,，。；;]+", text) if token and token not in stop]
