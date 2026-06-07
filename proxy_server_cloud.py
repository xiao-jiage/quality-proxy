#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
质量数据分析系统 — 云端代理服务器（适配 Railway / Render）
作用：通过腾讯文档 Open API 读取在线表格数据，为 HTML 页面提供 HTTP API
用法：Railway 自动部署，或本地测试：python proxy_server_cloud.py

认证方式：个人开发者版（无需 Client Secret）
  - 使用 Access-Token + Client-Id + Open-Id 调用 API
  - Access Token 有效期约 30 天，过期后需手动重新授权
"""

import os
import json
import time
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, jsonify, request, Response

app = Flask(__name__)

# =============================================================================
# 环境变量配置（从 Railway 控制台设置）
# =============================================================================
CLIENT_ID = os.environ.get("TENCENT_DOC_CLIENT_ID", "").strip()
ACCESS_TOKEN = os.environ.get("TENCENT_DOC_ACCESS_TOKEN", "").strip()
OPEN_ID = os.environ.get("TENCENT_DOC_OPEN_ID", "").strip()
FILE_ID = os.environ.get("TENCENT_DOC_FILE_ID", "DVHNUbFJWSk5tbE93").strip()

# 可选：代理密码保护（强烈建议设置）
PROXY_AUTH_USER = os.environ.get("PROXY_AUTH_USER", "").strip()
PROXY_AUTH_PASS = os.environ.get("PROXY_AUTH_PASS", "").strip()

# 腾讯文档 API 基础地址
API_V2_BASE = "https://docs.qq.com/openapi/sheetbook/v2"
API_V3_BASE = "https://docs.qq.com/openapi/spreadsheet/v3"

# 缓存
cache_data = None
cache_timestamp = 0
CACHE_TTL_SECONDS = 300  # 5 分钟缓存

# 子表读取范围（列范围固定 A-Z，行范围根据子表动态调整）
DEFAULT_RANGE = "A1:Z1000"


# =============================================================================
# Basic Auth 保护
# =============================================================================
def check_auth(username, password):
    """验证用户名密码"""
    if not PROXY_AUTH_USER:
        return True
    return username == PROXY_AUTH_USER and password == PROXY_AUTH_PASS


def authenticate():
    """返回 401 响应"""
    return Response(
        json.dumps({"error": "需要认证", "hint": "请在请求头中携带 Basic Auth"}, ensure_ascii=False),
        401,
        {"WWW-Authenticate": 'Basic realm="Proxy Auth"', "Content-Type": "application/json; charset=utf-8"}
    )


def require_auth(f):
    """装饰器：要求 Basic Auth"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not PROXY_AUTH_USER:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


# =============================================================================
# 腾讯文档 API 调用
# =============================================================================
def get_api_headers():
    """获取 API 请求头（个人开发者版：Access-Token + Client-Id + Open-Id）"""
    return {
        "Access-Token": ACCESS_TOKEN,
        "Client-Id": CLIENT_ID,
        "Open-Id": OPEN_ID,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }


def api_call(method, url, **kwargs):
    """统一的 API 调用封装，处理 token 过期等错误"""
    try:
        resp = requests.request(method, url, headers=get_api_headers(), timeout=30, **kwargs)
        data = resp.json()
        
        # token 过期检测
        if resp.status_code == 401 or data.get("code") == 400006:
            return {"_token_expired": True, "original_response": data, "status_code": resp.status_code}
        
        return data
    except requests.exceptions.Timeout:
        return {"_error": "请求超时", "code": -1}
    except requests.exceptions.ConnectionError:
        return {"_error": "连接失败", "code": -1}
    except Exception as e:
        return {"_error": str(e), "code": -1}


def fetch_sheets_list():
    """
    获取文件下的子表列表
    API: GET /openapi/sheetbook/v2/{bookID}/sheets-info
    """
    url = f"{API_V2_BASE}/{FILE_ID}/sheets-info"
    data = api_call("GET", url)
    
    if data.get("_token_expired"):
        return {"error": "access_token 已过期", "token_expired": True}
    if data.get("_error"):
        return {"error": data["_error"]}
    
    # v2 接口返回结构不确定，尝试多种解析方式
    sheets = []
    if isinstance(data, dict):
        # 可能返回 { "code": 0, "data": { "sheets": [...] } }
        raw_sheets = data.get("data", {}).get("sheets", [])
        if not raw_sheets:
            raw_sheets = data.get("sheets", [])
        if not raw_sheets:
            raw_sheets = data.get("properties", [])  # v3 风格
        
        for sheet in raw_sheets:
            sheets.append({
                "sheet_id": str(sheet.get("sheetId", sheet.get("id", ""))),
                "title": sheet.get("title", sheet.get("name", "未知")),
                "row_count": sheet.get("rowTotal", sheet.get("rowCount", 1000)),
                "column_count": sheet.get("columnTotal", sheet.get("columnCount", 26))
            })
    
    return {"sheets": sheets}


def fetch_sheet_data_v3(sheet_id, max_rows=1000):
    """
    使用 v3 接口读取子表单元格数据
    API: GET /openapi/spreadsheet/v3/files/{fileId}/{sheetId}/{range}
    
    由于单次最多读取 1000 行，如果数据超过 1000 行，需要分页读取。
    这里先读取 A1:Z1000，如果表格更大，可以扩展分页逻辑。
    """
    # 构建 range，限制在 1000 行以内
    end_row = min(max_rows, 1000)
    range_str = f"A1:Z{end_row}"
    
    url = f"{API_V3_BASE}/files/{FILE_ID}/{sheet_id}/{range_str}"
    data = api_call("GET", url)
    
    if data.get("_token_expired"):
        return {"error": "access_token 已过期", "token_expired": True}
    if data.get("_error"):
        return {"error": data["_error"]}
    
    # 解析 gridData
    grid_data = data.get("data", {}).get("gridData", {})
    rows_data = grid_data.get("rows", [])
    
    # 将 gridData 解析为二维数组（和现有 data_cache.json 格式一致）
    rows = []
    for row in rows_data:
        row_values = []
        for cell in row.get("values", []):
            cell_value = cell.get("cellValue", {})
            if cell_value is None:
                row_values.append("")
                continue
            
            # 普通文本类型
            text = cell_value.get("text", "")
            if text is not None:
                row_values.append(text)
                continue
            
            # 其他类型（数字、日期等）
            # 腾讯文档可能返回 numberValue, stringValue 等
            number_val = cell_value.get("numberValue")
            if number_val is not None:
                row_values.append(number_val)
                continue
            
            string_val = cell_value.get("stringValue")
            if string_val is not None:
                row_values.append(string_val)
                continue
            
            row_values.append("")
        
        rows.append(row_values)
    
    return {"rows": rows}


def build_cache():
    """构建缓存数据（格式兼容 data_cache.json）"""
    global cache_data, cache_timestamp
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始拉取腾讯文档数据...")
    
    # 检查凭据
    if not all([ACCESS_TOKEN, CLIENT_ID, OPEN_ID]):
        missing = []
        if not ACCESS_TOKEN: missing.append("ACCESS_TOKEN")
        if not CLIENT_ID: missing.append("CLIENT_ID")
        if not OPEN_ID: missing.append("OPEN_ID")
        error_msg = f"缺少必要凭据: {', '.join(missing)}"
        print(f"[ERROR] {error_msg}")
        cache_data = {
            "data_source": "tencent_docs_api",
            "sheets": {},
            "summary": {"totalSheets": 0, "description": error_msg},
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": error_msg
        }
        cache_timestamp = time.time()
        return cache_data
    
    # 获取子表列表
    sheets_info = fetch_sheets_list()
    if sheets_info.get("error"):
        error_msg = sheets_info["error"]
        print(f"[ERROR] {error_msg}")
        cache_data = {
            "data_source": "tencent_docs_api",
            "sheets": {},
            "summary": {"totalSheets": 0, "description": error_msg},
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": error_msg,
            "token_expired": sheets_info.get("token_expired", False)
        }
        cache_timestamp = time.time()
        return cache_data
    
    sheets = sheets_info.get("sheets", [])
    if not sheets:
        print("[WARN] 未获取到子表列表")
        cache_data = {
            "data_source": "tencent_docs_api",
            "sheets": {},
            "summary": {"totalSheets": 0, "description": "未获取到子表列表"},
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        cache_timestamp = time.time()
        return cache_data
    
    # 拉取每个子表的数据
    sheets_data = {}
    for sheet in sheets:
        sid = sheet["sheet_id"]
        name = sheet["title"]
        max_rows = sheet.get("row_count", 1000)
        
        print(f"  拉取子表: {name} ({sid})...")
        result = fetch_sheet_data_v3(sid, max_rows)
        
        if result.get("error"):
            print(f"  ❌ {name}: {result['error']}")
            sheets_data[sid] = {
                "name": name,
                "rows": [],
                "error": result["error"],
                "token_expired": result.get("token_expired", False)
            }
        else:
            rows = result.get("rows", [])
            sheets_data[sid] = {
                "name": name,
                "rows": rows
            }
            print(f"  ✅ {name}: {len(rows)} 行")
    
    cache_data = {
        "data_source": "tencent_docs_api",
        "sheets": sheets_data,
        "summary": {
            "totalSheets": len(sheets_data),
            "description": "腾讯文档在线表格数据（云端代理实时拉取）"
        },
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    cache_timestamp = time.time()
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 数据拉取完成，共 {len(sheets_data)} 个子表")
    return cache_data


def get_cached_data():
    """获取缓存数据，过期则刷新"""
    global cache_data, cache_timestamp
    
    now = time.time()
    if cache_data is None or (now - cache_timestamp) > CACHE_TTL_SECONDS:
        return build_cache()
    return cache_data


# =============================================================================
# Flask 路由
# =============================================================================
@app.route("/", methods=["GET"])
def index():
    """根路径 - 服务信息"""
    return jsonify({
        "service": "质量数据分析系统 - 云端代理",
        "version": "2.1-personal",
        "data_source": "腾讯文档 Open API（个人开发者版）",
        "file_id": FILE_ID,
        "auth_mode": "个人开发者（Access-Token，无 Client Secret）",
        "token_expiry_hint": "Access Token 有效期约 30 天，过期后需重新授权",
        "endpoints": {
            "GET /api/status": "查询代理和缓存状态",
            "GET /api/data": "获取所有数据（多 sheet JSON）",
            "GET /api/sheets": "获取子表列表",
            "GET /api/refresh": "强制刷新缓存（重新拉取腾讯文档数据）"
        }
    })


@app.route("/api/status", methods=["GET"])
@require_auth
def api_status():
    """查询代理和缓存状态"""
    now = time.time()
    age = round(now - cache_timestamp, 1) if cache_timestamp else None
    is_valid = age is not None and age <= CACHE_TTL_SECONDS
    
    sheet_count = 0
    token_expired = False
    if cache_data and "sheets" in cache_data:
        sheet_count = len(cache_data["sheets"])
        for sid, sheet in cache_data["sheets"].items():
            if sheet.get("token_expired"):
                token_expired = True
                break
    
    missing_creds = []
    if not ACCESS_TOKEN: missing_creds.append("ACCESS_TOKEN")
    if not CLIENT_ID: missing_creds.append("CLIENT_ID")
    if not OPEN_ID: missing_creds.append("OPEN_ID")
    
    return jsonify({
        "configured": len(missing_creds) == 0,
        "missing_credentials": missing_creds,
        "cache_available": cache_data is not None,
        "cache_valid": is_valid,
        "cache_age_seconds": age,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "sheet_count": sheet_count,
        "data_source": "tencent_docs_api",
        "token_expired": token_expired,
        "updated_at": cache_data.get("updated_at") if cache_data else None,
        "error": cache_data.get("error") if cache_data else None
    })


@app.route("/api/data", methods=["GET"])
@require_auth
def api_data():
    """获取所有数据"""
    data = get_cached_data()
    return jsonify(data)


@app.route("/api/sheets", methods=["GET"])
@require_auth
def api_sheets():
    """获取子表列表"""
    data = get_cached_data()
    sheets = []
    
    if "sheets" in data:
        for sid, sheet in data["sheets"].items():
            sheets.append({
                "id": sid,
                "name": sheet.get("name", sid),
                "row_count": len(sheet.get("rows", [])),
                "error": sheet.get("error")
            })
    
    return jsonify({"sheets": sheets})


@app.route("/api/refresh", methods=["GET", "POST"])
@require_auth
def api_refresh():
    """强制刷新缓存"""
    global cache_data, cache_timestamp
    cache_data = None
    cache_timestamp = 0
    data = get_cached_data()
    
    token_expired = any(
        sheet.get("token_expired")
        for sheet in data.get("sheets", {}).values()
    )
    
    return jsonify({
        "success": not bool(data.get("error")),
        "message": data.get("error") or "缓存已刷新",
        "sheet_count": len(data.get("sheets", {})),
        "token_expired": token_expired,
        "updated_at": data.get("updated_at")
    })


@app.after_request
def after_request(response):
    """CORS 跨域支持"""
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
    response.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    return response


# =============================================================================
# 启动
# =============================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print("  质量数据分析系统 — 云端代理服务器（个人开发者版）")
    print("=" * 60)
    print(f"  监听地址: http://0.0.0.0:{port}")
    print(f"  数据来源: 腾讯文档 Open API")
    print(f"  文件 ID : {FILE_ID}")
    print(f"  缓存 TTL: {CACHE_TTL_SECONDS} 秒")
    print(f"  认证保护: {'已启用' if PROXY_AUTH_USER else '未启用'}")
    print("=" * 60)
    
    # 启动时预加载数据
    if ACCESS_TOKEN and CLIENT_ID and OPEN_ID:
        print("  启动预加载数据中...")
        build_cache()
    else:
        print("  ⚠️  缺少凭据，请在环境变量中配置")
        print("     需要: TENCENT_DOC_CLIENT_ID, TENCENT_DOC_ACCESS_TOKEN, TENCENT_DOC_OPEN_ID")
    
    print("=" * 60)
    
    app.run(host="0.0.0.0", port=port)
