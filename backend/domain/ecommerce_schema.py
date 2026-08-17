"""平台无关的电商标准实体与字段映射目录。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldDefinition:
    field_key: str
    name: str
    data_type: str
    required: bool = False
    primary_key: bool = False
    aliases: tuple[str, ...] = ()
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    sensitive: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.field_key,
            "name": self.name,
            "data_type": self.data_type,
            "required": self.required,
            "primary_key": self.primary_key,
            "aliases": list(self.aliases),
            "description": self.description,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "sensitive": self.sensitive,
        }


@dataclass(frozen=True)
class EntityDefinition:
    entity_key: str
    name: str
    grain: str
    description: str
    fields: tuple[FieldDefinition, ...]

    def get_field(self, field_key: str) -> FieldDefinition:
        for definition in self.fields:
            if definition.field_key == field_key:
                return definition
        raise KeyError(f"实体 {self.entity_key} 不存在字段: {field_key}")

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.entity_key,
            "name": self.name,
            "grain": self.grain,
            "description": self.description,
            "fields": [field.to_public_dict() for field in self.fields],
        }


def _field(
    key: str,
    name: str,
    data_type: str,
    *,
    required: bool = False,
    primary_key: bool = False,
    aliases: tuple[str, ...] = (),
    description: str = "",
    minimum: float | None = None,
    maximum: float | None = None,
    sensitive: bool = False,
) -> FieldDefinition:
    return FieldDefinition(
        field_key=key,
        name=name,
        data_type=data_type,
        required=required,
        primary_key=primary_key,
        aliases=aliases,
        description=description,
        minimum=minimum,
        maximum=maximum,
        sensitive=sensitive,
    )


def build_entity_registry() -> dict[str, EntityDefinition]:
    entities = [
        EntityDefinition("shop", "店铺", "每行一个店铺", "店铺与平台身份。", (
            _field("shop_id", "店铺 ID", "string", required=True, primary_key=True, aliases=("store_id", "店铺id", "店铺编号")),
            _field("tenant_id", "租户 ID", "string", aliases=("企业id", "商家id")),
            _field("platform", "平台", "string", required=True, aliases=("渠道平台", "电商平台")),
            _field("shop_name", "店铺名称", "string", required=True, aliases=("store_name", "店铺", "店名")),
        )),
        EntityDefinition("product", "商品 SPU", "每行一个商品 SPU", "平台无关的商品主数据。", (
            _field("product_id", "商品 ID", "string", required=True, primary_key=True, aliases=("spu_id", "商品id", "产品id", "商品编号")),
            _field("shop_id", "店铺 ID", "string", aliases=("store_id", "店铺id")),
            _field("product_name", "商品名称", "string", required=True, aliases=("title", "产品名称", "商品", "产品")),
            _field("category", "类目", "string", required=True, aliases=("category_name", "品类", "分类", "产品类别")),
            _field("brand", "品牌", "string", aliases=("brand_name", "品牌名称")),
            _field("list_price", "标价", "number", aliases=("price", "unit_price", "售价", "单价"), minimum=0),
            _field("product_status", "商品状态", "string", aliases=("status", "上下架状态", "是否在售")),
            _field("created_at", "创建时间", "datetime", aliases=("created_date", "上架时间", "上架日期")),
        )),
        EntityDefinition("sku", "商品 SKU", "每行一个 SKU", "规格、售价、成本和状态。", (
            _field("sku_id", "SKU ID", "string", required=True, primary_key=True, aliases=("sku", "规格id", "sku编号")),
            _field("product_id", "商品 ID", "string", required=True, aliases=("spu_id", "商品id", "产品id")),
            _field("sku_name", "SKU 名称", "string", aliases=("规格名称", "销售属性")),
            _field("sale_price", "销售价", "number", required=True, aliases=("price", "unit_price", "售价", "成交价"), minimum=0),
            _field("cost_price", "成本价", "number", aliases=("cost", "商品成本", "成本"), minimum=0),
            _field("sku_status", "SKU 状态", "string", aliases=("status", "上下架状态")),
        )),
        EntityDefinition("order", "订单", "每行一个订单", "订单头与支付结果。", (
            _field("order_id", "订单 ID", "string", required=True, primary_key=True, aliases=("order_no", "订单号", "订单编号")),
            _field("shop_id", "店铺 ID", "string", aliases=("store_id", "店铺id")),
            _field("customer_id", "客户 ID", "string", aliases=("buyer_id", "user_id", "客户id", "买家id"), sensitive=True),
            _field("ordered_at", "下单时间", "datetime", required=True, aliases=("order_date", "created_at", "创建时间", "订单时间")),
            _field("paid_at", "支付时间", "datetime", aliases=("payment_time", "付款时间", "支付日期")),
            _field("paid_amount", "实付金额", "number", required=True, aliases=("total_amount", "payment_amount", "订单金额", "支付金额", "销售额"), minimum=0),
            _field("discount_amount", "优惠金额", "number", aliases=("discount", "折扣金额", "优惠"), minimum=0),
            _field("order_status", "订单状态", "string", required=True, aliases=("status", "交易状态")),
            _field("channel", "订单渠道", "string", aliases=("source", "来源渠道", "流量来源")),
        )),
        EntityDefinition("order_item", "订单明细", "每行一个订单商品明细", "订单中的商品和 SKU 成交信息。", (
            _field("order_item_id", "明细 ID", "string", primary_key=True, aliases=("item_id", "子订单号", "明细id")),
            _field("order_id", "订单 ID", "string", required=True, aliases=("order_no", "订单号")),
            _field("product_id", "商品 ID", "string", required=True, aliases=("spu_id", "商品id", "产品id")),
            _field("sku_id", "SKU ID", "string", aliases=("sku", "规格id")),
            _field("quantity", "成交数量", "integer", required=True, aliases=("qty", "购买数量", "销量"), minimum=0),
            _field("unit_price", "成交单价", "number", required=True, aliases=("price", "sale_price", "单价", "成交价"), minimum=0),
            _field("discount_amount", "明细优惠金额", "number", aliases=("discount", "优惠金额"), minimum=0),
        )),
        EntityDefinition("traffic_daily", "流量日表", "每店铺/商品/渠道/日期一行", "展现到支付的转化过程。", (
            _field("stat_date", "统计日期", "date", required=True, aliases=("date", "日期", "数据日期")),
            _field("shop_id", "店铺 ID", "string", required=True, aliases=("store_id", "店铺id")),
            _field("product_id", "商品 ID", "string", aliases=("spu_id", "商品id")),
            _field("channel", "流量渠道", "string", aliases=("source", "渠道", "流量来源")),
            _field("impressions", "曝光量", "integer", aliases=("exposure", "展现量", "曝光"), minimum=0),
            _field("clicks", "点击量", "integer", aliases=("click_count", "点击"), minimum=0),
            _field("visitors", "访客数", "integer", required=True, aliases=("uv", "访客", "访问人数"), minimum=0),
            _field("add_to_cart_users", "加购人数", "integer", aliases=("cart_users", "加购用户数", "加购人数"), minimum=0),
            _field("orders", "下单数", "integer", aliases=("order_count", "下单人数", "订单数"), minimum=0),
            _field("payers", "支付买家数", "integer", aliases=("paid_buyers", "支付人数", "支付买家"), minimum=0),
        )),
        EntityDefinition("ad_daily", "广告日表", "每计划/素材/商品/日期一行", "广告曝光、点击、消耗和归因成交。", (
            _field("stat_date", "统计日期", "date", required=True, aliases=("date", "日期")),
            _field("campaign_id", "计划 ID", "string", required=True, aliases=("plan_id", "广告计划id", "计划编号")),
            _field("product_id", "商品 ID", "string", aliases=("spu_id", "商品id")),
            _field("impressions", "广告曝光", "integer", aliases=("exposure", "展现量", "曝光"), minimum=0),
            _field("clicks", "广告点击", "integer", aliases=("click_count", "点击"), minimum=0),
            _field("ad_spend", "广告消耗", "number", required=True, aliases=("spend", "cost", "花费", "消耗"), minimum=0),
            _field("attributed_orders", "归因订单数", "integer", aliases=("conversion_orders", "成交订单数"), minimum=0),
            _field("attributed_gmv", "归因成交额", "number", aliases=("conversion_amount", "成交金额", "广告成交额"), minimum=0),
        )),
        EntityDefinition("inventory_snapshot", "库存快照", "每 SKU/仓库/快照时间一行", "库存、在途和库龄快照。", (
            _field("snapshot_at", "快照时间", "datetime", required=True, aliases=("snapshot_time", "统计时间", "快照日期")),
            _field("sku_id", "SKU ID", "string", required=True, aliases=("sku", "规格id")),
            _field("warehouse_id", "仓库 ID", "string", aliases=("warehouse", "仓库id", "仓库")),
            _field("available_stock", "可售库存", "integer", required=True, aliases=("stock_quantity", "available_qty", "库存数量", "可售数量"), minimum=0),
            _field("locked_stock", "锁定库存", "integer", aliases=("locked_qty", "占用库存"), minimum=0),
            _field("inbound_stock", "在途库存", "integer", aliases=("in_transit_qty", "在途数量"), minimum=0),
            _field("stock_age_days", "库龄天数", "integer", aliases=("inventory_age", "库龄"), minimum=0),
        )),
        EntityDefinition("refund", "退款售后", "每笔退款申请一行", "退款金额、原因、状态和商品归因。", (
            _field("refund_id", "退款 ID", "string", required=True, primary_key=True, aliases=("refund_no", "售后单号", "退款单号")),
            _field("order_id", "订单 ID", "string", required=True, aliases=("order_no", "订单号")),
            _field("sku_id", "SKU ID", "string", aliases=("sku", "规格id")),
            _field("refund_amount", "退款金额", "number", required=True, aliases=("amount", "售后金额", "退款"), minimum=0),
            _field("refund_reason", "退款原因", "string", aliases=("reason", "售后原因", "原因")),
            _field("refund_status", "退款状态", "string", required=True, aliases=("status", "售后状态")),
            _field("created_at", "申请时间", "datetime", required=True, aliases=("apply_time", "退款时间", "申请日期")),
        )),
        EntityDefinition("review", "商品评价", "每条评价一行", "评分、文本、情感和商品归因。", (
            _field("review_id", "评价 ID", "string", required=True, primary_key=True, aliases=("comment_id", "评论id", "评价编号")),
            _field("product_id", "商品 ID", "string", required=True, aliases=("spu_id", "商品id", "产品id")),
            _field("sku_id", "SKU ID", "string", aliases=("sku", "规格id")),
            _field("rating", "评分", "number", required=True, aliases=("score", "星级", "评价分数"), minimum=1, maximum=5),
            _field("review_text", "评价内容", "string", aliases=("content", "comment", "评论内容", "评价文字")),
            _field("sentiment", "情感标签", "string", aliases=("情感", "评价类型")),
            _field("reviewed_at", "评价时间", "datetime", required=True, aliases=("review_date", "comment_time", "评价日期", "评论时间")),
        )),
    ]
    return {entity.entity_key: entity for entity in entities}


ENTITY_REGISTRY = build_entity_registry()


def get_entity(entity_key: str) -> EntityDefinition:
    try:
        return ENTITY_REGISTRY[entity_key]
    except KeyError as error:
        raise KeyError(f"标准实体不存在: {entity_key}") from error


def list_entities() -> list[EntityDefinition]:
    return sorted(ENTITY_REGISTRY.values(), key=lambda entity: entity.entity_key)


def entity_catalog() -> dict[str, Any]:
    entities = list_entities()
    return {
        "schema_version": "1.0",
        "entity_count": len(entities),
        "entities": [entity.to_public_dict() for entity in entities],
    }


def normalize_field_name(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value).strip().lower())


def suggest_field_mapping(entity_key: str, source_fields: list[str]) -> dict[str, Any]:
    entity = get_entity(entity_key)
    candidates: dict[str, tuple[str, float]] = {}
    for field in entity.fields:
        for alias in (field.field_key, field.name, *field.aliases):
            normalized = normalize_field_name(alias)
            if normalized:
                current = candidates.get(normalized)
                if current is None or current[1] < 1:
                    candidates[normalized] = (field.field_key, 1.0)

    mapping: dict[str, str] = {}
    details: list[dict[str, Any]] = []
    used_targets: set[str] = set()
    for source in source_fields:
        normalized_source = normalize_field_name(source)
        target = ""
        confidence = 0.0
        if normalized_source in candidates:
            target, confidence = candidates[normalized_source]
        else:
            partial = [
                (candidate_key, candidate_value)
                for candidate_key, candidate_value in candidates.items()
                if len(candidate_key) >= 3 and (candidate_key in normalized_source or normalized_source in candidate_key)
            ]
            if partial:
                partial.sort(key=lambda item: len(item[0]), reverse=True)
                target = partial[0][1][0]
                confidence = 0.75
        if target and target not in used_targets:
            mapping[source] = target
            used_targets.add(target)
        else:
            target = ""
            confidence = 0.0
        details.append({"source_field": source, "target_field": target, "confidence": confidence})

    required_fields = {field.field_key for field in entity.fields if field.required}
    return {
        "entity": entity_key,
        "mapping": mapping,
        "details": details,
        "unmapped_source_fields": [item["source_field"] for item in details if not item["target_field"]],
        "missing_required_fields": sorted(required_fields - set(mapping.values())),
    }
