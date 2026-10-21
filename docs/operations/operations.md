# 本地开发、运行与运维

本项目是单机本地工作台。Compose 默认只在 loopback 发布前端；后端只在私有 bridge 网络内提供 API；文档处理器没有网络。当前系统没有登录、租户隔离或远程服务级 SLA。

## 运行服务

在仓库根目录执行：

```sh
docker compose config
docker compose build
docker compose up -d
docker compose ps
```

打开 <http://127.0.0.1:8765>。Compose 健康状态应为 `web`、`server`、`processor` 均 healthy。停止服务：

```sh
docker compose stop
```

默认端口是 `127.0.0.1:8765`。需要更换本机端口时，设置 `AUTOFILL_WEB_PORT` 并把 `AUTOFILL_PUBLIC_ORIGIN` 改成对应本机 URL；运行参数可放在本地 `.env`，该文件不应提交。不要仅将 `AUTOFILL_WEB_BIND` 改为 `0.0.0.0` 后开放远程访问。远程部署必须另行提供认证、TLS 和网络访问控制。

## 开发与检查

Python **3.12.15**、Node **24.13.0** 和 uv **0.13.1** 与 CI/镜像版本对应。Python 依赖锁在 `server/uv.lock`；前端依赖锁在 `web/package-lock.json`，使用 Vue、Vite 和 Pinia。

```sh
uv sync --project server --locked --all-groups
uv run --project server --locked python -m autofill --help
uv run --project server --locked python scripts/test_tracked.py
npm ci --prefix web
npm test --prefix web
npm run build --prefix web
uv run --project server --locked python -m autofill.adapters.http.export_openapi --check
docker compose --env-file /dev/null config --quiet
docker compose --env-file /dev/null build
```

`scripts/test_tracked.py` 只加载 Git 索引中的 `tests/**/test_*.py`，以避开本机忽略的测试和客户资料。纳入索引的新测试必须先确认只访问临时合成输入或本地 fake HTTP。开发验证不读取旧 `inputs/`、`analysis/`、`output/`、`data/` 或 `secrets/` 目录，不调用付费模型 API。

## CLI prepare/apply 调试

保留命令入口 `python -m autofill`。一般文档路径使用 `--general`：

```sh
uv run --project server --locked python -m autofill prepare \
  --general --questionnaire /path/to/questionnaire.pdf \
  --source /path/to/source.pdf --out /tmp/workflow-prepare

uv run --project server --locked python -m autofill apply \
  --bundle /tmp/workflow-prepare --answers /path/to/answers.json \
  --mode real --out /tmp/workflow-result
```

`prepare` 生成可检查 bundle；`apply` 对输入和 bundle 重新核对哈希，校验答案文件后写入、回读并生成报告，不创建模型客户端。请为输出指定新的临时目录，并核对结果报告。模型 `run` 命令只有在明确执行时才会连接配置中的服务。

## 备份与恢复

运行状态使用三个新目录：

| 路径 | 内容 |
| --- | --- |
| `var/workflow/` | SQLite 状态及 WAL、上传/处理临时文件、处理器共享队列 |
| `var/secrets/` | SQLite 配置 Key 对应的 master key |
| `var/results/` | 最终文件、报告、诊断和可能被扣留的候选文件 |

执行一致性备份前先停掉所有服务，再复制三个目录：

```sh
docker compose stop
# 使用受控且加密的本地/备份介质复制 var/workflow、var/secrets、var/results
```

三目录都可能含客户文档或凭据材料，应限制访问并加密备份；数据库和 master key 必须一起保存、一起恢复。恢复时保持服务停止，将三目录恢复到原位置并保留容器所需的目录权限，然后启动并检查三个健康状态。不要把旧 `data/`、`secrets/`、`output/` 内容合并进来；新版本不会自动迁移这些目录。

`docker compose down` 会删除容器和网络，但不会删除这三个宿主机 bind mount 目录。不要对含有唯一结果或密钥的目录执行清理操作。

## 诊断顺序

1. 看 `docker compose ps`，确认三个服务健康；访问首页和 `/api/v1/session` 检查前端代理与 API 会话。
2. 若前端端口已占用，换 `AUTOFILL_WEB_PORT` 并同步 `AUTOFILL_PUBLIC_ORIGIN`，重新执行 `docker compose up -d`。
3. 若 API 返回 `FORBIDDEN`，核对浏览器 origin、Compose 中的 `AUTOFILL_PUBLIC_ORIGIN` 与可信前端 IP；不要通过放宽到任意代理来规避。
4. 若 DOC/XLS 转换或 OCR 失败，检查 `processor` 健康状态和队列挂载 `var/workflow/processor-jobs`；该容器没有网络，不能从处理器内访问模型服务。
5. 若模型调用失败，检查界面中配置的服务地址、模型协议及 Key，并查看任务安全错误码。配置 API 不回显 Key；修改服务地址后可能需要重新录入 Key。
6. 若结果文件未提供下载，阅读报告中的逐题状态与版式/内容门禁；验证失败的候选只供诊断，不会出现在下载清单。

服务重启时未完成的任务会标成中断，不会自动重放模型请求。用户需重新选择文件并显式提交新任务。查看运行输出时不要将客户文档或 API Key 粘贴进 issue、聊天或 CI 日志。
