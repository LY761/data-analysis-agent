"""商品与 SKU 的确定性画像、标签、排名和下滑贡献拆解。"""

from __future__ import annotations

from typing import Any

from domain.metric_registry import metric_registry


RULE_VERSION = "1.0.0"

DEFAULT_THRESHOLDS = {
    "high_growth_pct": 20.0,
    "potential_growth_pct": 10.0,
    "profit_margin_pct": 30.0,
    "healthy_margin_pct": 15.0,
    "high_refund_rate_pct": 10.0,
    "healthy_refund_rate_pct": 5.0,
    "low_rating": 3.5,
    "stockout_days": 7.0,
    "slow_moving_days": 60.0,
}

DEFAULT_RANKING_WEIGHTS = {
    "gmv": 0.30,
    "gross_profit": 0.25,
    "gmv_growth_pct": 0.15,
    "average_rating": 0.10,
    "refund_health": 0.10,
    "inventory_health": 0.10,
}


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _safe_divide(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _change_pct(current: float, previous: float) -> float | None:
    return _safe_divide((current - previous) * 100, previous)


def _empty_raw(product_id: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    product = metadata or {}
    return {
        "product_id": product_id,
        "product_name": product.get("product_name") or product_id,
        "category": product.get("category") or "",
        "brand": product.get("brand") or "",
        "gmv": 0.0,
        "order_ids": set(),
        "sold_units": 0.0,
        "cost_of_goods": 0.0,
        "ad_spend": 0.0,
        "refund_amount": 0.0,
        "refund_count": 0,
        "visitors": 0.0,
        "payers": 0.0,
        "available_stock": 0.0,
        "stock_age_days": 0.0,
        "rating_total": 0.0,
        "review_count": 0,
        "period_days": 1,
        "skus": {},
    }


def _period_metrics(raw: dict[str, Any]) -> dict[str, float | int | None]:
    gmv = float(raw.get("gmv", 0))
    sold_units = float(raw.get("sold_units", 0))
    available_stock = float(raw.get("available_stock", 0))
    order_count = len(raw.get("order_ids", set()))
    cost_of_goods = float(raw.get("cost_of_goods", 0))
    ad_spend = float(raw.get("ad_spend", 0))
    refund_amount = float(raw.get("refund_amount", 0))
    visitors = float(raw.get("visitors", 0))
    payers = float(raw.get("payers", 0)) or float(order_count)
    review_count = int(raw.get("review_count", 0))
    period_days = max(1, int(raw.get("period_days", 1)))
    average_daily_sales = sold_units / period_days
    gross_profit = gmv - cost_of_goods - ad_spend - refund_amount

    return {
        "gmv": _round(gmv),
        "order_count": order_count,
        "sold_units": _round(sold_units),
        "average_order_value": _round(_safe_divide(gmv, order_count)),
        "cost_of_goods": _round(cost_of_goods),
        "ad_spend": _round(ad_spend),
        "refund_amount": _round(refund_amount),
        "refund_count": int(raw.get("refund_count", 0)),
        "refund_rate_pct": _round(_safe_divide(refund_amount * 100, gmv)),
        "gross_profit": _round(gross_profit),
        "gross_margin_pct": _round(_safe_divide(gross_profit * 100, gmv)),
        "visitors": _round(visitors),
        "conversion_rate_pct": _round(_safe_divide(payers * 100, visitors)),
        "available_stock": _round(available_stock),
        "sell_through_rate_pct": _round(_safe_divide(sold_units * 100, sold_units + available_stock)),
        "average_daily_sales": _round(average_daily_sales),
        "inventory_days": _round(_safe_divide(available_stock, average_daily_sales)),
        "stock_age_days": _round(float(raw.get("stock_age_days", 0))),
        "average_rating": _round(_safe_divide(float(raw.get("rating_total", 0)), review_count)),
        "review_count": review_count,
    }


def _sku_profiles(current: dict[str, Any], previous: dict[str, Any]) -> list[dict[str, Any]]:
    sku_ids = sorted(set(current.get("skus", {})) | set(previous.get("skus", {})))
    profiles = []
    for sku_id in sku_ids:
        current_raw = current.get("skus", {}).get(sku_id) or _empty_raw(sku_id)
        previous_raw = previous.get("skus", {}).get(sku_id) or _empty_raw(sku_id)
        current_metrics = _period_metrics(current_raw)
        previous_metrics = _period_metrics(previous_raw)
        profiles.append({
            "sku_id": sku_id,
            "sku_name": current_raw.get("sku_name") or previous_raw.get("sku_name") or sku_id,
            "metrics": {
                **current_metrics,
                "gmv_growth_pct": _round(_change_pct(
                    float(current_metrics["gmv"] or 0),
                    float(previous_metrics["gmv"] or 0),
                )),
            },
            "previous_metrics": previous_metrics,
            "gmv_change": _round(float(current_metrics["gmv"] or 0) - float(previous_metrics["gmv"] or 0)),
        })
    profiles.sort(key=lambda item: float(item["metrics"]["gmv"] or 0), reverse=True)
    return profiles


def _normalize_weights(overrides: dict[str, float] | None) -> dict[str, float]:
    weights = dict(DEFAULT_RANKING_WEIGHTS)
    for key, value in (overrides or {}).items():
        if key not in weights:
            raise ValueError(f"不支持的商品排名权重: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"商品排名权重 {key} 必须是非负数字")
        weights[key] = float(value)
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("商品排名权重总和必须大于 0")
    return {key: value / total for key, value in weights.items()}


def _min_max_score(value: float, values: list[float]) -> float:
    minimum = min(values)
    maximum = max(values)
    if maximum == minimum:
        return 0.5
    return (value - minimum) / (maximum - minimum)


def _inventory_health(metrics: dict[str, Any], thresholds: dict[str, float]) -> float:
    days = metrics.get("inventory_days")
    stock = float(metrics.get("available_stock") or 0)
    sold = float(metrics.get("sold_units") or 0)
    if stock == 0 and sold > 0:
        return 0.0
    if days is None:
        return 0.5 if stock == 0 else 0.2
    if thresholds["stockout_days"] <= days <= thresholds["slow_moving_days"]:
        return 1.0
    if days < thresholds["stockout_days"]:
        return max(0.0, days / thresholds["stockout_days"])
    return max(0.0, thresholds["slow_moving_days"] / days)


def _rank_profiles(
    profiles: list[dict[str, Any]],
    weights: dict[str, float],
    thresholds: dict[str, float],
) -> list[dict[str, Any]]:
    if not profiles:
        return []
    positive_keys = ("gmv", "gross_profit", "gmv_growth_pct", "average_rating")
    ranges = {
        key: [float(profile["metrics"].get(key) or 0) for profile in profiles]
        for key in positive_keys
    }
    rankings = []
    for profile in profiles:
        metrics = profile["metrics"]
        component_scores = {
            key: _min_max_score(float(metrics.get(key) or 0), ranges[key])
            for key in positive_keys
        }
        refund_rate = float(metrics.get("refund_rate_pct") or 0)
        component_scores["refund_health"] = max(0.0, 1 - refund_rate / thresholds["high_refund_rate_pct"])
        component_scores["inventory_health"] = _inventory_health(metrics, thresholds)
        breakdown = {
            key: round(component_scores[key] * weight * 100, 2)
            for key, weight in weights.items()
        }
        rankings.append({
            "product_id": profile["product_id"],
            "product_name": profile["product_name"],
            "score": round(sum(breakdown.values()), 2),
            "score_breakdown": breakdown,
        })
    rankings.sort(key=lambda item: (-item["score"], item["product_id"]))
    for rank, item in enumerate(rankings, 1):
        item["rank"] = rank
    return rankings


def _product_tags(
    profile: dict[str, Any],
    total_gmv: float,
    total_visitors: float,
    thresholds: dict[str, float],
) -> list[dict[str, str]]:
    metrics = profile["metrics"]
    growth = metrics.get("gmv_growth_pct")
    margin = metrics.get("gross_margin_pct")
    refund_rate = metrics.get("refund_rate_pct")
    rating = metrics.get("average_rating")
    inventory_days = metrics.get("inventory_days")
    stock = float(metrics.get("available_stock") or 0)
    sold = float(metrics.get("sold_units") or 0)
    tags: list[dict[str, str]] = []

    if growth is not None and growth >= thresholds["potential_growth_pct"] and (margin or 0) >= thresholds["healthy_margin_pct"] and (refund_rate or 0) < thresholds["healthy_refund_rate_pct"]:
        tags.append({"key": "potential", "name": "潜力品", "level": "positive", "reason": "增长、毛利和退款表现同时达到潜力品规则。"})
    if (margin or 0) >= thresholds["profit_margin_pct"] and float(metrics.get("gross_profit") or 0) > 0:
        tags.append({"key": "profit", "name": "利润品", "level": "positive", "reason": "毛利率达到利润品阈值且经营毛利为正。"})

    gmv_share = _safe_divide(float(metrics.get("gmv") or 0), total_gmv) or 0
    visitor_share = _safe_divide(float(metrics.get("visitors") or 0), total_visitors) or 0
    if visitor_share >= 0.2 and visitor_share > gmv_share * 1.3:
        tags.append({"key": "traffic", "name": "引流品", "level": "info", "reason": "访客贡献明显高于成交额贡献。"})

    if stock > 0 and (sold == 0 or (inventory_days is not None and inventory_days >= thresholds["slow_moving_days"])):
        tags.append({"key": "slow_moving", "name": "滞销品", "level": "warning", "reason": "存在库存且近期销量不足或库存可售天数过高。"})
    if stock == 0 and sold > 0 or inventory_days is not None and inventory_days <= thresholds["stockout_days"]:
        tags.append({"key": "stockout_risk", "name": "缺货风险", "level": "warning", "reason": "可售库存不足以覆盖近期销售。"})

    risk_reasons = []
    if refund_rate is not None and refund_rate >= thresholds["high_refund_rate_pct"]:
        risk_reasons.append("退款金额率过高")
    if rating is not None and rating < thresholds["low_rating"]:
        risk_reasons.append("平均评分偏低")
    if growth is not None and growth <= -thresholds["high_growth_pct"]:
        risk_reasons.append("GMV 明显下滑")
    if risk_reasons:
        tags.append({"key": "risk", "name": "风险品", "level": "critical", "reason": "、".join(risk_reasons) + "。"})
    return tags


def _recommendations(tags: list[dict[str, str]]) -> list[str]:
    tag_keys = {tag["key"] for tag in tags}
    recommendations = []
    if "potential" in tag_keys:
        recommendations.append("保持价格与转化优势，逐步增加曝光并观察增量毛利。")
    if "profit" in tag_keys:
        recommendations.append("优先保障利润品库存，并用于关联购和核心陈列。")
    if "traffic" in tag_keys:
        recommendations.append("保留引流能力，同时通过搭配购和详情页提升流量变现。")
    if "slow_moving" in tag_keys:
        recommendations.append("暂停补货，按 SKU 评估降价、组合销售或清仓。")
    if "stockout_risk" in tag_keys:
        recommendations.append("核对在途库存和补货周期，避免高销量 SKU 断货。")
    if "risk" in tag_keys:
        recommendations.append("优先拆解退款原因、差评和下滑 SKU，处理后再扩大投放。")
    if not recommendations:
        recommendations.append("当前未触发默认商品风险规则，继续按 SKU 监控增长、利润和库存。")
    return recommendations


def _evidence(
    profile: dict[str, Any],
    metric_key: str,
    snapshot_ids: list[str],
) -> dict[str, Any]:
    definition = metric_registry.get(metric_key)
    current_value = profile["metrics"].get(metric_key)
    previous_value = profile["previous_metrics"].get(metric_key)
    return {
        "metric_key": metric_key,
        "metric_name": definition.name,
        "metric_version": definition.version,
        "formula": definition.formula,
        "current_value": current_value,
        "previous_value": previous_value,
        "contribution": profile["gmv_change"] if metric_key == "gmv" else None,
        "dimension": {"product_id": profile["product_id"]},
        "source": {
            "type": "standard_snapshots",
            "snapshot_ids": list(snapshot_ids),
            "product_id": profile["product_id"],
        },
    }


def analyze_product_diagnosis(
    current_products: dict[str, dict[str, Any]],
    previous_products: dict[str, dict[str, Any]] | None = None,
    *,
    snapshot_ids: list[str] | None = None,
    product_ids: list[str] | None = None,
    ranking_weights: dict[str, float] | None = None,
    thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    previous = previous_products or {}
    selected_ids = set(product_ids or [])
    resolved_thresholds = dict(DEFAULT_THRESHOLDS)
    for key, value in (thresholds or {}).items():
        if key not in resolved_thresholds:
            raise ValueError(f"不支持的商品诊断阈值: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"商品诊断阈值 {key} 必须是非负数字")
        resolved_thresholds[key] = float(value)
    weights = _normalize_weights(ranking_weights)
    all_product_ids = sorted(set(current_products) | set(previous))

    profiles = []
    for product_id in all_product_ids:
        current = current_products.get(product_id) or _empty_raw(product_id, previous.get(product_id))
        prior = previous.get(product_id) or _empty_raw(product_id, current)
        current_metrics = _period_metrics(current)
        previous_metrics = _period_metrics(prior)
        current_metrics["gmv_growth_pct"] = _round(_change_pct(
            float(current_metrics["gmv"] or 0),
            float(previous_metrics["gmv"] or 0),
        ))
        profile = {
            "product_id": product_id,
            "product_name": current.get("product_name") or prior.get("product_name") or product_id,
            "category": current.get("category") or prior.get("category") or "",
            "brand": current.get("brand") or prior.get("brand") or "",
            "metrics": current_metrics,
            "previous_metrics": previous_metrics,
            "gmv_change": _round(float(current_metrics["gmv"] or 0) - float(previous_metrics["gmv"] or 0)),
            "skus": _sku_profiles(current, prior),
        }
        profiles.append(profile)

    total_gmv = sum(float(profile["metrics"]["gmv"] or 0) for profile in profiles)
    total_visitors = sum(float(profile["metrics"]["visitors"] or 0) for profile in profiles)
    rankings = _rank_profiles(profiles, weights, resolved_thresholds)
    rank_by_product = {item["product_id"]: item for item in rankings}
    evidence = []
    for profile in profiles:
        profile["tags"] = _product_tags(profile, total_gmv, total_visitors, resolved_thresholds)
        profile["recommendations"] = _recommendations(profile["tags"])
        profile["risk_level"] = (
            "high" if any(tag["level"] == "critical" for tag in profile["tags"])
            else "medium" if any(tag["level"] == "warning" for tag in profile["tags"])
            else "low"
        )
        profile["ranking"] = rank_by_product[profile["product_id"]]
        sku_declines = [sku for sku in profile["skus"] if float(sku["gmv_change"] or 0) < 0]
        decline_total = sum(abs(float(sku["gmv_change"] or 0)) for sku in sku_declines)
        profile["sku_decline_contributors"] = [
            {
                "sku_id": sku["sku_id"],
                "sku_name": sku["sku_name"],
                "gmv_change": sku["gmv_change"],
                "contribution_to_decline_pct": _round(abs(float(sku["gmv_change"] or 0)) / decline_total * 100),
            }
            for sku in sorted(sku_declines, key=lambda item: float(item["gmv_change"] or 0))
        ] if decline_total else []
        profile_evidence = [
            _evidence(profile, metric_key, list(snapshot_ids or []))
            for metric_key in (
                "gmv",
                "gross_profit",
                "gross_margin_pct",
                "refund_rate_pct",
                "sold_units",
                "available_stock",
                "inventory_days",
                "average_rating",
            )
        ]
        profile["evidence"] = profile_evidence
        evidence.extend(profile_evidence)

    total_change = sum(float(profile["gmv_change"] or 0) for profile in profiles)
    declines = [profile for profile in profiles if float(profile["gmv_change"] or 0) < 0]
    gross_decline = sum(abs(float(profile["gmv_change"] or 0)) for profile in declines)
    decline_contributors = [
        {
            "product_id": profile["product_id"],
            "product_name": profile["product_name"],
            "gmv_change": profile["gmv_change"],
            "contribution_to_decline_pct": _round(abs(float(profile["gmv_change"] or 0)) / gross_decline * 100),
            "net_change_contribution_pct": _round(float(profile["gmv_change"] or 0) / total_change * 100) if total_change else None,
        }
        for profile in sorted(declines, key=lambda item: float(item["gmv_change"] or 0))
    ] if gross_decline else []

    if selected_ids:
        profiles = [profile for profile in profiles if profile["product_id"] in selected_ids]
        rankings = [ranking for ranking in rankings if ranking["product_id"] in selected_ids]
        decline_contributors = [
            item for item in decline_contributors
            if item["product_id"] in selected_ids
        ]
        evidence = [item for item in evidence if item["dimension"]["product_id"] in selected_ids]

    return {
        "schema_version": "1.0",
        "scenario": "product_diagnosis",
        "rule_version": RULE_VERSION,
        "thresholds": resolved_thresholds,
        "ranking_weights": weights,
        "summary": {
            "product_count": len(profiles),
            "total_gmv": _round(sum(float(profile["metrics"]["gmv"] or 0) for profile in profiles)),
            "total_gmv_change": _round(sum(float(profile["gmv_change"] or 0) for profile in profiles)),
            "high_risk_product_count": sum(profile["risk_level"] == "high" for profile in profiles),
        },
        "profiles": profiles,
        "rankings": rankings,
        "decline_contributors": decline_contributors,
        "evidence": evidence,
        "snapshot_ids": list(snapshot_ids or []),
        "assumptions": [
            "商品 GMV 按订单明细成交数量、成交单价和明细优惠计算。",
            "商品毛利扣除商品成本、商品归因广告消耗和退款金额。",
            "退款优先按 SKU 归因；无 SKU 时仅对单商品订单归因。",
            "标签和排名由版本化规则计算，不由 LLM 决定。",
        ],
    }
