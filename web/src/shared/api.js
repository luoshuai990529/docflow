export const API_PREFIX = '/api/v1';
export const MAX_WEB_UPLOAD_BYTES = 10 * 1024 * 1024;

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
let csrfToken = null;
let sessionBootstrap = null;

export class APIError extends Error {
  constructor(message, status = 0, code = '') {
    super(message);
    this.status = status;
    this.code = code;
  }

  get uncertain() { return this.status === 0 || this.status >= 500; }
}

export function apiUrl(path) {
  if (typeof path !== 'string' || !path.startsWith('/')) {
    throw new TypeError('API paths must be absolute paths inside the versioned API.');
  }
  return API_PREFIX + path;
}

async function readJson(response, fallbackMessage) {
  try {
    return await response.json();
  } catch {
    throw new APIError(fallbackMessage, response.ok ? 0 : response.status);
  }
}

export async function bootstrapApiSession({ refresh = false } = {}) {
  if (refresh) {
    csrfToken = null;
    sessionBootstrap = null;
  }
  if (csrfToken) return csrfToken;
  if (!sessionBootstrap) {
    sessionBootstrap = (async () => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(apiUrl('/session'), {
          method: 'GET', cache: 'no-store', credentials: 'same-origin',
          headers: { Accept: 'application/json' }, signal: controller.signal,
        });
        const data = await readJson(response, '服务暂时无法响应，请重试。');
        if (!response.ok) throw new APIError(data.error?.message || '无法建立本地会话，请刷新页面。', response.status, data.error?.code);
        if (typeof data.csrf_token !== 'string' || data.csrf_token.length < 16) {
          throw new APIError('服务返回的会话信息无效，请刷新页面。', 502);
        }
        csrfToken = data.csrf_token;
        return csrfToken;
      } catch (error) {
        if (error instanceof APIError) throw error;
        throw new APIError('无法建立 API 会话，请检查连接后重试。');
      } finally {
        clearTimeout(timeout);
      }
    })().catch(error => {
      sessionBootstrap = null;
      throw error;
    });
  }
  return sessionBootstrap;
}

export async function api(path, method = 'GET', body) {
  const verb = method.toUpperCase();
  const serializedBody = body === undefined ? undefined : JSON.stringify(body);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), SAFE_METHODS.has(verb) ? 10000 : 30000);
  try {
    const headers = { Accept: 'application/json' };
    if (!SAFE_METHODS.has(verb)) headers['X-CSRF-Token'] = await bootstrapApiSession();
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(apiUrl(path), {
      signal: controller.signal, method: verb, cache: 'no-store', credentials: 'same-origin',
      headers, body: serializedBody,
    });
    const data = await readJson(response, '服务暂时无法响应，请重试。');
    if (!response.ok) throw new APIError(data.error?.message || '请求失败，请稍后重试。', response.status, data.error?.code);
    return data;
  } catch (error) {
    if (error instanceof APIError) throw error;
    throw new APIError('连接中断，服务端可能仍在处理。');
  } finally {
    clearTimeout(timeout);
  }
}

export async function uploadFile(file, role, signal) {
  if (!file?.size || file.size > MAX_WEB_UPLOAD_BYTES) {
    throw new APIError('文件为空或超过 10 MiB，请重新选择。', 413, 'WEB_UPLOAD_SIZE');
  }
  const token = await bootstrapApiSession();
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) cancel();
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; cancel(); }, 120000);
  try {
    const query = new URLSearchParams({ name: file.name, role });
    const response = await fetch(apiUrl('/uploads?' + query), {
      signal: controller.signal, method: 'PUT', cache: 'no-store', credentials: 'same-origin',
      headers: { Accept: 'application/json', 'X-CSRF-Token': token, 'Content-Type': 'application/octet-stream' },
      body: file,
    });
    const data = await readJson(response, '上传响应异常，请重新上传。');
    if (!response.ok) throw new APIError(data.error?.message || '上传失败，请重试。', response.status, data.error?.code);
    if (typeof data.token !== 'string') throw new APIError('上传响应异常，请重新上传。', 502);
    return data.token;
  } catch (error) {
    if (error instanceof APIError) throw error;
    throw new APIError(signal?.aborted ? '已取消上传，文件选择已保留。' : timedOut ? '上传超时，请重试。' : '上传连接中断，请重试。', 0);
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', cancel);
  }
}
