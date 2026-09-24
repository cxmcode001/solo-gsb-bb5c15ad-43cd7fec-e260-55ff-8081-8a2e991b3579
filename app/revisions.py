"""证书修订历史（可追查版本）。

版本规则
--------
* 证书首次按 ID 写入时，保存完整业务字段快照为 **v1**；
* 版本功能上线前已存在的**存量证书**（无任何版本记录），在其首次更新
  **之前**先把库内旧值补录为 v1（``backfilled=1``）；
* 仅当实际业务字段发生变化时才追加**连续**新版本（v2、v3……），
  同内容重交不增版；
* 版本写入与证书更新由调用方放在**同一事务**内，任一失败整体回滚。

快照为证书全部业务字段（不含仅记录写入时刻的 ``created_at``）的规范化
JSON（键排序、无空白、UTF-8），任意两版可逐字段比对差异。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .utils import utcnow_str

# 参与版本快照与差异比对的证书业务字段（与 certificates 表列对应，
# 但不含仅记录写入时刻的 created_at）
SNAPSHOT_FIELDS = (
    "id",
    "instrument_code",
    "cert_number",
    "issued_at",
    "valid_from",
    "valid_until",
    "uncertainty",
    "unit",
    "digest_alg",
    "signature_digest",
    "signed_payload",
)


class RevisionNotFound(Exception):
    """请求的证书版本不存在（路由层映射为 HTTP 404）。"""

    def __init__(self, message: str, detail: dict[str, Any]):
        super().__init__(message)
        self.code = "CERTIFICATE_REVISION_NOT_FOUND"
        self.message = message
        self.detail = detail


# ---------------------------------------------------------------- 快照

def canonical_snapshot(values: dict[str, Any]) -> str:
    """按固定字段集生成规范化快照 JSON（键排序、无空白）。"""
    return json.dumps(
        {k: values[k] for k in SNAPSHOT_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_from_row(row: sqlite3.Row) -> str:
    """从库内证书行构造规范化快照（用于存量证书补录旧值与比对）。"""
    return canonical_snapshot({k: row[k] for k in SNAPSHOT_FIELDS})


def parse_snapshot(snapshot: str) -> dict[str, Any]:
    return json.loads(snapshot)


# ---------------------------------------------------------------- 写入

def insert_revision(
    conn: sqlite3.Connection,
    certificate_id: str,
    version: int,
    snapshot: str,
    *,
    backfilled: bool = False,
    created_at: Optional[str] = None,
) -> None:
    """写入一条版本记录。调用方须保证与证书更新处于同一事务。"""
    conn.execute(
        """
        INSERT INTO certificate_revisions(certificate_id, version, snapshot,
                                          backfilled, created_at)
        VALUES (:certificate_id, :version, :snapshot, :backfilled, :created_at)
        """,
        {
            "certificate_id": certificate_id,
            "version": version,
            "snapshot": snapshot,
            "backfilled": 1 if backfilled else 0,
            "created_at": created_at or utcnow_str(),
        },
    )


def record_certificate_write(
    conn: sqlite3.Connection,
    certificate_id: str,
    new_snapshot: str,
    existing_row: Optional[sqlite3.Row],
    *,
    created_at: str,
) -> Optional[int]:
    """证书 upsert 的版本记账，须在证书行 INSERT/UPDATE 的**同一事务**内调用。

    ``existing_row`` 为写入前的库内证书行（新证书为 ``None``）。

    * 新证书：写入 v1（完整快照），返回 ``1``；
    * 存量证书且无版本记录：先把旧值补录为 v1（``backfilled=1``），
      仅当新快照与旧值不同才追加 v2，返回新版本号；内容相同返回 ``None``；
    * 已有版本记录：仅当新快照与最新版本不同才追加 ``latest+1``，
      同内容重交返回 ``None``（不增版）。

    本函数只写版本表，不修改证书行；调用方负责证书行本身的写入。
    """
    if existing_row is None:
        insert_revision(
            conn, certificate_id, 1, new_snapshot, created_at=created_at
        )
        return 1

    latest = get_latest_revision(conn, certificate_id)
    if latest is None:
        # 存量证书首次更新：更新前先补录库内旧值为 v1
        insert_revision(
            conn,
            certificate_id,
            1,
            snapshot_from_row(existing_row),
            backfilled=True,
            created_at=existing_row["created_at"],
        )
        prior_snapshot = snapshot_from_row(existing_row)
        next_version = 2
    else:
        prior_snapshot = latest["snapshot"]
        next_version = int(latest["version"]) + 1

    if new_snapshot == prior_snapshot:
        return None  # 同内容重交，不增版

    insert_revision(
        conn, certificate_id, next_version, new_snapshot, created_at=created_at
    )
    return next_version


# ---------------------------------------------------------------- 读取

def get_latest_revision(
    conn: sqlite3.Connection, certificate_id: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM certificate_revisions
        WHERE certificate_id = ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (certificate_id,),
    ).fetchone()


def get_revision(
    conn: sqlite3.Connection, certificate_id: str, version: int
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificate_revisions "
        "WHERE certificate_id = ? AND version = ?",
        (certificate_id, version),
    ).fetchone()


def list_revisions(
    conn: sqlite3.Connection, certificate_id: str
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificate_revisions WHERE certificate_id = ? "
        "ORDER BY version",
        (certificate_id,),
    ).fetchall()


def revision_out(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "certificate_id": row["certificate_id"],
        "version": int(row["version"]),
        "backfilled": bool(row["backfilled"]),
        "created_at": row["created_at"],
        "snapshot": parse_snapshot(row["snapshot"]),
    }


def build_diff(
    conn: sqlite3.Connection,
    certificate_id: str,
    from_version: int,
    to_version: int,
) -> dict[str, Any]:
    """构造指定两版之间逐字段差异；任一版本缺失即抛 :class:`RevisionNotFound`。"""
    available = [int(r["version"]) for r in list_revisions(conn, certificate_id)]

    def require(version: int) -> sqlite3.Row:
        row = get_revision(conn, certificate_id, version)
        if row is None:
            raise RevisionNotFound(
                f"证书 {certificate_id} 不存在版本 v{version}",
                {
                    "certificate_id": certificate_id,
                    "version": version,
                    "available_versions": available,
                },
            )
        return row

    from_row = require(from_version)
    to_row = require(to_version)
    from_snap = parse_snapshot(from_row["snapshot"])
    to_snap = parse_snapshot(to_row["snapshot"])

    changes = [
        {
            "field": field,
            "from_value": from_snap.get(field),
            "to_value": to_snap.get(field),
        }
        for field in SNAPSHOT_FIELDS
        if from_snap.get(field) != to_snap.get(field)
    ]
    return {
        "certificate_id": certificate_id,
        "from_version": from_version,
        "to_version": to_version,
        "reversed": from_version > to_version,
        "changed": bool(changes),
        "change_count": len(changes),
        "changes": changes,
        "from_revision": revision_out(from_row),
        "to_revision": revision_out(to_row),
    }
