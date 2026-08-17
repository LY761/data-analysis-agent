from middleware.auth_middleware import AuthMiddleware, current_user_ctx
from services.data_masking import mask_result
from agent.sql_validator import sql_validator


def test_root_public_path_does_not_bypass_api_auth():
    assert AuthMiddleware._is_public_path("/") is True
    assert AuthMiddleware._is_public_path("/workspace.js") is True
    assert AuthMiddleware._is_public_path("/api/query") is False


def test_masking_accepts_api_response_without_success_flag():
    result = mask_result({
        "columns": ["customer_phone", "sales"],
        "data": [{"customer_phone": "13812345678", "sales": 100}],
    })
    assert result["data"][0]["customer_phone"] == "138****5678"
    assert result["_masked"] is True


def test_sql_validator_enforces_table_permissions():
    token = current_user_ctx.set({
        "user_id": "viewer",
        "role": "viewer",
        "permissions": {"tables": ["orders"]},
    })
    try:
        result = sql_validator.validate("SELECT product_name FROM products")
    finally:
        current_user_ctx.reset(token)
    assert result["valid"] is False
    assert result["stage"] == "table_permission"