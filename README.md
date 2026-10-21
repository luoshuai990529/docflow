# DocFlow

本地运行的问卷填写工具。上传问卷和资料后，系统提取题目、生成答案、核对证据与填写位置，并将通过验证的结果作为新文件交付；原始文件不会被改写。

## 功能与流程

- **依据资料填写**：根据上传的资料作答，核对引用和题目；证据不足的内容留待复核。
- **模型自主作答**：只需问卷，模型按明确的假设作答；报告标明生成内容和假设。
- **固定流程**：预检 → 提取 → 理解 → 作答 → 校验 → 写入 → 验证。模型只返回数据，程序负责检查和写入；未通过验证的文件不提供下载。
- **调试入口**：CLI 保留 `prepare` / `apply`，可分开检查提取结果和填写结果。

## 目录

| 目录 | 职责 |
| --- | --- |
| `web/` | Vue、Vite、Pinia 前端与 Nginx 同源 API 代理 |
| `server/` | FastAPI、应用用例、领域规则、workflow 节点和外部适配器 |
| `deploy/processor/` | 无网络的 Office 转换与 OCR 处理器 |
| `tests/` | 本地合成输入和假 HTTP 测试 |
| `docs/` | [架构](docs/architecture/architecture.md)、[API](docs/api/README.md)与[运维](docs/operations/operations.md)说明 |

## 快速启动

安装 Docker、Docker Compose 和 Make 后，在仓库根目录执行：

```sh
make start
```

命令会构建并启动服务，待服务就绪后显示本地访问地址（默认 <http://127.0.0.1:8765>）。打开页面，配置 DeepSeek 官方模型；如需自主作答的选项决策，再配置 Jev 官方服务。随后上传问卷和资料。模型调用会向所配置的服务发送相关内容，请先确认服务的数据处理规则。默认仅在本机提供访问。

停止服务：

```sh
make stop
```

详细的部署和本地开发命令见[部署说明](deploy/README.md)与[运维手册](docs/operations/operations.md)。
