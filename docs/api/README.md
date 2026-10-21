# `/api/v1` 使用说明

当前 API 供本机浏览器界面使用，API 合同以 [OpenAPI 文件](openapi.yaml) 为准。Compose 默认只把 `127.0.0.1:8765` 发布到宿主机；后端端口不直接映射。页面通过 Nginx 同源访问 API。

## 请求约定

- 根路径为 `http://127.0.0.1:8765/api/v1`。错误响应使用 `ApiError`，通常包含稳定错误码和面向用户的说明。
- 先 `GET /session` 取得临时 `csrf_token`。所有写请求带 `X-CSRF-Token`；浏览器还应保留同源 `Origin` 和 `Host`。
- 配置读接口只返回公开元数据、`has_key` 和固定掩码，不回显 API Key。写入 Key 只用于保存或当前任务，后端把持久化 Key 加密后写入本机 SQLite。
- 上传走 `PUT /uploads?name=<文件名>&role=questionnaire|source`，请求体为 `application/octet-stream`；收到 token 后，`POST /jobs` 提交问卷 token、资料 token 列表、模式、配置 ID 和 request ID。
- 写请求的 `request_id` 用于任务幂等；相同 ID 和相同请求返回已有任务，内容不一致会冲突。提交成功后通过 `GET /jobs/{id}` 轮询节点与任务状态。
- 取消使用 `POST /jobs/{id}/cancel`。报告由 `/jobs/{id}/report.json|md|html` 读取，已交付文件通过 `/jobs/{id}/download/{filename}` 下载。

最小的本机启动检查：

```sh
curl --fail http://127.0.0.1:8765/api/v1/session
```

该请求只建立会话，不会上传文件或调用模型。实际配置、上传、任务、取消、报告与下载请求的字段和响应 schema 请直接查 OpenAPI；不要把生产 Key 写入命令历史、脚本、测试报告或 issue。

## 安全边界

API 会验证实际 peer 是否属于配置的可信代理网段、`Host` 是否对应预期 origin，以及写请求的 CSRF token。Nginx 覆盖 forwarded client address，不把用户提交的任意转发头当作信任依据。API 不启用通配 CORS，也不把后端端口公开给宿主机。

当前系统没有用户登录、租户隔离或角色授权，不是远程多用户 API。不要仅把 `AUTOFILL_WEB_BIND` 改成 `0.0.0.0` 就暴露到公网；远程使用前必须由部署方提供认证、TLS、网络访问控制和相应的威胁审查。

## 更新 API 合同

FastAPI 路由和 Pydantic schema 是生成源。修改接口后运行：

```sh
uv run --project server --locked python -m autofill.adapters.http.export_openapi
uv run --project server --locked python -m autofill.adapters.http.export_openapi --check
```

第一条更新本文件旁的 `openapi.yaml`，第二条只核对工作树合同是否与代码一致。CI 使用 `--check`，不会改写 API 文档。
