"""FastAPI 入口：多人文件交接留痕（哈希链）服务。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config, database
from .schemas import (
    ChainOut,
    ChainSummary,
    CreateChainRequest,
    HandoverRequest,
    HandoverResult,
    HealthOut,
    OfferConfirmRequest,
    OfferConfirmResult,
    OfferCreateRequest,
    OfferOut,
    OfferStatusOut,
    VerifyResult,
    WitnessStatusOut,
    WitnessVoteRequest,
    WitnessVoteResult,
)
from .service import (
    ChainError,
    append_handover,
    confirm_offer,
    create_chain,
    create_offer,
    get_chain,
    get_offer,
    get_witness_status,
    list_chains,
    verify_chain,
    witness_vote,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动即完成数据库初始化（建表 / 校验目录）
    database.init_db()
    yield


app = FastAPI(
    title="文件交接留痕服务",
    description="基于 SHA-256 哈希链的多人文件交接记录、查询与完整性校验，支持见证确认邀约。",
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(ChainError)
async def chain_error_handler(request: Request, exc: ChainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": exc.message},
    )


@app.get("/health", response_model=HealthOut, tags=["meta"], summary="健康检查")
def health() -> HealthOut:
    return HealthOut(status="ok")


@app.post(
    "/chains",
    response_model=ChainOut,
    status_code=201,
    tags=["chains"],
    summary="创建交接链",
    description="提交文件 SHA-256 摘要与首位持有人，生成序号 0 的创世记录并返回其哈希。",
)
def api_create_chain(req: CreateChainRequest) -> ChainOut:
    return ChainOut(**create_chain(req.file_hash, req.first_holder))


@app.post(
    "/handovers",
    response_model=HandoverResult,
    status_code=201,
    tags=["chains"],
    summary="执行一次移交（原子追加）",
    description=(
        "必须携带当前链尾哈希 tail_hash、接收方 receiver 与一次性随机数 nonce。"
        "交出方由服务端按当前持有人确定。链尾不匹配或随机数重放返回 409，不产生分叉。"
    ),
)
def api_handover(req: HandoverRequest) -> HandoverResult:
    return HandoverResult(
        **append_handover(req.chain_id, req.tail_hash, req.receiver, req.nonce)
    )


@app.post(
    "/offers",
    response_model=OfferOut,
    status_code=201,
    tags=["offers"],
    summary="发起待接收移交邀约",
    description=(
        "以最新链尾 tail_hash 锁定邀约，指定 receiver、有效期（秒）与一次性确认码。"
        "服务端只保存确认码的 SHA-256 摘要，返回 offer_id 供接收方确认。"
        "可选见证确认模式：提交 2~5 名见证人（各自独立的一次性令牌，同样只存摘要）"
        "与通过阈值 witness_threshold；确认时须同意数达阈值且无拒绝。"
    ),
)
def api_create_offer(req: OfferCreateRequest) -> OfferOut:
    return OfferOut(
        **create_offer(
            req.chain_id,
            req.tail_hash,
            req.receiver,
            req.expires_in_seconds,
            req.confirmation_code,
            (
                [(w.name, w.token) for w in req.witnesses]
                if req.witnesses is not None
                else None
            ),
            req.witness_threshold,
        )
    )


@app.post(
    "/offers/{offer_id}/confirm",
    response_model=OfferConfirmResult,
    status_code=201,
    tags=["offers"],
    summary="确认邀约（原子追加移交事件）",
    description=(
        "接收方凭 offer_id 与一次性确认码确认。服务端在同一 SQLite 立即事务中校验"
        "邀约未过期、未使用、（见证模式下）见证同意数达阈值且无拒绝、链尾仍匹配，"
        "再追加唯一移交事件并将邀约置为 accepted。"
        "过期、重复确认、见证拒绝、同意数不足或链尾已推进均返回 409，不写事件。"
    ),
)
def api_confirm_offer(offer_id: str, req: OfferConfirmRequest) -> OfferConfirmResult:
    return OfferConfirmResult(**confirm_offer(offer_id, req.confirmation_code))


@app.post(
    "/offers/{offer_id}/witnesses",
    response_model=WitnessVoteResult,
    status_code=201,
    tags=["offers"],
    summary="见证人表态（同意 / 拒绝）",
    description=(
        "见证人凭独立一次性令牌表态，令牌只存 SHA-256 摘要，同一见证人只能表态一次。"
        "邀约已接受、已过期、已被其他见证人拒绝或链尾已推进时返回 409，表态不落库。"
    ),
)
def api_witness_vote(offer_id: str, req: WitnessVoteRequest) -> WitnessVoteResult:
    return WitnessVoteResult(**witness_vote(offer_id, req.token, req.decision))


@app.get(
    "/offers/{offer_id}/witnesses",
    response_model=WitnessStatusOut,
    tags=["offers"],
    summary="查询见证状态",
    description=(
        "返回见证通过阈值、各见证人表态（pending / approve / reject）与统计，"
        "不含令牌及其摘要。"
    ),
)
def api_get_witnesses(offer_id: str) -> WitnessStatusOut:
    return WitnessStatusOut(**get_witness_status(offer_id))


@app.get(
    "/offers/{offer_id}",
    response_model=OfferStatusOut,
    tags=["offers"],
    summary="查询单个邀约状态",
    description=(
        "返回邀约状态（pending / accepted / expired / rejected）与确认结果，"
        "见证模式下附带通过阈值与表态统计；不含确认码、令牌及其摘要。"
    ),
)
def api_get_offer(offer_id: str) -> OfferStatusOut:
    return OfferStatusOut(**get_offer(offer_id))


@app.get(
    "/chains",
    response_model=list[ChainSummary],
    tags=["chains"],
    summary="列出全部交接链",
)
def api_list_chains() -> list[ChainSummary]:
    return [ChainSummary(**c) for c in list_chains()]


@app.get(
    "/chains/{chain_id}",
    response_model=ChainOut,
    tags=["chains"],
    summary="查询链条（含全部事件）",
)
def api_get_chain(chain_id: str) -> ChainOut:
    return ChainOut(**get_chain(chain_id))


@app.get(
    "/chains/{chain_id}/verify",
    response_model=VerifyResult,
    tags=["chains"],
    summary="完整性校验",
    description="逐条重算哈希并校验前序指针，结果给出首个断裂序号 first_broken_seq。",
)
def api_verify_chain(chain_id: str) -> VerifyResult:
    return VerifyResult(**verify_chain(chain_id))


def main() -> None:
    """本地直接运行入口（python -m app.main）。"""
    import uvicorn

    database.init_db()
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
