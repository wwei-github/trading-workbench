"""健康检查（2026-10-05 补本地 logger：与全 API 文件惯例一致，供未来探针扩展）"""
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health_check():
    # 纯静态返回，无可失败路径；升级为 DB/下游探针时在此用 logger 记录检查结果
    return {"status": "ok"}
