"""证书修订历史：v1 快照、存量补录、同内容不增版、事务与查询 API。"""
import sqlite3

import pytest

from app.database import DB_PATH


AT = "2026-06-15T00:00:00Z"


def _instrument(code="W"):
    return {"code": code, "name": "工作仪器"}


def _cert(
    cid="CW",
    *,
    instrument="W",
    number="CAL-1",
    issued="2026-01-10T00:00:00",
    vf="2026-01-01T00:00:00",
    vu="2027-01-01T00:00:00",
    u=0.002,
    unit="mm",
    digest_alg="sha256",
):
    return {
        "id": cid,
        "instrument_code": instrument,
        "cert_number": number,
        "issued_at": issued,
        "valid_from": vf,
        "valid_until": vu,
        "uncertainty": u,
        "unit": unit,
        "digest_alg": digest_alg,
    }


def _raw_conn():
    return sqlite3.connect(DB_PATH)


# ---------- 单条证书接口 ----------

def test_first_save_creates_v1_snapshot(client):
    client.post("/api/instruments", json=_instrument())
    r = client.post("/api/certificates", json=_cert())
    assert r.status_code == 201

    revs = client.get("/api/certificates/CW/revisions").json()
    assert revs["revision_count"] == 1
    assert revs["latest_version"] == 1
    v1 = revs["revisions"][0]
    assert v1["version"] == 1
    assert v1["backfilled"] is False
    snap = v1["snapshot"]
    assert snap["id"] == "CW"
    assert snap["cert_number"] == "CAL-1"
    assert snap["uncertainty"] == 0.002
    assert snap["unit"] == "mm"
    # 服务端重算并落库的摘要也进入快照
    assert snap["signature_digest"]
    assert snap["signed_payload"]
    # 快照只含业务字段，不含证书行的 created_at
    assert "created_at" not in snap


def test_identical_resubmit_does_not_add_version(client):
    client.post("/api/instruments", json=_instrument())
    body = _cert()
    assert client.post("/api/certificates", json=body).status_code == 201
    assert client.post("/api/certificates", json=body).status_code == 201

    revs = client.get("/api/certificates/CW/revisions").json()
    assert revs["revision_count"] == 1
    assert revs["revisions"][0]["version"] == 1


def test_business_field_change_appends_consecutive_version(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert(u=0.002))
    client.post("/api/certificates", json=_cert(u=0.002))  # 同内容
    client.post("/api/certificates", json=_cert(u=0.003))  # 实际变化

    revs = client.get("/api/certificates/CW/revisions").json()
    assert [r["version"] for r in revs["revisions"]] == [1, 2]
    assert revs["revisions"][1]["backfilled"] is False
    assert revs["revisions"][1]["snapshot"]["uncertainty"] == 0.003


def test_diff_two_versions_lists_changed_fields(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert(u=0.002, vu="2027-01-01T00:00:00"))
    client.post(
        "/api/certificates",
        json=_cert(u=0.003, vu="2028-01-01T00:00:00", number="CAL-1"),
    )
    r = client.get("/api/certificates/CW/revisions/diff?from=1&to=2")
    assert r.status_code == 200
    d = r.json()
    assert d["reversed"] is False
    # uncertainty 与 valid_until 变化后，规范化载荷与其签名摘要也随之重算
    fields = {ch["field"] for ch in d["changes"]}
    assert fields == {
        "uncertainty",
        "valid_until",
        "signed_payload",
        "signature_digest",
    }
    assert d["changed"] is True
    assert d["change_count"] == 4
    u = next(ch for ch in d["changes"] if ch["field"] == "uncertainty")
    assert u["from_value"] == 0.002
    assert u["to_value"] == 0.003


def test_diff_same_version_has_no_changes(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert())
    r = client.get("/api/certificates/CW/revisions/diff?from=1&to=1")
    assert r.status_code == 200
    d = r.json()
    assert d["changed"] is False
    assert d["change_count"] == 0
    assert d["changes"] == []


def test_diff_reversed_order_is_flagged(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert(u=0.002))
    client.post("/api/certificates", json=_cert(u=0.003))
    r = client.get("/api/certificates/CW/revisions/diff?from=2&to=1")
    d = r.json()
    assert d["reversed"] is True
    ch = d["changes"][0]
    assert ch["field"] == "uncertainty"
    assert ch["from_value"] == 0.003
    assert ch["to_value"] == 0.002


def test_diff_missing_version_reports_available_versions(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert())
    client.post("/api/certificates", json=_cert(u=0.005))

    r = client.get("/api/certificates/CW/revisions/diff?from=1&to=9")
    assert r.status_code == 404
    body = r.json()["detail"]
    assert body["code"] == "CERTIFICATE_REVISION_NOT_FOUND"
    assert body["detail"]["version"] == 9
    assert body["detail"]["available_versions"] == [1, 2]

    # 两个版本都缺失
    r2 = client.get("/api/certificates/CW/revisions/diff?from=5&to=6")
    assert r2.status_code == 404
    assert r2.json()["detail"]["detail"]["version"] == 5


def test_revision_endpoints_unknown_certificate_404(client):
    assert client.get("/api/certificates/NOPE/revisions").status_code == 404
    assert (
        client.get("/api/certificates/NOPE/revisions/diff?from=1&to=2").status_code
        == 404
    )


def test_diff_version_zero_is_422(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert())
    r = client.get("/api/certificates/CW/revisions/diff?from=0&to=1")
    assert r.status_code == 422


# ---------- 批量提交 ----------

def test_batch_submit_versions_each_certificate_by_id(client):
    payload = {
        "instruments": [_instrument(), _instrument("R")],
        "certificates": [
            _cert("CW"),
            _cert("CR", instrument="R", number="STD-1", u=0.0001),
        ],
        "edges": [],
    }
    assert client.post("/api/submissions", json=payload).status_code == 201
    assert client.get("/api/certificates/CW/revisions").json()[
        "latest_version"
    ] == 1
    assert client.get("/api/certificates/CR/revisions").json()[
        "latest_version"
    ] == 1

    # 同内容整批重交：不增版
    assert client.post("/api/submissions", json=payload).status_code == 201
    assert (
        client.get("/api/certificates/CW/revisions").json()["revision_count"]
        == 1
    )

    # 批次内更新其中一张：只该证书增版
    payload["certificates"][0] = _cert("CW", u=0.004)
    assert client.post("/api/submissions", json=payload).status_code == 201
    cw = client.get("/api/certificates/CW/revisions").json()
    cr = client.get("/api/certificates/CR/revisions").json()
    assert [v["version"] for v in cw["revisions"]] == [1, 2]
    assert cr["revision_count"] == 1


def test_batch_preflight_leaves_no_revisions(client):
    client.post("/api/instruments", json=_instrument())
    body = {"instruments": [], "certificates": [_cert()], "edges": []}
    r = client.post("/api/submissions/preflight", json=body)
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    # 预检不写证书，也不写版本
    assert client.get("/api/certificates").json() == []
    assert (
        client.get("/api/certificates/CW/revisions").status_code == 404
    )  # 证书不存在

    # 对既有证书预检“更新”同样不留版本
    client.post("/api/certificates", json=_cert(u=0.002))
    body["certificates"] = [_cert(u=0.009)]
    r = client.post("/api/submissions/preflight", json=body)
    assert r.status_code == 200
    revs = client.get("/api/certificates/CW/revisions").json()
    assert revs["revision_count"] == 1
    assert revs["revisions"][0]["snapshot"]["uncertainty"] == 0.002


def test_batch_validation_failure_rolls_back_versions(client):
    """批次中后续条目失败（422），同批此前写入的版本也必须随事务回滚。"""
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert(u=0.002))

    # 仪器 R 不存在 -> 证书 CR 写入会 422；CW 与 CR 在同一批，
    # 证书 CR 先失败时整批回滚，CW 即便先处理也不得留下新版本
    r = client.post(
        "/api/submissions",
        json={
            "instruments": [],
            "certificates": [
                _cert("CW", u=0.007),
                _cert("CR", instrument="R", number="STD-1"),
            ],
            "edges": [],
        },
    )
    assert r.status_code == 422
    # 库内 CW 仍为旧值，版本没有增加
    assert client.get("/api/certificates/CW").json()["uncertainty"] == 0.002
    revs = client.get("/api/certificates/CW/revisions").json()
    assert revs["revision_count"] == 1


def test_batch_edge_failure_rolls_back_cert_versions(client):
    """证书先成功、后续边校验失败时，证书更新与其新版本一并回滚。"""
    client.post("/api/submissions", json={
        "instruments": [_instrument(), _instrument("R")],
        "certificates": [
            _cert("CW", u=0.002),
            _cert("CR", instrument="R", number="STD-1", u=0.0001),
        ],
        "edges": [{"child_cert_id": "CW", "parent_cert_id": "CR"}],
    })
    r = client.post("/api/submissions", json={
        "instruments": [],
        "certificates": [_cert("CW", u=0.008)],
        # 自环边必然 422：发生在证书写入之后
        "edges": [{"child_cert_id": "CW", "parent_cert_id": "CW"}],
    })
    assert r.status_code == 422
    assert client.get("/api/certificates/CW").json()["uncertainty"] == 0.002
    revs = client.get("/api/certificates/CW/revisions").json()
    assert revs["revision_count"] == 1
    assert revs["revisions"][0]["snapshot"]["uncertainty"] == 0.002


# ---------- 存量证书：首次更新前补录旧值 ----------

def test_legacy_certificate_backfilled_before_first_change(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert(u=0.002))

    # 模拟“版本功能上线前的存量证书”：直接删掉其版本记录
    conn = _raw_conn()
    conn.execute("DELETE FROM certificate_revisions WHERE certificate_id='CW'")
    conn.commit()
    conn.close()

    # 首次更新：先补录旧值 v1（backfilled），再追加 v2
    client.post("/api/certificates", json=_cert(u=0.004))
    revs = client.get("/api/certificates/CW/revisions").json()
    assert [v["version"] for v in revs["revisions"]] == [1, 2]
    v1, v2 = revs["revisions"]
    assert v1["backfilled"] is True
    assert v1["snapshot"]["uncertainty"] == 0.002
    assert v2["backfilled"] is False
    assert v2["snapshot"]["uncertainty"] == 0.004

    d = client.get(
        "/api/certificates/CW/revisions/diff?from=1&to=2"
    ).json()
    # 业务字段仅 uncertainty 变化；载荷与摘要随服务端重算一并更新
    fields = {ch["field"] for ch in d["changes"]}
    assert fields == {"uncertainty", "signed_payload", "signature_digest"}
    u = next(ch for ch in d["changes"] if ch["field"] == "uncertainty")
    assert u["from_value"] == 0.002
    assert u["to_value"] == 0.004


def test_legacy_certificate_identical_resubmit_backfills_only(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert(u=0.002))

    conn = _raw_conn()
    conn.execute("DELETE FROM certificate_revisions WHERE certificate_id='CW'")
    conn.commit()
    conn.close()

    # 存量证书以相同内容重交：补录旧值 v1，但不追加 v2
    client.post("/api/certificates", json=_cert(u=0.002))
    revs = client.get("/api/certificates/CW/revisions").json()
    assert revs["revision_count"] == 1
    assert revs["revisions"][0]["backfilled"] is True

    # 之后再实际变化，版本号必须连续为 v2
    client.post("/api/certificates", json=_cert(u=0.005))
    revs = client.get("/api/certificates/CW/revisions").json()
    assert [v["version"] for v in revs["revisions"]] == [1, 2]


def test_revisions_cleared_by_admin_reset(client):
    client.post("/api/instruments", json=_instrument())
    client.post("/api/certificates", json=_cert())
    assert (
        client.get("/api/certificates/CW/revisions").json()["revision_count"]
        == 1
    )
    client.post("/api/admin/reset")
    conn = _raw_conn()
    n = conn.execute("SELECT COUNT(*) FROM certificate_revisions").fetchone()[0]
    conn.close()
    assert n == 0


# ---------- 摘要字段也参与版本判定 ----------

def test_explicit_digest_change_is_a_new_version(client):
    client.post("/api/instruments", json=_instrument())
    # 服务端重算摘要
    client.post("/api/certificates", json=_cert())
    v1_digest = client.get("/api/certificates/CW/revisions").json()["revisions"][
        0
    ]["snapshot"]["signature_digest"]

    # 业务字段相同但显式提交另一个（被篡改的）摘要：属于实际内容变化
    tampered = _cert()
    tampered["signature_digest"] = "0" * 64
    r = client.post("/api/certificates", json=tampered)
    assert r.status_code == 201
    revs = client.get("/api/certificates/CW/revisions").json()
    assert [v["version"] for v in revs["revisions"]] == [1, 2]
    assert (
        revs["revisions"][1]["snapshot"]["signature_digest"]
        != v1_digest
    )
