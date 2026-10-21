"""FastAPI entry adapter; application services remain framework-independent."""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.requests import ClientDisconnect

from ...errors import safe_error
from .schemas import (
    ApiError,
    ApiErrorEnvelope,
    CancelAccepted,
    ConfigList,
    ConfigResult,
    ConfigWrite,
    DeleteResult,
    JobAccepted,
    JobList,
    JobSubmit,
    JobView,
    ReportView,
    SessionBootstrap,
    UploadReceipt,
)
from .security import RequestSecurity


ERROR_RESPONSES = {
    400: {'model': ApiErrorEnvelope, 'description': 'Request rejected'},
    403: {'model': ApiErrorEnvelope, 'description': 'Origin, trusted peer, or CSRF check failed'},
    404: {'model': ApiErrorEnvelope, 'description': 'Resource not found'},
    409: {'model': ApiErrorEnvelope, 'description': 'Task conflict'},
    413: {'model': ApiErrorEnvelope, 'description': 'The request is too large; web uploads are limited to 10 MiB per file.'},
    422: {'model': ApiErrorEnvelope, 'description': 'Request schema is invalid'},
}
API_PREFIX = '/api/v1'
MAX_WEB_UPLOAD_BYTES = 10 * 1024 * 1024
API_ERROR_MESSAGES = {
    'WEB_UPLOAD_SIZE': '单个 Web 上传文件不得超过 10 MiB。',
}


def _default_service():
    from ...service import WorkflowService

    results = Path(os.environ.get('AUTOFILL_RESULTS_DIR', 'var/results'))
    state = Path(os.environ.get('AUTOFILL_STATE_DIR', 'var/workflow'))
    key_file = Path(os.environ.get('AUTOFILL_KEY_FILE', 'var/secrets/master.key'))
    return WorkflowService(results, state_dir=state, key_file=key_file)


def _service(request: Request):
    service = getattr(request.app.state, 'workflow_service', None)
    if service is None:
        raise HTTPException(status_code=503, detail='SERVICE_NOT_READY')
    return service


def _csrf(request: Request, csrf_header: Annotated[str, Header(alias='X-CSRF-Token')]):
    expected = request.app.state.csrf_token
    if not csrf_header or not secrets.compare_digest(csrf_header, expected):
        raise HTTPException(status_code=403, detail='FORBIDDEN')


def _error_payload(code: str, message: str | None = None, diagnostics=None):
    fallback = safe_error(ValueError(code))
    return {'error': {'code': code, 'message': message or fallback['message'],
                      **({'diagnostics': diagnostics} if diagnostics else {})}}


def _resolve_public_file(service, value):
    if not isinstance(value, str) or not value:
        return None
    root = Path(service.root).resolve()
    path = Path(value).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def create_app(*, service=None, public_origin=None, trusted_proxy_cidrs=None,
               csrf_token=None, service_factory=None) -> FastAPI:
    """Create the API without starting storage or touching runtime state until lifespan."""
    owns_service = service is None
    factory = service_factory or _default_service
    security = RequestSecurity(
        public_origin=public_origin or os.environ.get('AUTOFILL_PUBLIC_ORIGIN') or None,
        trusted_proxy_cidrs=tuple(trusted_proxy_cidrs or
            filter(None, os.environ.get('AUTOFILL_TRUSTED_PROXY_CIDRS', '127.0.0.1/32,::1/128').split(','))),
    )

    @asynccontextmanager
    async def lifespan(app):
        if owns_service:
            app.state.workflow_service = factory()
        else:
            app.state.workflow_service = service
        try:
            yield
        finally:
            if owns_service:
                app.state.workflow_service.close()
                app.state.workflow_service = None

    app = FastAPI(
        title='Workflow API',
        version='1.0.0',
        description='Versioned same-origin API for local document workflow tasks.',
        openapi_url=f'{API_PREFIX}/openapi.json',
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        responses={422: ERROR_RESPONSES[422]},
    )
    app.state.csrf_token = csrf_token or secrets.token_urlsafe(32)
    app.state.request_security = security

    @app.get('/healthz', include_in_schema=False)
    def healthcheck():
        return {'status': 'ok'}

    @app.middleware('http')
    async def enforce_private_origin(request, call_next):
        if request.url.path.startswith(API_PREFIX) and not security.allows(request):
            response = JSONResponse(status_code=403, content=_error_payload('FORBIDDEN'))
        else:
            response = await call_next(request)
        if request.url.path.startswith(API_PREFIX):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, error):
        if any(item.get('loc', ())[-1:] == ('X-CSRF-Token',) for item in error.errors()):
            return JSONResponse(status_code=403, content=_error_payload('FORBIDDEN'))
        return JSONResponse(status_code=422, content=_error_payload('REQUEST_INVALID'))

    @app.exception_handler(HTTPException)
    async def http_error(_request, error):
        code = error.detail if isinstance(error.detail, str) and error.detail.isupper() else 'HTTP_ERROR'
        return JSONResponse(status_code=error.status_code,
                            content=_error_payload(code, API_ERROR_MESSAGES.get(code)))

    @app.exception_handler(ValueError)
    async def workflow_error(_request, error):
        result = safe_error(error)
        code = result['code']
        status = 409 if code in ('BUSY', 'REQUEST_CONFLICT') else 413 if code == 'UPLOAD_SIZE' else 400
        return JSONResponse(status_code=status, content={'error': result})

    @app.exception_handler(Exception)
    async def unexpected_error(_request, _error):
        return JSONResponse(status_code=500, content=_error_payload('PROCESSING_ERROR'))

    router = APIRouter(prefix=API_PREFIX, responses=ERROR_RESPONSES)
    csrf = [Depends(_csrf)]

    @router.get('/session', response_model=SessionBootstrap, tags=['session'])
    def session(request: Request):
        return SessionBootstrap(csrf_token=request.app.state.csrf_token)

    @router.get('/configs', response_model=ConfigList, tags=['configuration'])
    def configurations(service=Depends(_service)):
        return service.configurations()

    @router.post('/configs', response_model=ConfigResult, status_code=201,
                 dependencies=csrf, tags=['configuration'])
    def create_configuration(body: ConfigWrite, service=Depends(_service)):
        return {'config': service.store.save_profile(body.to_store_body())}

    @router.put('/configs/{identity}', response_model=ConfigResult, dependencies=csrf, tags=['configuration'])
    @router.post('/configs/{identity}', response_model=ConfigResult, dependencies=csrf,
                 include_in_schema=False, tags=['configuration'])
    def update_configuration(identity: Annotated[str, PathParam(pattern=r'^[a-f0-9]{32}$')],
                             body: ConfigWrite, service=Depends(_service)):
        return {'config': service.store.save_profile(body.to_store_body(), identity)}

    @router.delete('/configs/{identity}', response_model=DeleteResult,
                   dependencies=csrf, tags=['configuration'])
    def delete_configuration(identity: Annotated[str, PathParam(pattern=r'^[a-f0-9]{32}$')],
                             service=Depends(_service)):
        service.store.delete_profile(identity)
        return {'deleted': True}

    @router.put('/uploads', response_model=UploadReceipt, status_code=201,
                dependencies=csrf, tags=['uploads'],
                description='Stream a document upload. Web clients may upload at most 10 MiB per file; CLI and document inspection limits are separate.')
    async def upload(
        request: Request,
        name: Annotated[str, Query(min_length=1, max_length=240)],
        role: Literal['questionnaire', 'source'],
        service=Depends(_service),
    ):
        if request.headers.get('content-type') != 'application/octet-stream':
            raise ValueError('INVALID_UPLOAD')
        content_length = request.headers.get('content-length')
        expected_length = None
        try:
            if content_length is not None:
                expected_length = int(content_length)
        except ValueError:
            raise ValueError('INVALID_UPLOAD') from None
        if expected_length is not None and expected_length <= 0:
            raise ValueError('INVALID_UPLOAD')
        if expected_length is not None and expected_length > MAX_WEB_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail='WEB_UPLOAD_SIZE')

        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b') as spool:
            received = 0
            try:
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > MAX_WEB_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail='WEB_UPLOAD_SIZE')
                    spool.write(chunk)
            except ClientDisconnect:
                raise ValueError('UPLOAD_INCOMPLETE') from None
            if not received:
                raise ValueError('INVALID_UPLOAD')
            if expected_length is not None and received != expected_length:
                raise ValueError('UPLOAD_INCOMPLETE')
            spool.seek(0)
            return service.tasks.upload(spool, received, name, role)

    @router.delete('/uploads/{token}', response_model=DeleteResult,
                   dependencies=csrf, tags=['uploads'])
    def delete_upload(token: Annotated[str, PathParam(pattern=r'^[a-f0-9]{32}$')],
                      service=Depends(_service)):
        service.tasks.discard(token)
        return {'deleted': True}

    @router.post('/jobs', response_model=JobAccepted, status_code=202,
                 dependencies=csrf, tags=['jobs'])
    def submit_job(body: JobSubmit, service=Depends(_service)):
        return {'id': service.submit(body.model_dump(exclude_none=True))}

    @router.get('/jobs', response_model=JobList, tags=['jobs'])
    def list_jobs(service=Depends(_service)):
        return {'jobs': service.jobs()}

    @router.get('/jobs/{identity}', response_model=JobView, tags=['jobs'])
    def get_job(identity: Annotated[str, PathParam(pattern=r'^[a-f0-9]{32}$')], service=Depends(_service)):
        job = service.public_job(identity)
        if job is None:
            raise HTTPException(status_code=404, detail='NOT_FOUND')
        return job

    @router.post('/jobs/{identity}/cancel', response_model=CancelAccepted,
                 dependencies=csrf, tags=['jobs'])
    def cancel_job(identity: Annotated[str, PathParam(pattern=r'^[a-f0-9]{32}$')],
                   service=Depends(_service)):
        service.tasks.cancel(identity)
        return {'accepted': True}

    @router.get('/jobs/{identity}/report.{extension}', response_model=ReportView,
                responses={200: {'content': {'text/html': {}, 'text/markdown': {}}}}, tags=['reports'])
    def report_view(identity: Annotated[str, PathParam(pattern=r'^[a-f0-9]{32}$')],
                    extension: Literal['json', 'md', 'html'], service=Depends(_service)):
        job = service.store.run(identity, True)
        path = _resolve_public_file(service, job.get('files', {}).get('report.json') if job else None)
        if path is None:
            raise HTTPException(status_code=404, detail='NOT_FOUND')
        report = json.loads(path.read_text(encoding='utf-8'))
        if extension == 'html':
            from ...adapters.documents.report import render_html

            return HTMLResponse(render_html(report), headers={'Cache-Control': 'no-store'})
        if extension == 'md':
            from ...adapters.documents.report import render_markdown

            return PlainTextResponse(render_markdown(report), media_type='text/markdown; charset=utf-8',
                headers={'Content-Disposition': 'attachment; filename="report.md"'})
        from ...adapters.documents.report import prepare_report

        return prepare_report(report)

    @router.get('/jobs/{identity}/download/{filename}', tags=['downloads'])
    def download(
        identity: Annotated[str, PathParam(pattern=r'^[a-f0-9]{32}$')],
        filename: Annotated[str, PathParam(min_length=1, max_length=200, pattern=r'^[A-Za-z0-9._-]+$')],
        service=Depends(_service),
    ):
        job = service.store.run(identity, True)
        files = job.get('files', {}) if job else {}
        path = _resolve_public_file(service, files.get(filename) if isinstance(files, dict) else None)
        if path is None:
            raise HTTPException(status_code=404, detail='NOT_FOUND')
        media_type = 'application/pdf' if path.suffix.lower() == '.pdf' else 'application/octet-stream'
        return FileResponse(path, media_type=media_type, filename=path.name,
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    app.include_router(router)
    return app


def main():
    import uvicorn

    uvicorn.run(
        'autofill.adapters.http.app:create_app',
        factory=True,
        host=os.environ.get('AUTOFILL_API_HOST', '0.0.0.0'),
        port=int(os.environ.get('AUTOFILL_API_PORT', '8000')),
        proxy_headers=False,
        access_log=False,
    )


if __name__ == '__main__':
    main()
