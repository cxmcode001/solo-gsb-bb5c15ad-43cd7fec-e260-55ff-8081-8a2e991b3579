"""证书修订历史路由：版本列表与指定两版字段差异。"""
from __future__ import annotations

from sqlite3 import Connection

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import crud, revisions
from ..database import get_db
from ..schemas import CertificateRevisionDiff, CertificateRevisionList

router = APIRouter(
    prefix="/api/certificates/{cert_id}/revisions",
    tags=["certificate-revisions"],
)


@router.get(
    "",
    response_model=CertificateRevisionList,
    summary="按证书 ID 查询修订版本列表（版本号升序）",
)
def list_certificate_revisions(cert_id: str, db: Connection = Depends(get_db)):
    if crud.get_certificate(db, cert_id) is None:
        raise HTTPException(404, f"证书不存在: {cert_id}")
    rows = revisions.list_revisions(db, cert_id)
    return {
        "certificate_id": cert_id,
        "revision_count": len(rows),
        "latest_version": int(rows[-1]["version"]) if rows else None,
        "revisions": [revisions.revision_out(r) for r in rows],
    }


@router.get(
    "/diff",
    response_model=CertificateRevisionDiff,
    summary="比对指定两版证书的字段差异",
)
def diff_certificate_revisions(
    cert_id: str,
    from_version: int = Query(
        ...,
        alias="from",
        ge=1,
        description="起始版本号（>=1），如 from=1",
    ),
    to_version: int = Query(
        ...,
        alias="to",
        ge=1,
        description="目标版本号（>=1），如 to=2",
    ),
    db: Connection = Depends(get_db),
):
    """返回两版之间每个发生变化的业务字段的旧值/新值。

    证书不存在返回 **404**；任一指定版本不存在也返回 **404**，响应体
    带机器可读 ``code=CERTIFICATE_REVISION_NOT_FOUND`` 与该证书当前
    可用版本号列表 ``available_versions``。``from`` 晚于 ``to`` 时
    仍正常比对，响应中 ``reversed=true``。
    """
    if crud.get_certificate(db, cert_id) is None:
        raise HTTPException(404, f"证书不存在: {cert_id}")
    try:
        return revisions.build_diff(db, cert_id, from_version, to_version)
    except revisions.RevisionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": exc.code,
                "message": exc.message,
                "detail": exc.detail,
            },
        )
