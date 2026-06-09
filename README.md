# 质量数据分析系统 — 云端代理（个人开发者版）

## 特点
- **无需 Client Secret**：个人开发者没有 Client Secret 也能使用
- **使用 Access Token + Client ID + Open ID 调用腾讯文档 Open API**
- Access Token 有效期约 30 天，过期后需手动重新授权

## 环境变量（Railway 控制台设置）

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `TENCENT_DOC_CLIENT_ID` | ✅ | 腾讯文档应用 Client ID |
| `TENCENT_DOC_ACCESS_TOKEN` | ✅ | 用户授权后的 Access Token |
| `TENCENT_DOC_OPEN_ID` | ✅ | 用户 Open ID（在开发者控制台获取） |
| `TENCENT_DOC_FILE_ID` | ❌ | 腾讯文档文件 ID，默认 `DVHNUbFJWSk5tbE93` |
| `PROXY_AUTH_USER` | ❌ | 代理访问用户名（建议设置，防止未授权访问） |
| `PROXY_AUTH_PASS` | ❌ | 代理访问密码 |

## 部署步骤

### 1. 获取 Open ID
在腾讯文档开发者控制台 → 应用管理页面，找到并复制 **Open ID**。

### 2. 创建 GitHub 仓库
1. 打开 https://github.com/new
2. 仓库名填 `quality-proxy`
3. 设为 Public
4. 上传本目录所有文件

### 3. Railway 部署
1. 打开 https://railway.app → New Project → Deploy from GitHub repo
2. 选择 `quality-proxy` 仓库
3. 进入项目 → Variables → 添加环境变量（见上表）
4. 等待自动部署完成

### 4. 获取域名
部署完成后，Railway 会分配一个域名，如：
```
https://quality-proxy.up.railway.app
```

### 5. 修改前端 HTML
把 `质量分析管理系统.html` 中的代理地址改为 Railway 域名：
```javascript
const PROXY_BASE = 'https://quality-proxy.up.railway.app';
const PROXY_AUTH = { user: '你设置的用户名', pass: '你设置的密码' };
```

## API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `GET /api/status` | GET | 查询代理状态 |
| `GET /api/data` | GET | 获取所有子表数据 |
| `GET /api/sheets` | GET | 获取子表列表 |
| `GET /api/refresh` | GET | 强制刷新缓存 |

## Access Token 过期处理

当 Access Token 过期（约 30 天）时：
1. 登录腾讯文档开发者控制台
2. 重新发起授权 → 扫码登录
3. 获取新的 Access Token
4. 更新 Railway 环境变量中的 `TENCENT_DOC_ACCESS_TOKEN`
5. Railway 会自动重新部署

## 技术架构

```
浏览器（任何电脑）
   ↓ 请求
Railway 云端代理（Python Flask）
   ↓ 携带 Access-Token + Client-Id + Open-Id
腾讯文档 Open API
   ↓ 返回表格数据
Railway 缓存 → 返回给浏览器
```
