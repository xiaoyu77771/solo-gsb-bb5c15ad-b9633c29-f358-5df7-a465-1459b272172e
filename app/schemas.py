"""请求 / 响应模型与输入校验。"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_NAME_MAX = 200
_NONCE_MAX = 256
_CODE_MAX = 128
_OFFER_TTL_MAX = 7 * 24 * 3600  # 邀约有效期上限：7 天
_WITNESS_MIN = 2  # 见证人数量下限
_WITNESS_MAX = 5  # 见证人数量上限


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


def _validate_token(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("见证人令牌不能为空（一次性令牌）")
    if len(value) > _CODE_MAX:
        raise ValueError(f"见证人令牌长度不能超过 {_CODE_MAX}")
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


class WitnessSpec(BaseModel):
    """发起邀约时登记的单个见证人：标识 + 一次性令牌（服务端只存摘要）。"""

    name: str = Field(..., description="见证人标识")
    token: str = Field(
        ..., description="该见证人的一次性表态令牌，服务端只保存其 SHA-256 摘要"
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_name(v, "见证人标识")

    @field_validator("token")
    @classmethod
    def _check_token(cls, v: str) -> str:
        return _validate_token(v)


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
    witness_threshold: Optional[int] = Field(
        None,
        description="见证通过阈值：同意数达到该值且无拒绝方可确认；无见证邀约不填",
        ge=1,
    )
    witnesses: Optional[list[WitnessSpec]] = Field(
        None,
        description=f"见证人列表（{_WITNESS_MIN}~{_WITNESS_MAX} 名）；不提供则为无见证邀约",
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

    @model_validator(mode="after")
    def _check_witness_config(self) -> "OfferCreateRequest":
        if self.witnesses is None:
            if self.witness_threshold is not None:
                raise ValueError("未提供见证人列表时不能设置 witness_threshold")
            return self
        if not _WITNESS_MIN <= len(self.witnesses) <= _WITNESS_MAX:
            raise ValueError(
                f"见证人数量必须在 {_WITNESS_MIN}~{_WITNESS_MAX} 之间"
            )
        names = [w.name for w in self.witnesses]
        if len(set(names)) != len(names):
            raise ValueError("见证人标识不能重复")
        tokens = [w.token for w in self.witnesses]
        if len(set(tokens)) != len(tokens):
            raise ValueError("见证人令牌不能重复")
        if self.witness_threshold is None:
            raise ValueError("提供见证人时必须设置 witness_threshold")
        if self.witness_threshold > len(self.witnesses):
            raise ValueError("witness_threshold 不能超过见证人数量")
        return self


class OfferOut(BaseModel):
    offer_id: str
    chain_id: str
    receiver: str
    status: str = Field(..., description="邀约状态：pending / accepted / expired")
    expires_at: str
    created_at: str
    witness_threshold: Optional[int] = Field(
        None, description="见证通过阈值；无见证邀约为 null"
    )
    witness_count: int = Field(0, description="见证人数量；无见证邀约为 0")


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
        ...,
        description=(
            "pending（待确认）/ accepted（已确认）/ expired（已过期，派生状态）/ "
            "rejected（已有见证人拒绝，派生状态）"
        ),
    )
    expires_at: str
    created_at: str
    accepted_at: Optional[str] = None
    accepted_seq: Optional[int] = Field(
        None, description="确认后追加的移交事件序号；未确认时为 null"
    )
    witness_threshold: Optional[int] = Field(
        None, description="见证通过阈值；无见证邀约为 null"
    )
    approvals: Optional[int] = Field(
        None, description="见证同意数；无见证邀约为 null"
    )
    rejections: Optional[int] = Field(
        None, description="见证拒绝数；无见证邀约为 null"
    )


class WitnessVoteRequest(BaseModel):
    token: str = Field(..., description="见证人一次性令牌（服务端只比对摘要）")
    decision: Literal["approve", "reject"] = Field(
        ..., description="表态：approve 同意 / reject 拒绝"
    )

    @field_validator("token")
    @classmethod
    def _check_token(cls, v: str) -> str:
        return _validate_token(v)


class WitnessStatusItem(BaseModel):
    """单个见证人的对外状态：绝不包含令牌或其摘要。"""

    witness: str
    decision: str = Field(..., description="pending / approve / reject")
    decided_at: Optional[str] = None


class WitnessStatusOut(BaseModel):
    offer_id: str
    offer_status: str = Field(
        ..., description="邀约派生状态：pending / accepted / expired / rejected"
    )
    witness_threshold: Optional[int] = Field(
        None, description="见证通过阈值；无见证邀约为 null"
    )
    approvals: int = Field(..., description="已表态同意的数量")
    rejections: int = Field(..., description="已表态拒绝的数量")
    pending_count: int = Field(..., description="尚未表态的见证人数量")
    quorum_reached: Optional[bool] = Field(
        None, description="同意数是否已达阈值；无见证邀约为 null"
    )
    witnesses: list[WitnessStatusItem]


class WitnessVoteResult(BaseModel):
    offer_id: str
    witness: str = Field(..., description="按令牌摘要定位到的见证人标识")
    decision: str
    decided_at: str
    witness_threshold: Optional[int] = None
    approvals: int
    rejections: int
    offer_status: str = Field(
        ..., description="表态后的邀约派生状态：pending / rejected"
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
