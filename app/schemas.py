"""请求 / 响应模型与输入校验。"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME_MAX = 200
_NONCE_MAX = 256
_CODE_MAX = 128
_OFFER_TTL_MAX = 7 * 24 * 3600  # 邀约有效期上限：7 天


def _validate_name(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} 不能为空")
    if len(value) > _NAME_MAX:
        raise ValueError(f"{field} 长度不能超过 {_NAME_MAX}")
    return value


def _validate_hex64(value: str, field: str) -> str:
    value = value.strip().lower()
    if not _HEX64.match(value):
        raise ValueError(f"{field} 必须是 64 位十六进制哈希")
    return value


def _validate_code(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("confirmation_code 不能为空（一次性确认码）")
    if len(value) > _CODE_MAX:
        raise ValueError(f"confirmation_code 长度不能超过 {_CODE_MAX}")
    return value


class CreateChainRequest(BaseModel):
    file_hash: str = Field(..., description="交接文件的 SHA-256 摘要（64 位十六进制）")
    first_holder: str = Field(..., description="首位持有人标识")

    @field_validator("file_hash")
    @classmethod
    def _check_file_hash(cls, v: str) -> str:
        return _validate_hex64(v, "file_hash")

    @field_validator("first_holder")
    @classmethod
    def _check_first_holder(cls, v: str) -> str:
        return _validate_name(v, "first_holder")


class HandoverRequest(BaseModel):
    chain_id: str = Field(..., description="交接链 ID")
    tail_hash: str = Field(..., description="客户端认定的当前链尾哈希")
    receiver: str = Field(..., description="接收方标识")
    nonce: str = Field(..., description="一次性随机数，同一链内不可重复（防重放）")

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")

    @field_validator("receiver")
    @classmethod
    def _check_receiver(cls, v: str) -> str:
        return _validate_name(v, "receiver")

    @field_validator("nonce")
    @classmethod
    def _check_nonce(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nonce 不能为空（一次性随机数）")
        if len(v) > _NONCE_MAX:
            raise ValueError(f"nonce 长度不能超过 {_NONCE_MAX}")
        return v


class EventOut(BaseModel):
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str
    event_hash: str
    canonical: str = Field(..., description="计算该哈希所用的紧凑 JSON 原文，便于审计复核")
    created_at: str


class ChainOut(BaseModel):
    chain_id: str
    file_hash: str
    first_holder: str
    tail_hash: str
    current_holder: str
    length: int = Field(..., description="事件总数（含创世记录，因此首次创建后为 1）")
    created_at: str
    events: list[EventOut]


class ChainSummary(BaseModel):
    chain_id: str
    file_hash: str
    current_holder: str
    tail_hash: str
    length: int
    created_at: str


class HandoverResult(BaseModel):
    chain_id: str
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str
    event_hash: str
    canonical: str
    tail_hash: str
    current_holder: str


class VerifyResult(BaseModel):
    chain_id: str
    valid: bool
    length: int
    expected_next_seq: int
    first_broken_seq: Optional[int] = Field(
        None, description="首个断裂事件的序号；链条头部信息被篡改时为 null"
    )
    reason: Optional[str] = None
    recomputed_tail_hash: Optional[str] = None
    stored_tail_hash: Optional[str] = None


class OfferCreateRequest(BaseModel):
    chain_id: str = Field(..., description="交接链 ID")
    tail_hash: str = Field(..., description="发起邀约时锁定的当前链尾哈希")
    receiver: str = Field(..., description="指定接收方标识（仅其应获知确认码）")
    expires_in_seconds: int = Field(
        ..., description="邀约有效期（秒），自创建时刻起算", ge=1, le=_OFFER_TTL_MAX
    )
    confirmation_code: str = Field(
        ..., description="一次性确认码，服务端只保存其 SHA-256 摘要"
    )

    @field_validator("chain_id")
    @classmethod
    def _check_chain_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("chain_id 不能为空")
        return v

    @field_validator("tail_hash")
    @classmethod
    def _check_tail_hash(cls, v: str) -> str:
        return _validate_hex64(v, "tail_hash")

    @field_validator("receiver")
    @classmethod
    def _check_receiver(cls, v: str) -> str:
        return _validate_name(v, "receiver")

    @field_validator("confirmation_code")
    @classmethod
    def _check_confirmation_code(cls, v: str) -> str:
        return _validate_code(v)


class OfferOut(BaseModel):
    offer_id: str
    chain_id: str
    receiver: str
    status: str = Field(..., description="邀约状态：pending / accepted / expired")
    expires_at: str
    created_at: str


class OfferConfirmRequest(BaseModel):
    confirmation_code: str = Field(..., description="发起邀约时约定的一次性确认码")

    @field_validator("confirmation_code")
    @classmethod
    def _check_confirmation_code(cls, v: str) -> str:
        return _validate_code(v)


class OfferStatusOut(BaseModel):
    offer_id: str
    chain_id: str
    receiver: str
    status: str = Field(
        ..., description="pending（待确认）/ accepted（已确认）/ expired（已过期，派生状态）"
    )
    expires_at: str
    created_at: str
    accepted_at: Optional[str] = None
    accepted_seq: Optional[int] = Field(
        None, description="确认后追加的移交事件序号；未确认时为 null"
    )


class OfferConfirmResult(BaseModel):
    offer_id: str
    chain_id: str
    seq: int
    prev_hash: str
    giver: str
    receiver: str
    nonce: str = Field(..., description="由邀约派生的一次性随机数（offer:<offer_id>）")
    event_hash: str
    canonical: str
    tail_hash: str
    current_holder: str


class HealthOut(BaseModel):
    status: str
