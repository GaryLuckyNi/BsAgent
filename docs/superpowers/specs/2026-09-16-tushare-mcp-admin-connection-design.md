# Tushare MCP 数据源接入设计

## 背景

当前启动脚本把 `TUSHARE_TOKEN` 拼接成 Tushare 官方 MCP URL，并写入
`BS_AGENT_MCP_SERVER_URL`：

```text
TUSHARE_TOKEN
  -> https://api.tushare.pro/mcp/token=<token>
  -> BS_AGENT_MCP_SERVER_URL
```

这条链路混淆了两类不同配置：

- `BS_AGENT_MCP_SERVER_URL` 是 Agent 可选连接的通用 MCP Server；
- Tushare MCP 是 Admin 管理的市场数据源，属于业务数据源配置。

仓库内的本地 MCP 服务是学习和单独调试示例，不属于默认运行链路。Agent 本身已经具备 MCP Client 能力，因此无需为了接入 Tushare 再启动自建 MCP Server。

## 目标

1. 由 Admin 管理并持久化完整的 Tushare MCP URL。
2. Agent 从 Admin 获取当前激活的数据源连接，直接发现和调用 Tushare MCP 工具。
3. 保留 `BS_AGENT_MCP_SERVER_URL` 作为独立、可选的通用 MCP 连接，默认不配置。
4. 删除 `TUSHARE_TOKEN -> BS_AGENT_MCP_SERVER_URL` 的隐式转换。
5. Tushare MCP 不可用时，仅移除对应工具，不阻断平台工具和 Web Search。
6. Admin 切换数据源后，Agent 能刷新工具目录而无需重启。

## 非目标

- 不让 Admin 代理 MCP 工具调用。
- 不把仓库内的学习示例 MCP Server 加回默认启动链路。
- 不在本次改造中实现通用 Secret Manager 或数据库字段加密。
- 不改变 Planner、Tool Selector 和工具执行协议。
- 不继续维护旧的 Tushare REST Adapter 作为 Agent 主链路。

## 方案选择

采用“Admin 管理配置，Agent 直连 MCP”的方式：

```text
Admin UI
  -> Admin API
  -> market_data_provider_configs.settings.mcp_server_url

Agent Tool Catalog
  -> Admin 内部连接接口
  -> Tushare 官方 MCP
  -> discover tools / call tool

可选通用 MCP
  -> BS_AGENT_MCP_SERVER_URL
  -> discover tools / call tool
```

不采用以下方案：

- 公开数据源接口直接返回完整 URL：URL 中包含密钥，会暴露给浏览器。
- Admin 代理 MCP 调用：会增加一次网络跳转，并让 Admin 承担不必要的数据面职责。

## 配置模型

### Tushare 数据源配置

`MarketDataProviderConfigRecord.settings` 继续使用 JSON，不需要数据库迁移。Tushare 的有效配置为：

```json
{
  "mcp_server_url": "https://api.tushare.pro/mcp/token=<完整密钥>"
}
```

校验规则：

- `mcp_server_url` 必须是非空的 `http` 或 `https` URL；
- 激活的数据源必须存在有效的 `mcp_server_url`；
- URL 由管理员完整输入，Admin 和 Agent 都不拼接 token；
- `provider=tushare` 决定服务类型和默认连接名 `tushare`。

旧字段 `token_env_var` 和 `enabled_api_names` 属于仓库内本地 Tushare REST/MCP 适配层。它们不再是 Admin Tushare 数据源的必填项，也不参与 Agent 的官方 MCP 连接。已有记录在读取时保留未知字段，以便平滑迁移；管理员保存后以 `mcp_server_url` 为准。

### 通用 Agent MCP 配置

以下环境变量继续保留：

```text
BS_AGENT_MCP_SERVER_URL=
BS_AGENT_MCP_SERVER_NAME=
BS_AGENT_MCP_SERVER_REVISION=
BS_AGENT_MCP_CONFIG_REVISION=
```

默认 URL 为空。只有显式设置 `BS_AGENT_MCP_SERVER_URL` 时，Agent 才发现该服务的工具。它不再使用 `tushare` 作为默认名称，也不读取 `TUSHARE_TOKEN`。

### Admin 地址

Agent 使用独立的 `BS_AGENT_ADMIN_BASE_URL` 访问 Admin 运行时配置。默认值与本地 Admin Backend 地址保持一致。`skill_registry_base_url` 继续只表达 Skill Registry 的用途，避免再次混用配置语义。

## Admin API 契约

### 公开 CRUD 接口

现有列表、详情、创建、更新、激活和删除接口保持路径不变。公开响应中的 Tushare MCP URL 必须脱敏：

```json
{
  "settings": {
    "mcp_server_url": "https://api.tushare.pro/mcp/token=******"
  }
}
```

前端编辑规则：

- 新建时必须输入完整 URL；
- 编辑时 URL 输入框为空表示保留原值；
- 输入新 URL 表示替换原值；
- 脱敏值不能作为更新值写回数据库；
- 页面不在表格、错误提示或浏览器日志中展示完整 URL。

### Agent 内部读取接口

新增内部接口：

```text
GET /api/internal/market-data-providers/active/mcp-connection
```

成功响应：

```json
{
  "provider_id": "tushare-primary",
  "provider": "tushare",
  "server_name": "tushare",
  "server_url": "https://api.tushare.pro/mcp/token=<完整密钥>",
  "config_revision": "<由 id 和 updated_at 派生的稳定版本>"
}
```

状态语义：

- `200`：存在有效且激活的数据源；
- `404`：没有激活的数据源；
- `409`：激活记录存在，但配置无效。

该接口只属于服务间控制面。当前项目尚未建立 Admin 服务认证，因此本地环境由服务边界隔离；生产部署必须只在内部网络暴露 Admin Backend。完整 URL 不写入访问日志、异常信息或追踪属性。

## Agent 运行时

### 数据源配置客户端

新增一个职责单一的 Admin 市场数据源客户端，负责：

1. 请求内部连接接口；
2. 将 `200` 响应解析为 `McpServerConfig` 所需字段；
3. 将 `404` 解释为“未配置 Tushare”，而不是系统异常；
4. 将超时、网络失败、`409` 和非法响应记录为脱敏诊断；
5. 永远不记录 `server_url`。

### MCP 连接集合

工具目录每次构建时按以下顺序生成连接集合：

1. 从 Admin 获取激活的 Tushare MCP；
2. 若 `BS_AGENT_MCP_SERVER_URL` 非空，加入通用 Agent MCP；
3. 对每个连接独立执行 `list_tools`；
4. 合并成功发现的工具；
5. 最后注册 `rag_search`、`web_search` 和 `search_tool` 等平台工具。

一个 MCP 连接失败不能跳过其他连接。不同连接返回同名工具时，保留先注册的工具，跳过冲突项并记录服务名和工具名；日志不包含服务 URL。

### 工具目录刷新

当前 `ToolCatalogRuntime` 只在首次访问时建立快照。本次增加有界刷新能力：

- 新增 `market_data_provider_refresh_seconds`，默认 30 秒；
- 快照未过期时直接复用；
- 过期后的首个读取者构建新目录，其余读取者继续使用旧快照；
- 新目录构建成功后原子替换快照和 Tool Search Index；
- 刷新失败时保留上一个可用快照，并记录脱敏错误；
- 测试可显式清空或强制刷新目录。

这样 Admin 激活或修改数据源后，最长一个刷新周期即可生效，研究请求不因刷新失败失去已有工具。

## 启动与部署

### 本地启动

`scripts/dev.mjs` 调整为：

- 不读取 `TUSHARE_TOKEN`；
- 不生成 Tushare MCP URL；
- 不要求 `TUSHARE_TOKEN` 或 `BS_AGENT_MCP_SERVER_URL` 才能启动；
- 默认不设置 `BS_AGENT_MCP_SERVER_URL`；
- 向 Agent 注入 Admin Backend 地址；
- `npm run dev:mcp` 仍可单独启动本地学习示例。

### 生产部署

`compose.production.yml` 不再将 `TUSHARE_TOKEN` 拼接到 `BS_AGENT_MCP_SERVER_URL`。通用 MCP 变量改为可选空值，Tushare 完整 URL 由管理员通过 Admin 配置。

README、部署文档和环境变量示例需要明确：

- Tushare MCP 在 Admin 的“市场数据源”页面配置；
- `BS_AGENT_MCP_SERVER_URL` 只用于额外的通用 MCP Server；
- Admin Backend 必须位于内部网络，因为 Agent 内部接口返回完整连接 URL。

## 错误与降级

| 场景 | 行为 |
| --- | --- |
| Admin 无激活数据源 | 不注册 Tushare 工具，Web Search 可继续工作 |
| Admin 不可达 | 保留旧工具目录；首次启动时仅使用其他可用工具 |
| Tushare `list_tools` 返回 401 | 不注册 Tushare 工具，记录 `status=401` 和服务名 |
| 通用 Agent MCP 失败 | 不影响 Tushare 和平台工具 |
| 两个 MCP 返回同名工具 | 保留先注册项，记录冲突诊断 |
| 配置刷新失败 | 继续使用最近一次成功快照 |

用户可见错误需要区分“未配置市场数据源”“Tushare 鉴权失败”和“当前候选工具未生成有效调用”，避免把连接故障误报为模型决策失败。

## 测试策略

### Admin Backend

- Tushare 配置只要求有效的 `mcp_server_url`；
- 拒绝空值、非 HTTP URL 和脱敏占位值；
- 公开列表和详情响应只返回脱敏 URL；
- 空 URL 更新保留原值，新 URL 更新替换原值；
- 内部连接接口返回完整 URL 和稳定 revision；
- 无激活配置返回 404，无效激活配置返回 409。

### Admin Frontend

- 新建表单要求 MCP URL；
- 编辑表单不回填完整 URL；
- 空值提交保留原连接；
- 表格只展示脱敏地址；
- 不再要求 `token_env_var` 和 `enabled_api_names`。

### Agent Backend

- Admin 客户端正确处理 200、404、409、超时和非法响应；
- Tushare 与通用 MCP 可以同时发现工具；
- 任一 MCP 失败不会阻断其他工具；
- 未配置通用 MCP 时不会创建空连接；
- 工具冲突处理稳定且不泄露 URL；
- 目录过期后刷新，失败时继续提供旧快照；
- Tushare 401 在诊断中可识别。

### 启动与部署

- 未设置 `TUSHARE_TOKEN` 和 `BS_AGENT_MCP_SERVER_URL` 时可以启动；
- 启动脚本不再拼接 Tushare URL；
- Compose 不再声明 Tushare URL 映射；
- 文档与运行时配置保持一致。

## 迁移

1. 部署 Admin 对 `mcp_server_url` 的支持和内部读取接口。
2. 在 Admin 中为 Tushare 数据源填写完整官方 MCP URL并激活。
3. 部署 Agent 的 Admin 配置客户端和多 MCP 工具目录。
4. 删除运行环境中为 Tushare 设置的 `BS_AGENT_MCP_SERVER_URL`。
5. 删除不再使用的 `TUSHARE_TOKEN` 启动要求。

迁移期间，如果 Admin 尚未配置 Tushare，Agent 会自动降级到 Web Search，不会尝试从旧环境变量推断或拼接 Tushare 地址。

## 验收标准

- Admin 可以创建、编辑并激活带完整 MCP URL 的 Tushare 数据源。
- Admin 页面和公开 API 不泄露 URL 中的密钥。
- Agent 能从 Admin 获取连接并发现 Tushare 工具。
- `BS_AGENT_MCP_SERVER_URL` 为空时不影响 Tushare；非空时作为独立连接工作。
- 启动脚本和生产 Compose 不再把 Tushare 配置映射到通用 MCP 变量。
- 修改 Admin 数据源后无需重启 Agent 即可更新工具目录。
- Tushare 401 等故障能被准确诊断，同时 Web Search 保持可用。
