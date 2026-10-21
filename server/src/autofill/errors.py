"""Public workflow errors without provider responses or credential material."""

from .application.ports import LLMFailure
from .domain.errors import normalize_error_code


ERRORS = {
    'UNSUPPORTED_PREEXISTING_CHECKBOX': '原问卷已有勾选且与答案冲突，已停止填写以保留原标记。',
    'UNSUPPORTED_QUESTIONNAIRE': '未能可靠识别填写位置，请核对文件或使用可编辑问卷。',
    'DOC_LAYOUT_CHANGED': '旧 DOC 转换后的版式与已核对映射不一致，已停止填写。',
    'DOC_CONVERTER_REQUIRED': '旧 DOC 需要转换组件，请使用 Docker 服务。',
    'LLM_AUTH': '模型服务鉴权失败，请检查保存的 API Key。',
    'LLM_TIMEOUT': '模型调用超时，请稍后重新提交。',
    'LLM_RATE_LIMIT': '模型服务限流，请稍后重新提交。',
    'LLM_BAD_JSON': '模型回复的格式无法读取，本次未生成填写文件。请查看诊断报告；这不表示问卷文件损坏。',
    'LLM_OUTPUT_MODE_UNSUPPORTED': '模型服务不支持所选结构化输出模式。请在模型配置的高级设置中选择服务支持的模式；严格 Schema 不支持时可改用 JSON。',
    'LLM_SCHEMA_REJECTED': '模型服务拒绝了本次 Schema 约束。请改用 JSON 模式或将诊断报告交给维护人员检查模型兼容性。',
    'LLM_MISSING_KEY': '请先在模型配置中保存 API Key。',
    'LLM_INVALID_KEY_FORMAT': 'API Key 格式不正确，请检查空格或换行。',
    'TEMPLATE_HASH_MISMATCH': '模板与上传问卷不匹配，请选择此问卷的已审核模板。',
    'OUTPUT_EXISTS': '输出目录已存在。',
    'LLM_OUTPUT_LIMIT': '模型输出达到长度或上下文限制，答案未完整返回。请缩短待填写内容后手动重试，或联系维护人员检查输出预算。',
    'LLM_CONTENT_FILTER': '模型服务拦截了响应，未交付填写文件。请核查服务限制后手动重试。',
    'LLM_RESOURCE': '模型服务资源不足，请稍后手动重试。',
    'LLM_ABORTED': '模型服务中止了响应，请稍后手动重试。',
    'LLM_UNEXPECTED_TOOLS': '模型返回了工具调用，当前流程需要 JSON 答案，请检查模型兼容性。',
    'LLM_INCOMPLETE_RESPONSE': '模型响应没有正常结束，未交付填写文件。请下载诊断报告并检查模型兼容性。',
    'LLM_HTTP': '模型服务返回 HTTP 错误，请检查服务地址、模型权限及服务状态。',
    'LLM_CONNECTION': '无法连接模型服务，请检查服务地址及网络。',
    'CONFIG_BASE_URL': '服务地址无效，请填写不含密钥、账号、查询参数的 API 基础地址。',
    'CONFIG_HTTPS_REQUIRED': '远程模型服务地址必须使用 HTTPS。',
    'UPLOAD_SIZE': '文件为空或超过 50 MiB，请更换文件。',
    'UNSUPPORTED_FORMAT': '文件类型不支持，请按上传卡标注的格式选择。',
    'ANSWER_SCHEMA': '模型返回的答案结构不符合要求，未交付填写文件。',
    'CONFIG_NAME': '请填写不超过 80 字的配置名称。',
    'CONFIG_OUTPUT_MODE': '请选择有效的结构化输出模式。',
    'CONFIG_MODEL_ROLE': '请选择正确的模型类型：主模型使用 LLM，选项决策可使用 Jev。',
    'CONFIG_FIELDS': '模型配置包含不支持的字段，请刷新页面后重试。',
    'UNKNOWN_PRESET': '该模型服务已不再支持，请改用 DeepSeek 官方或 Jev 官方配置。',
    'CONFIG_OFFICIAL_ENDPOINT': '模型服务地址须使用对应的官方地址，请新建官方配置。',
    'ANSWER_MODE': '请选择“依据资料填写”或“模型自主作答”。',
    'UPLOAD_FIELDS': '文件或填写模式不匹配，请重新选择后提交。',
    'CONFIG_KEY_REENTRY': '服务地址已更改，请填写对应的 Key。',
    'PROFILE_NOT_FOUND': '模型配置已不存在，请重新选择。',
    'KEY_UNAVAILABLE': '无法解密已保存 Key，请检查主密钥或重新保存 Key。',
    'BUSY': '已有任务执行中，可在历史中查看进度。',
    'REQUEST_ID': '提交标识无效，请刷新页面。',
    'SERVICE_CLOSING': '服务正在关闭，请启动服务后重新提交。',
}

ERRORS.update({
    'TASK_CANCELLED': '任务已取消，上传副本与中间文件已清理。',
    'CANCELLED': '任务已取消，上传副本与中间文件已清理。',
    'NO_FILL_TARGETS': '未找到可靠的填写位置，请提供含空白单元格、表单或填写线的问卷。',
    'PROCESSOR_UNAVAILABLE': '文档转换与识别服务未就绪，请启动 converter 服务后重试。',
    'OCR_UNREADABLE': '图片页未识别出可用文字，请提供更清晰的文件。',
    'FORMAT_EXTENSION_MISMATCH': '文件实际内容与扩展名不一致，请从原应用重新导出。',
    'PAGE_LIMIT': '文件页数超过上限：问卷最多 500 页，单份 PDF 资料最多 2,000 页。请拆分后上传。',
    'QUESTION_LIMIT': '问卷超过 2,000 题，请拆分后填写。',
    'SOURCE_COUNT_LIMIT': '答案资料最多 20 份。',
    'SOURCE_TOTAL_LIMIT': '答案资料合计超过 200 MiB。',
    'UPLOAD_COLLECTION_LIMIT': '待提交资料过多，请刷新或清理后重试。',
    'UPLOAD_EXPIRED': '临时上传已失效，请重新选择文件。',
    'CONVERSION_CONTENT_LOSS': '转换前后内容不一致，已阻止交付。请另存为 DOCX 或 XLSX 后重试。',
    'CONVERSION_LAYOUT_REVIEW': '旧格式转换前后排版不一致，已阻止交付。请核对后另存为现代格式。',
    'REVIEW_STALE': '确认内容已更新或任务已结束，请刷新。',
    'REQUEST_CONFLICT': '相同提交标识对应了不同文件，请重新提交。',
    'VERIFICATION_FAILED': '填写副本未通过内容或排版校验，请查看报告。',
    'DOCUMENT_ACTIVE_CONTENT': '文档含宏、活动内容或嵌入对象，请移除后重新导出。',
    'FORMULA_VALUE_UNAVAILABLE': '答案表格含尚未计算的公式，请在表格软件中计算并保存后上传。',
})


ERRORS.update({
    'QUOTE_MISMATCH': '模型提供的答案引用未能与资料原文核对，本次未生成填写文件。请重新提交；无法核对的题目将自动留空，其他题目继续处理。',
    'EVIDENCE_ID': '模型引用了无法识别的资料位置，未采用对应答案。请重新提交；无法核对的题目将自动留空。',
    'NO_READABLE_TEXT': '未读取到可用文字。请提供文字版文件，或更清晰的扫描件。',
    'DOCUMENT_CORRUPT': '文件内容不完整或无法读取，请用原应用打开后重新导出。',
    'PASSWORD_REQUIRED': '文件已加密，请解除打开密码后重新上传。',
    'DOCUMENT_PROTECTED': '文件受到编辑保护，请提供允许填写的副本。',
    'ACTIVE_CONTENT': '文件含宏、脚本或嵌入对象，暂无法安全处理。请移除这些内容后另存副本。',
    'UNSUPPORTED_DOCUMENT_STRUCTURE': '文件包含暂不支持的内容结构，请接受修订、移除嵌入对象或另存为简化副本后重试。',
    'UNSUPPORTED_EMBEDDED_IMAGE': '文档包含暂不支持的内嵌图片格式，请把文档内图片替换为 PNG / JPG，或重新导出为普通 PDF。',
    'UNSUPPORTED_SIGNATURE_FIELD': '问卷包含签名字段，当前不能自动完成电子签名。请提供不含签名字段的填写副本。',
    'UNSUPPORTED_XFA': '暂不支持这种动态 PDF 表单，请导出为普通 PDF 后上传。',
    'SOURCE_COUNT': '答案资料需选择 1 至 20 份，请减少文件数量后重试。',
    'SOURCE_TOTAL': '答案资料合计超过 200 MiB，请减少文件或压缩后重新上传。',
    'TEXT_LIMIT': '提取文字超过 500 万字符，请拆分问卷或精简答案资料。',
    'SHEET_LIMIT': '工作表超过 100 张，请保留本次需要的工作表。',
    'CELL_LIMIT': '表格有效单元格超过 100 万个，请拆分或删除无关区域。',
    'EXPANDED_LIMIT': '文件解压后的内容过大或结构过于复杂，请拆分文件或用原应用重新导出。',
    'RENDER_LIMIT': '文档页面或图片分辨率过大，请缩小页面、降低扫描分辨率或拆分后重试。',
    'TEXT_ENCODING': '无法识别文本编码，请将文件另存为 UTF-8 后上传。',
    'CONVERSION_FAILED': '文档转换未成功，请用 Word 或 Excel 另存为 DOCX、XLSX 后重试。',
    'CONVERSION_STRUCTURE_LOSS': '转换后表格、公式或图片发生变化，已停止处理。请从原应用另存为 DOCX 或 XLSX 并核对内容。',
    'PROCESS_TIMEOUT': '文档转换或文字识别超时，请拆分文件或降低图片分辨率后重试。',
    'UPLOAD_INCOMPLETE': '文件没有上传完整，请重新选择文件并上传。',
    'LLM_INPUT_TOO_LARGE': '本批资料超过模型输入限制，请精简资料或更换支持更长输入的模型。',
    'LLM_RESPONSE_TOO_LARGE': '模型返回内容过长，请减少本次资料或更换兼容模型后重试。',
    'QUOTE_SIZE': '模型返回的引用过长，无法按要求核对。请重新提交或更换兼容模型。',
    'ANSWER_SIZE': '模型返回的单题答案过长，无法安全写入。请精简资料或更换兼容模型。',
    'UNKNOWN_OR_DUPLICATE_QUESTION': '模型返回了重复或不属于本批的题目，已停止填写以防答案错位。请重新提交或更换兼容模型。',
    'REVIEW_REQUIRED': '答案或填写位置尚需人工确认，请先完成待确认项再填写。',
    'SERVICE_RESTARTED': '服务重启时任务尚未完成。请重新选择文件并手动提交。',
})

DEFAULT_MESSAGE = '处理未能完成，本次未生成填写文件。请重新提交；若仍失败，可将“诊断代码”或诊断报告交给维护人员排查。'


def present_error(error):
    """Refresh public wording without rewriting historical task records or artifacts."""
    return dict(error, message=ERRORS.get(error.get('code'), DEFAULT_MESSAGE))


def safe_error(error):
    code = normalize_error_code(error)
    result = {
        'code': code,
        'message': ERRORS.get(code, DEFAULT_MESSAGE),
    }
    if isinstance(error, LLMFailure):
        result['diagnostics'] = dict(error.diagnostics)
    return result
