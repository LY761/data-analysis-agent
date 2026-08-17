"""
数据脱敏 — 查询结果返回前端前自动对敏感字段打码

脱敏规则:
  - 手机号: 138****1234（保留前3后4）
  - 身份证: 440***********1234（保留前3后4）
  - 邮箱: a***@example.com（保留首字母+域名）
  - 地址: 广东省深圳市***（保留省市，打码详细地址）
  - 银行卡: ****1234（只保留后4位）

使用方式:
  在 routes.py 查询结果返回前调用:
    from services.data_masking import mask_result
    result = mask_result(result)
"""
import re
import logging

logger = logging.getLogger(__name__)

# 脱敏规则：{字段名正则: (脱敏函数, 描述)}
MASK_RULES = {
    # 手机号
    r".*(phone|mobile|tel|电话|手机|联系方式).*": (
        lambda v: re.sub(r'^(\d{3})\d{4}(\d{4})$', r'\1****\2', str(v)) if re.match(r'^\d{11}$', str(v)) else v,
        "手机号→138****1234"
    ),
    # 身份证
    r".*(id_card|身份证|id_number).*": (
        lambda v: re.sub(r'^(\d{3})\d+(\d{4})$', r'\1***********\2', str(v)) if re.match(r'^\d{15,18}', str(v)) else v,
        "身份证→440***********1234"
    ),
    # 邮箱
    r".*(email|邮箱|mail).*": (
        lambda v: re.sub(r'^(.)(.*)(@.*)$', lambda m: m.group(1) + '***' + m.group(3), str(v)) if '@' in str(v) else v,
        "邮箱→a***@example.com"
    ),
    # 银行卡
    r".*(bank|银行卡|card_no|account).*": (
        lambda v: re.sub(r'^.*(\d{4})$', r'****\1', str(v)) if re.match(r'^\d{10,19}$', str(v)) else v,
        "银行卡→****1234"
    ),
    # 地址
    r".*(address|addr|地址|住址).*": (
        lambda v: re.sub(r'^(.{2,6})(.+)$', r'\1***', str(v)) if len(str(v)) > 6 else v,
        "地址→广东省深圳市***"
    ),
}


def mask_value(column_name: str, value) -> str:
    """对单个字段值进行脱敏"""
    if value is None:
        return None

    str_value = str(value)
    for pattern, (mask_func, _) in MASK_RULES.items():
        if re.match(pattern, column_name, re.IGNORECASE):
            return mask_func(str_value)

    return str_value


def mask_result(result: dict) -> dict:
    """返回脱敏副本；兼容执行器结果和 API 响应，不依赖 success 字段。"""
    if not isinstance(result, dict):
        return result

    data = result.get("data")
    if not isinstance(data, list) or not data:
        return dict(result)

    columns = result.get("columns") or (
        list(data[0].keys()) if isinstance(data[0], dict) else []
    )
    sensitive_columns = {
        column
        for column in columns
        if any(re.match(pattern, str(column), re.IGNORECASE) for pattern in MASK_RULES)
    }
    if not sensitive_columns:
        return dict(result)

    masked_result = dict(result)
    masked_result["data"] = [
        {
            key: mask_value(key, value) if key in sensitive_columns else value
            for key, value in row.items()
        }
        if isinstance(row, dict) else row
        for row in data
    ]
    masked_result["_masked"] = True
    logger.info("[Masking] masked columns: %s", sorted(sensitive_columns))
    return masked_result

def get_masking_report(result: dict) -> dict:
    """返回脱敏报告（哪些列被脱敏了，用于审计）"""
    if not result.get("_masked"):
        return {"masked": False}

    columns = result.get("columns", [])
    masked_cols = []
    for col in columns:
        for pattern, (_, desc) in MASK_RULES.items():
            if re.match(pattern, col, re.IGNORECASE):
                masked_cols.append({"column": col, "rule": desc})

    return {"masked": True, "columns": masked_cols}
