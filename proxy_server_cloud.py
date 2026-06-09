#!/usr/bin/env python3
"""
云端代理服务 — 通过腾讯文档 MCP HTTP API 实时拉取数据
适配 Railway / Render 等云平台部署

版本: v3.2 (MCP HTTP JSON-RPC 版)
更新日期: 2026-06-09
关键修复:
  - CORS 跨域支持简化，避免启动崩溃
  - 字段名统一为 rows，兼容前端 HTML
  - 硬编码子表列表，绕过 list_sheets 工具不稳定问题
  - 401 响应添加 WWW-Authenticate 头，支持浏览器弹窗认证
  - /api/data 返回格式扁平化，顶层直接包含 sheets，兼容前端解析
"""

from flask import Flask, jsonify, request
from functools import wraps
import os
import json
import requests
import time
from datetime import datetime

app = Flask(__name__)

# ============================================================
# CORS 跨域支持
# ============================================================
def add_cors_headers(response):
    """为响应添加 CORS 头"""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# ============================================================
# 配置（从环境变量读取）
# ============================================================
MCP_URL = "https://docs.qq.com/openapi/mcp"
FILE_ID = os.environ.get("TENCENT_DOC_FILE_ID", "DVHNUbFJWSk5tbE93")
MCP_TOKEN = os.environ.get("TENCENT_DOC_MCP_TOKEN", "")

# 代理认证（Basic Auth）
PROXY_AUTH_USER = os.environ.get("PROXY_AUTH_USER", "")
PROXY_AUTH_PASS = os.environ.get("PROXY_AUTH_PASS", "")

# 缓存
DATA_CACHE = None
CACHE_TIMESTAMP = None
CACHE_TTL_SECONDS = 300  # 5分钟缓存

# ============================================================
# 腾讯文档 MCP HTTP 调用封装
# ============================================================

def mcp_call(method, params=None, req_id=1):
    """发送 MCP JSON-RPC HTTP 请求"""
    if not MCP_TOKEN:
        return {"_error": "MCP Token 未配置", "code": -1}

    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method
    }
    if params:
        payload["params"] = params

    headers = {
        "Content-Type": "application/json",
        "Authorization": MCP_TOKEN
    }

    try:
        resp = requests.post(MCP_URL, headers=headers, json=payload, timeout=30)
        data = resp.json()

        if "error" in data:
            return {"_error": data["error"].get("message", "MCP 调用失败"),
                    "code": data["error"].get("code", -1)}

        return data.get("result", {})
    except requests.exceptions.Timeout:
        return {"_error": "MCP 请求超时", "code": -1}
    except requests.exceptions.ConnectionError:
        return {"_error": "MCP 连接失败", "code": -1}
    except Exception as e:
        return {"_error": str(e), "code": -1}


def mcp_call_tool(tool_name, arguments, req_id=1):
    """调用 MCP 工具"""
    return mcp_call("tools/call", {
        "name": tool_name,
        "arguments": arguments
    }, req_id)


def parse_cell_data(result):
    """解析 sheet.get_cell_data 返回的单元格数据为二维数组"""
    if not result or "_error" in result:
        return None

    content = result.get("content", [])
    if not content:
        return None

    # 找到 text 类型的内容
    text_content = None
    for item in content:
        if item.get("type") == "text":
            text_content = item.get("text", "")
            break

    if not text_content:
        return None

    try:
        data = json.loads(text_content)
        cells = data.get("cells", [])

        # 找到最大行列
        max_row = max(c["row"] for c in cells) if cells else 0
        max_col = max(c["col"] for c in cells) if cells else 0

        # 构建二维数组
        grid = [["" for _ in range(max_col + 1)] for _ in range(max_row + 1)]
        for cell in cells:
            r, c = cell["row"], cell["col"]
            vt = cell.get("value_type", "STRING")
            if vt == "STRING":
                grid[r][c] = cell.get("string_value", "")
            elif vt == "NUMBER":
                grid[r][c] = cell.get("number_value", 0)
            elif vt == "BOOL":
                grid[r][c] = cell.get("bool_value", False)
            else:
                grid[r][c] = cell.get("string_value", "")

        return grid
    except Exception as e:
        print(f"[parse_cell_data] 解析失败: {e}")
        return None


# ============================================================
# Basic Auth 认证
# ============================================================

def check_auth(username, password):
    if not PROXY_AUTH_USER or not PROXY_AUTH_PASS:
        return True  # 未配置认证，允许所有请求
    return username == PROXY_AUTH_USER and password == PROXY_AUTH_PASS


def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not PROXY_AUTH_USER or not PROXY_AUTH_PASS:
            return f(*args, **kwargs)
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            resp = jsonify({"error": "认证失败，请检查账号密码"})
            resp.status_code = 401
            resp.headers['WWW-Authenticate'] = 'Basic realm="Quality Analysis Proxy"'
            return add_cors_headers(resp)
        return f(*args, **kwargs)
    return decorated


# ============================================================
# 数据拉取逻辑
# ============================================================

def get_sheet_list():
    """获取子表列表 — 使用已知子表 ID（list_sheets 工具可能不稳定）"""
    return [
        {"id": "000001", "name": "产量汇总"},
        {"id": "000002", "name": "电气不良"},
        {"id": "000003", "name": "PCB不良"},
        {"id": "000005", "name": "机装组"},
        {"id": "000006", "name": "PCB组"},
        {"id": "000007", "name": "参照表"},
    ]


def get_sheet_data(sheet_id, start_row=1, end_row=100, start_col=1, end_col=20):
    """读取指定子表的数据"""
    result = mcp_call_tool("sheet.get_cell_data", {
        "file_id": FILE_ID,
        "sheet_id": sheet_id,
        "start_row": start_row,
        "end_row": end_row,
        "start_col": start_col,
        "end_col": end_col
    }, req_id=20)

    return parse_cell_data(result)


def fetch_all_data():
    """拉取所有子表数据，返回标准格式的缓存数据"""
    global DATA_CACHE, CACHE_TIMESTAMP

    print(f"[{datetime.now()}] 开始拉取腾讯文档数据...")

    if not MCP_TOKEN:
        return {"error": "MCP Token 未配置", "configured": False}

    # 1. 获取子表列表
    sheets = get_sheet_list()
    if not sheets:
        return {"error": "无法获取子表列表", "configured": True}

    print(f"  发现 {len(sheets)} 个子表")

    # 2. 读取每个子表数据
    sheets_data = {}
    for sheet in sheets:
        sid = sheet.get("id", "")
        sname = sheet.get("name", "")
        print(f"  读取子表: {sname} ({sid})")

        data = get_sheet_data(sid)
        if data:
            sheets_data[sid] = {
                "name": sname,
                "rows": data
            }
        else:
            sheets_data[sid] = {
                "name": sname,
                "rows": [],
                "error": "读取失败"
            }

    # 3. 构建标准缓存格式（兼容现有 HTML）
    cache = {
        "file_id": FILE_ID,
        "sheet_count": len(sheets),
        "sheets": sheets_data,
        "updated_at": datetime.now().isoformat(),
        "source": "tencent_docs_mcp"
    }

    DATA_CACHE = cache
    CACHE_TIMESTAMP = time.time()

    print(f"[{datetime.now()}] 数据拉取完成，{len(sheets)} 个子表")
    return cache


# ============================================================
# Flask 路由
# ============================================================

@app.route("/", methods=["GET", "OPTIONS"])
def index():
    if request.method == 'OPTIONS':
        return add_cors_headers(jsonify({}))
    return add_cors_headers(jsonify({
        "service": "质量数据分析云端代理",
        "version": "3.0-mcp-http",
        "status": "running",
        "endpoints": ["/api/status", "/api/data", "/api/refresh"]
    }))


@app.route("/api/status", methods=["GET", "OPTIONS"])
@auth_required
def api_status():
    """查询代理和缓存状态"""
    if request.method == 'OPTIONS':
        return add_cors_headers(jsonify({}))

    if not MCP_TOKEN:
        return add_cors_headers(jsonify({
            "configured": False,
            "error": "MCP Token 未配置",
            "missing_credentials": ["MCP_TOKEN"]
        }))

    # 简单验证 token 有效性（调用 tools/list）
    token_valid = False
    try:
        result = mcp_call("tools/list", req_id=99)
        token_valid = "_error" not in result and "tools" in result
    except:
        pass

    cache_age = None
    if CACHE_TIMESTAMP:
        cache_age = int(time.time() - CACHE_TIMESTAMP)

    return add_cors_headers(jsonify({
        "configured": True,
        "token_valid": token_valid,
        "cache_available": DATA_CACHE is not None,
        "cache_age_seconds": cache_age,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "sheet_count": DATA_CACHE.get("sheet_count", 0) if DATA_CACHE else 0,
        "updated_at": DATA_CACHE.get("updated_at") if DATA_CACHE else None,
        "data_source": "tencent_docs_mcp"
    }))


@app.route("/api/data", methods=["GET", "OPTIONS"])
@auth_required
def api_data():
    """获取缓存数据（带缓存刷新逻辑）"""
    if request.method == 'OPTIONS':
        return add_cors_headers(jsonify({}))

    global DATA_CACHE, CACHE_TIMESTAMP

    # 检查缓存是否过期
    cache_expired = True
    if CACHE_TIMESTAMP and (time.time() - CACHE_TIMESTAMP) < CACHE_TTL_SECONDS:
        cache_expired = False

    # 如果缓存过期或无缓存，自动刷新
    if cache_expired or not DATA_CACHE:
        result = fetch_all_data()
        if "error" in result:
            resp = jsonify({
                "success": False,
                "error": result["error"],
                "configured": result.get("configured", False)
            })
            resp.status_code = 500
            return add_cors_headers(resp)

    # 合并缓存数据和元信息到顶层，兼容前端期望格式 (respData.sheets)
    response_payload = {"success": True}
    if DATA_CACHE:
        response_payload.update(DATA_CACHE)
    response_payload["cache_age_seconds"] = int(time.time() - CACHE_TIMESTAMP) if CACHE_TIMESTAMP else None
    return add_cors_headers(jsonify(response_payload))


@app.route("/api/refresh", methods=["POST", "OPTIONS"])
@auth_required
def api_refresh():
    """强制刷新数据"""
    if request.method == 'OPTIONS':
        return add_cors_headers(jsonify({}))

    result = fetch_all_data()
    if "error" in result:
        resp = jsonify({
            "success": False,
            "error": result["error"]
        })
        resp.status_code = 500
        return add_cors_headers(resp)

    return add_cors_headers(jsonify({
        "success": True,
        "message": f"数据已刷新，共 {result.get('sheet_count', 0)} 个子表",
        "updated_at": result.get("updated_at")
    }))


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"=" * 60)
    print(f"质量数据分析云端代理 v3.2")
    print(f"MCP HTTP 模式")
    print(f"=" * 60)
    print(f"端口: {port}")
    print(f"文件ID: {FILE_ID}")
    print(f"MCP Token: {'已配置' if MCP_TOKEN else '未配置'}")
    print(f"认证: {'已启用' if PROXY_AUTH_USER else '未启用'}")
    print(f"=" * 60)
    app.run(host="0.0.0.0", port=port)
