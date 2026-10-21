# Compose 部署

Compose 从三个独立构建上下文生成镜像：

| 服务 | 构建上下文 | 运行职责 | 网络/端口 |
| --- | --- | --- | --- |
| `web` | `web/` | Vue/Vite 构建的页面和 Nginx 同源 `/api/v1/` 反向代理 | 默认仅发布 `127.0.0.1:8765`，加入私有 bridge |
| `server` | `server/` | FastAPI、应用用例、workflow runner、CLI 包及 SQLite/model/document adapters | 只加入私有 bridge，不发布宿主机端口 |
| `processor` | `deploy/processor/`，另接受受限 `server/` named context | LibreOffice 转换和 Tesseract OCR | `network_mode: none`，通过 `var/workflow/processor-jobs` 收发任务 |

运行前检查与启动：

```sh
docker compose --env-file /dev/null config
docker compose --env-file /dev/null build
docker compose --env-file /dev/null up -d
docker compose ps
```

`--env-file /dev/null` 使用 Compose 文件里的安全本机默认值；若要覆盖端口或 Origin，请改用本地环境配置并同步设置 `AUTOFILL_WEB_PORT` 和 `AUTOFILL_PUBLIC_ORIGIN`。不要把带 Key 的环境配置提交到仓库。模型 Key 在应用页面设置并保存在本机加密状态中。

容器均采用只读根文件系统、降权和资源限制；前端和后端共用私有网络，处理器不联网。默认部署用于单机本地工作台，没有认证、TLS 或租户隔离，不能直接作为公网服务开放。

运行数据只挂载到 `var/workflow/`、`var/secrets/`、`var/results/`。停止服务后备份这三个目录，数据库与 master key 应一起保存和恢复。详见[运维手册](../docs/operations/operations.md)。旧 `data/`、`secrets/`、`output/` 不会被读取、迁移或清理。

基础镜像使用固定标签。更新镜像、Python 依赖或 OCR/Office 软件时，应单独审查变更并运行本地合成检查与镜像构建。
