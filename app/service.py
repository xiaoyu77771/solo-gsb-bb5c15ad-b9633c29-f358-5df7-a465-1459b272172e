"""交接链核心业务逻辑。

哈希规则
--------
创世事件（seq=0）：
    canonical = sort_keys_json({"file_hash": ..., "holder": ..., "seq": 0})
    event_hash = SHA256(canonical)
移交事件（seq>=1）：
    canonical = sort_keys_json({
        "giver": 交出方, "nonce": 一次性随机数, "prev_hash": 前序哈希,
        "receiver": 接收方, "seq": 序号,
    })
    event_hash = SHA256(canonical)

交出方由服务端依据链尾持有人（current_holder）确定，客户端不能伪造。

待接收移交（邀约）流程
----------------------
1. 持有人以最新 tail_hash 发起邀约（指定 receiver、有效期、一次性确认码），
   服务端只保存确认码摘要并返回 offer_id；
2. 接收方凭 offer_id + 确认码确认：在同一 SQLite 立即事务中校验邀约
   未过期、未使用且链尾仍匹配，然后追加唯一移交事件并将邀约置为 accepted；
3. 过期、重复确认或链尾已推进均返回 409 且不写事件。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .database import read_tx, write_tx
from .hashing import canonical_json, digest_hex


class ChainError(Exception):
    """业务错误：携带 HTTP 状态码。"""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _code_digest(code: str) -> str:
    """一次性确认码的存储摘要：带域分隔的 SHA-256，绝不落库明文。"""
    return digest_hex(f"offer-code:{code}")


def _genesis_payload(file_hash: str, holder: str) -> dict[str, Any]:
    return {"file_hash": file_hash, "holder": holder, "seq": 0}


def _handover_payload(
    seq: int, prev_hash: str, giver: str, receiver: str, nonce: str
) -> dict[str, Any]:
    # 键名顺序无所谓 —— canonical_json 会 sort_keys；显式写出仅为可读性。
    return {
        "giver": giver,
        "nonce": nonce,
        "prev_hash": prev_hash,
        "receiver": receiver,
        "seq": seq,
    }


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "prev_hash": row["prev_hash"],
        "giver": row["giver"],
        "receiver": row["receiver"],
        "nonce": row["nonce"],
        "event_hash": row["event_hash"],
        "canonical": row["canonical"],
        "created_at": row["created_at"],
    }


def create_chain(file_hash: str, first_holder: str) -> dict[str, Any]:
    """创建交接链：写入创世记录（序号 0）。原子完成。"""
    chain_id = uuid.uuid4().hex
    created_at = _now()

    payload = _genesis_payload(file_hash, first_holder)
    canonical = canonical_json(payload)
    genesis_hash = digest_hex(canonical)

    try:
        with write_tx() as conn:
            conn.execute(
                """INSERT INTO chains
                   (chain_id, file_hash, first_holder, tail_hash,
                    current_holder, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chain_id, file_hash, first_holder, genesis_hash,
                 first_holder, created_at),
            )
            conn.execute(
                """INSERT INTO events
                   (chain_id, seq, prev_hash, giver, receiver, nonce,
                    event_hash, canonical, created_at)
                   VALUES (?, 0, '', '', '', '', ?, ?, ?)""",
                (chain_id, genesis_hash, canonical, created_at),
            )
    except sqlite3.IntegrityError as exc:  # 理论上 UUID 冲突才会走到这里
        raise ChainError(409, "conflict", f"链条创建冲突: {exc}") from exc

    return {
        "chain_id": chain_id,
        "file_hash": file_hash,
        "first_holder": first_holder,
        "tail_hash": genesis_hash,
        "current_holder": first_holder,
        "length": 1,
        "created_at": created_at,
        "events": [
            {
                "seq": 0,
                "prev_hash": "",
                "giver": "",
                "receiver": "",
                "nonce": "",
                "event_hash": genesis_hash,
                "canonical": canonical,
                "created_at": created_at,
            }
        ],
    }


def _append_handover_tx(
    conn: sqlite3.Connection, chain: sqlite3.Row, receiver: str, nonce: str
) -> dict[str, Any]:
    """在已开启的写事务内向链尾追加一次移交事件并推进链头。

    调用方必须已完成链尾匹配等前置校验；本函数负责算哈希、插事件、
    更新链头，与调用方的校验同处一个事务，整体原子提交或回滚。
    """
    chain_id = chain["chain_id"]
    giver = chain["current_holder"]
    last_event = conn.execute(
        "SELECT seq FROM events WHERE chain_id = ? ORDER BY seq DESC LIMIT 1",
        (chain_id,),
    ).fetchone()
    seq = last_event["seq"] + 1

    payload = _handover_payload(
        seq=seq,
        prev_hash=chain["tail_hash"],
        giver=giver,
        receiver=receiver,
        nonce=nonce,
    )
    canonical = canonical_json(payload)
    new_hash = digest_hex(canonical)
    created_at = _now()

    try:
        conn.execute(
            """INSERT INTO events
               (chain_id, seq, prev_hash, giver, receiver, nonce,
                event_hash, canonical, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (chain_id, seq, chain["tail_hash"], giver, receiver, nonce,
             new_hash, canonical, created_at),
        )
    except sqlite3.IntegrityError as exc:
        msg = str(exc)
        if "nonce" in msg:
            raise ChainError(
                409, "nonce_replay", "随机数重放被拒绝（并发请求）"
            ) from exc
        raise ChainError(409, "conflict", f"追加冲突: {msg}") from exc

    conn.execute(
        """UPDATE chains
           SET tail_hash = ?, current_holder = ?
           WHERE chain_id = ?""",
        (new_hash, receiver, chain_id),
    )

    return {
        "chain_id": chain_id,
        "seq": seq,
        "prev_hash": chain["tail_hash"],
        "giver": giver,
        "receiver": receiver,
        "nonce": nonce,
        "event_hash": new_hash,
        "canonical": canonical,
        "tail_hash": new_hash,
        "current_holder": receiver,
    }


def append_handover(
    chain_id: str, tail_hash: str, receiver: str, nonce: str
) -> dict[str, Any]:
    """追加一次移交。

    冲突（409）情形：链条不存在 -> 404；链尾哈希不匹配（过期/竞争请求）；
    随机数在该链内重放。任何冲突都不会写入数据，链条不会分叉。
    """
    with write_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        if chain["tail_hash"] != tail_hash:
            raise ChainError(
                409,
                "tail_mismatch",
                "链尾哈希不匹配：链条已被其他移交推进，请获取最新链尾后重试",
            )

        # UNIQUE(chain_id, nonce) 是最终防线；这里先给出明确错误信息。
        dup = conn.execute(
            "SELECT seq FROM events WHERE chain_id = ? AND nonce = ? AND seq > 0",
            (chain_id, nonce),
        ).fetchone()
        if dup is not None:
            raise ChainError(
                409,
                "nonce_replay",
                f"随机数已被使用（重放被拒绝），首次出现于序号 {dup['seq']}",
            )

        result = _append_handover_tx(conn, chain, receiver, nonce)

    return result


def _row_to_offer(row: sqlite3.Row, now: Optional[datetime] = None) -> dict[str, Any]:
    """邀约行 -> 对外字典。expired 是派生状态：pending 且当前时间已过有效期。"""
    status = row["status"]
    if status == "pending":
        now = now or _now_dt()
        if now > _parse_ts(row["expires_at"]):
            status = "expired"
    return {
        "offer_id": row["offer_id"],
        "chain_id": row["chain_id"],
        "receiver": row["receiver"],
        "status": status,
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "accepted_at": row["accepted_at"],
        "accepted_seq": row["accepted_seq"],
    }


def create_offer(
    chain_id: str,
    tail_hash: str,
    receiver: str,
    expires_in_seconds: int,
    confirmation_code: str,
) -> dict[str, Any]:
    """发起待接收移交邀约。

    以当前链尾 tail_hash 锁定邀约：链尾不匹配（链条已推进）返回 409，
    邀约创建即无意义。确认码只保存 SHA-256 摘要，明文不落库。
    """
    with write_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        if chain["tail_hash"] != tail_hash:
            raise ChainError(
                409,
                "tail_mismatch",
                "链尾哈希不匹配：链条已被其他移交推进，请获取最新链尾后再发起邀约",
            )

        offer_id = uuid.uuid4().hex
        now = _now_dt()
        created_at = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(seconds=expires_in_seconds)).isoformat(
            timespec="seconds"
        )

        conn.execute(
            """INSERT INTO offers
               (offer_id, chain_id, tail_hash, receiver, code_digest,
                status, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (offer_id, chain_id, tail_hash, receiver,
             _code_digest(confirmation_code), expires_at, created_at),
        )

    return {
        "offer_id": offer_id,
        "chain_id": chain_id,
        "receiver": receiver,
        "status": "pending",
        "expires_at": expires_at,
        "created_at": created_at,
    }


def confirm_offer(offer_id: str, confirmation_code: str) -> dict[str, Any]:
    """接收方凭 offer_id + 确认码确认邀约，追加唯一移交事件。

    所有校验与写入在同一个 SQLite 立即事务（BEGIN IMMEDIATE）中完成：
    校验确认码摘要 -> 邀约未过期 -> 邀约未使用 -> 链尾仍匹配，
    然后追加移交事件、推进链头并将邀约置为 accepted，整体原子提交。
    过期、重复确认、链尾已推进均返回 409 且整体回滚，不写任何事件。
    """
    with write_tx() as conn:
        offer = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
        if offer is None:
            raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")

        if offer["code_digest"] != _code_digest(confirmation_code):
            raise ChainError(403, "code_mismatch", "确认码不正确")

        if _now_dt() > _parse_ts(offer["expires_at"]):
            raise ChainError(
                409, "offer_expired", "邀约已过期，请由持有人重新发起邀约"
            )

        if offer["status"] != "pending":
            raise ChainError(
                409, "offer_already_accepted", "邀约已被确认，不能重复使用"
            )

        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (offer["chain_id"],)
        ).fetchone()
        if chain is None:  # 外键约束下理论上不可达
            raise ChainError(404, "not_found", f"链条不存在: {offer['chain_id']}")

        if chain["tail_hash"] != offer["tail_hash"]:
            raise ChainError(
                409,
                "tail_mismatch",
                "链尾已推进：邀约所锁定的链尾不再是最新，邀约失效",
            )

        # nonce 由邀约派生，天然一次性：同一邀约不可能确认出第二个事件，
        # 也不会与手工移交的 nonce 冲突；UNIQUE(chain_id, nonce) 兜底。
        result = _append_handover_tx(
            conn, chain, offer["receiver"], nonce=f"offer:{offer_id}"
        )

        conn.execute(
            """UPDATE offers
               SET status = 'accepted', accepted_at = ?, accepted_seq = ?
               WHERE offer_id = ?""",
            (_now(), result["seq"], offer_id),
        )

    result["offer_id"] = offer_id
    return result


def get_offer(offer_id: str) -> dict[str, Any]:
    """查询单个邀约状态（不返回确认码及其摘要）。"""
    with read_tx() as conn:
        row = conn.execute(
            "SELECT * FROM offers WHERE offer_id = ?", (offer_id,)
        ).fetchone()
    if row is None:
        raise ChainError(404, "not_found", f"邀约不存在: {offer_id}")
    return _row_to_offer(row)


def get_chain(chain_id: str) -> dict[str, Any]:
    with read_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")

        rows = conn.execute(
            "SELECT * FROM events WHERE chain_id = ? ORDER BY seq", (chain_id,)
        ).fetchall()

    return {
        "chain_id": chain["chain_id"],
        "file_hash": chain["file_hash"],
        "first_holder": chain["first_holder"],
        "tail_hash": chain["tail_hash"],
        "current_holder": chain["current_holder"],
        "length": len(rows),
        "created_at": chain["created_at"],
        "events": [_row_to_event(r) for r in rows],
    }


def list_chains() -> list[dict[str, Any]]:
    with read_tx() as conn:
        rows = conn.execute(
            """SELECT c.*, COUNT(e.seq) AS length
               FROM chains c
               LEFT JOIN events e ON e.chain_id = c.chain_id
               GROUP BY c.chain_id
               ORDER BY c.created_at, c.chain_id"""
        ).fetchall()
    return [
        {
            "chain_id": r["chain_id"],
            "file_hash": r["file_hash"],
            "current_holder": r["current_holder"],
            "tail_hash": r["tail_hash"],
            "length": r["length"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def verify_chain(chain_id: str) -> dict[str, Any]:
    """重放全链并指出首个断裂序号。

    对每个事件重新计算哈希并与存储值比对，同时校验序号连续与 prev_hash 指向。
    另将重算出的链尾与 chains 头信息比对，以发现头部被直接篡改的情况。
    """
    with read_tx() as conn:
        chain = conn.execute(
            "SELECT * FROM chains WHERE chain_id = ?", (chain_id,)
        ).fetchone()
        if chain is None:
            raise ChainError(404, "not_found", f"链条不存在: {chain_id}")
        rows = conn.execute(
            "SELECT * FROM events WHERE chain_id = ? ORDER BY seq", (chain_id,)
        ).fetchall()

    length = len(rows)
    expected_next_seq = length
    prev_hash = ""
    broken_seq: Optional[int] = None
    reason: Optional[str] = None

    for i, row in enumerate(rows):
        if row["seq"] != i:
            broken_seq, reason = row["seq"], f"序号不连续：期望 {i}，实际 {row['seq']}"
            break

        if row["seq"] == 0:
            payload = _genesis_payload(chain["file_hash"], chain["first_holder"])
        else:
            payload = _handover_payload(
                seq=row["seq"],
                prev_hash=row["prev_hash"],
                giver=row["giver"],
                receiver=row["receiver"],
                nonce=row["nonce"],
            )
        recomputed = digest_hex(canonical_json(payload))

        if recomputed != row["event_hash"]:
            broken_seq, reason = (
                row["seq"],
                f"事件哈希不匹配：记录 {row['event_hash']}，重算 {recomputed}"
                + ("（文件摘要或首位持有人被篡改）" if row["seq"] == 0 else ""),
            )
            break

        if row["event_hash"] != row["prev_hash"] and row["prev_hash"] != prev_hash:
            broken_seq, reason = (
                row["seq"],
                f"前序指针断裂：期望指向 {prev_hash}，实际指向 {row['prev_hash']}",
            )
            break

        prev_hash = row["event_hash"]

    recomputed_tail = prev_hash if broken_seq is None else None

    if broken_seq is None and recomputed_tail != chain["tail_hash"]:
        # 事件本身全部可验证，但链条头的链尾缓存被篡改：没有具体事件序号可指
        reason = (
            "链条头部信息被篡改：链尾哈希为 "
            f"{chain['tail_hash']}，按事件重算应为 {recomputed_tail}"
        )
        # first_broken_seq 保持 null
    valid = broken_seq is None and recomputed_tail == chain["tail_hash"]
    if valid and length > 0:
        # 最后一个事件的接收方必须等于头信息中的当前持有人
        last_holder = chain["first_holder"] if length == 1 else rows[-1]["receiver"]
        if chain["current_holder"] != last_holder:
            valid = False
            reason = (
                "链条头部信息被篡改：当前持有人为 "
                f"{chain['current_holder']}，按事件推导应为 {last_holder}"
            )

    return {
        "chain_id": chain_id,
        "valid": valid,
        "length": length,
        "expected_next_seq": expected_next_seq,
        "first_broken_seq": broken_seq,
        "reason": reason,
        "recomputed_tail_hash": recomputed_tail,
        "stored_tail_hash": chain["tail_hash"],
    }
