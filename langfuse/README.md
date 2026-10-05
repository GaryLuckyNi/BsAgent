# 本地 Langfuse 平台

本目录使用 Docker Compose 部署 Langfuse 4.16.0，包含 Web、Worker、PostgreSQL、ClickHouse、Redis 和 MinIO。数据保存在独立 Docker volumes 中；Web 与 MinIO 的宿主机端口只绑定到回环地址。

## 启动

在仓库根目录执行：

```bash
python3 langfuse/init.py
docker compose --project-directory langfuse -f langfuse/compose.yaml up -d
docker compose --project-directory langfuse -f langfuse/compose.yaml ps
```

- 网页：http://localhost:3000
- 本机账号：`admin@bsagent.local`
- 密码：本目录 `.env` 中的 `LANGFUSE_INIT_USER_PASSWORD`，请在本地编辑器查看。
- 初始组织：`BsAgent Local`；项目：`BsAgent Content Local`。
- 项目 public/secret key：`.env` 中的 `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` 和 `LANGFUSE_INIT_PROJECT_SECRET_KEY`。

Web 与 Worker 镜像均固定为 `4.16.0`。Web 映射到 `127.0.0.1:3000`，MinIO 映射到 `127.0.0.1:9090`，供本机浏览器访问媒体。PostgreSQL、ClickHouse 和 Redis 不映射宿主机端口。Web 和 Worker 的 Langfuse 遥测均已关闭。

`init.py` 会为缺失配置生成随机凭证，保留已有变量，通过原子写入更新 `.env` 并设置权限为 `600`。重复执行不会覆盖已有配置，也不会输出密钥。`.env` 已加入忽略规则，不要提交或分享其内容。初始化变量只用于新实例引导；平台已有数据后，修改这些变量不会更改现有账号密码。

## 内容观测验收

```bash
.venv/bin/python langfuse/smoke.py
```

需要仓库 Python 环境安装 `langfuse` 和 `httpx`。验收脚本使用本目录项目凭证，向本地平台发送合成 trace，再回读检查 input/output 及父子关系。结果写入 `smoke-result.json`，不保存密钥。

## 日常维护

```bash
docker compose --project-directory langfuse -f langfuse/compose.yaml ps
docker compose --project-directory langfuse -f langfuse/compose.yaml down
docker compose --project-directory langfuse -f langfuse/compose.yaml up -d
```

`down` 会保留数据卷；不要随意使用 `down -v`，它会删除平台数据。备份需同时保存 PostgreSQL、ClickHouse、MinIO 数据及 `.env` 中的加密密钥。此部署面向本机开发，不提供高可用和自动备份。
