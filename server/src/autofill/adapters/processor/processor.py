"""Fixed Office/OCR operations in a separate, networkless converter container."""
from pathlib import Path
import csv
import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid


LEASE_SECONDS = 15


def renew_lease(job):
    temporary = job / 'lease.tmp'
    temporary.write_text(json.dumps({'owner': 'autofill-v2', 'expires': time.time() + LEASE_SECONDS}))
    temporary.replace(job / 'lease.json')


def lease_expired(job):
    try:
        value = json.loads((job / 'lease.json').read_text())
        return value.get('owner') == 'autofill-v2' and value['expires'] < time.time()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def reap_orphans(queue, exclude=None):
    for lease in Path(queue).glob('*/lease.json'):
        job = lease.parent
        if job != exclude and lease_expired(job):
            shutil.rmtree(job, ignore_errors=True)


class Processor:
    def __init__(self, queue=None, cancelled=lambda: False):
        value = queue or os.environ.get('AUTOFILL_PROCESS_QUEUE')
        self.queue = Path(value) if value else None
        self.cancelled = cancelled

    def run(self, path, operation, destination, format=None):
        if self.queue is None or not (self.queue / 'heartbeat').exists():
            raise ValueError('PROCESSOR_UNAVAILABLE')
        if time.time() - (self.queue / 'heartbeat').stat().st_mtime > 900:
            raise ValueError('PROCESSOR_UNAVAILABLE')
        job = self.queue / uuid.uuid4().hex
        # The API and processor share a dedicated service UID inside the
        # deployment, so job inputs and intermediate documents stay private.
        job.mkdir(mode=0o700); job.chmod(0o700)
        renew_lease(job)
        source = job / ('input' + Path(path).suffix.lower())
        shutil.copyfile(path, source); source.chmod(0o644)
        request = {'operation': operation, 'input': source.name, 'format': format}
        (job / 'request.json').write_text(json.dumps(request))
        (job / 'ready').touch()
        started = time.monotonic()
        try:
            while not (job / 'done.json').exists():
                renew_lease(job)
                if self.cancelled() or time.monotonic() - started > 600:
                    (job / 'cancel').touch()
                    raise ValueError('CANCELLED' if self.cancelled() else 'PROCESS_TIMEOUT')
                time.sleep(.1)
            result = json.loads((job / 'done.json').read_text())
            if result.get('error'):
                raise ValueError(result['error'])
            output = (job / result['file']).resolve()
            if not output.is_relative_to(job.resolve()) or not output.is_file():
                raise ValueError('PROCESS_OUTPUT')
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output, destination)
            return destination
        finally:
            if not (job / 'done.json').exists():
                (job / 'cancel').touch()
                for _ in range(30):
                    if (job / 'done.json').exists():
                        break
                    time.sleep(.1)
            shutil.rmtree(job, ignore_errors=True)

    def convert(self, path, format, destination):
        return self.run(path, 'convert', destination, format)

    def ocr(self, path, destination):
        self.run(path, 'ocr', destination)
        return json.loads(Path(destination).read_text())


def _run(argv, job, timeout=300):
    # argv is constructed here, never from model text or user commands.
    process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True, env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                               'HOME': str(job), 'LANG': 'C.UTF-8', 'OMP_THREAD_LIMIT': '2'})
    start = time.monotonic()
    try:
        while process.poll() is None:
            reap_orphans(job.parent, exclude=job)
            if lease_expired(job):raise ValueError('CANCELLED')
            if (job / 'cancel').exists() or time.monotonic() - start > timeout:
                raise ValueError('CANCELLED' if (job / 'cancel').exists() else 'PROCESS_TIMEOUT')
            time.sleep(.1)
        if process.returncode:
            raise ValueError('CONVERSION_FAILED')
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def execute_task(job, request):
    job = Path(job).resolve()
    operation = request.get('operation')
    if operation not in ('convert', 'ocr'):
        raise ValueError('PROCESS_OPERATION')
    name = request.get('input', '')
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError('PROCESS_INPUT')
    source = (job / name).resolve()
    if not source.is_relative_to(job) or not source.is_file() or source.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('PROCESS_INPUT')
    if operation == 'convert':
        filters = {'pdf': 'pdf', 'docx': 'docx:Office Open XML Text', 'xlsx': 'xlsx:Calc MS Excel 2007 XML', 'fodt': 'fodt:OpenDocument Text Flat XML', 'fods': 'fods:OpenDocument Spreadsheet Flat XML', 'doc': 'doc:MS Word 97', 'xls': 'xls:MS Excel 97'}
        fmt = request.get('format')
        if fmt not in filters:
            raise ValueError('PROCESS_FORMAT')
        output = job / 'converted'; output.mkdir(exist_ok=True)
        profile = job / 'profile'; (profile / 'user').mkdir(parents=True, exist_ok=True)
        (profile / 'user/registrymodifications.xcu').write_text('''<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item></oor:items>''')
        _run(['soffice', '-env:UserInstallation=' + profile.as_uri(), '--headless', '--norestore', '--nodefault',
              '--convert-to', filters[fmt], '--outdir', str(output), str(source)], job)
        result = output / (source.stem + '.' + fmt)
        if not result.is_file() or not result.stat().st_size:
            raise ValueError('CONVERSION_FAILED')
        return str(result.relative_to(job))
    if source.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
        raise ValueError('PROCESS_FORMAT')
    _run(['tesseract', str(source), str(job / 'ocr'), '-l', 'eng+chi_sim', '--psm', '11', 'tsv'], job)
    with (job / 'ocr.tsv').open(encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream, delimiter='\t'))
    words = []
    for row in rows:
        if row.get('text', '').strip() and float(row.get('conf', -1)) >= 0:
            x, y, w, h = (int(row[k]) for k in ('left', 'top', 'width', 'height'))
            words.append({'text': row['text'], 'confidence': float(row['conf']), 'bbox': [x, y, x+w, y+h],
                          'line': [row['block_num'], row['par_num'], row['line_num']]})
    (job / 'ocr.json').write_text(json.dumps(words, ensure_ascii=False))
    return 'ocr.json'


def main():
    queue = Path(os.environ.get('AUTOFILL_PROCESS_QUEUE', '/work'))
    queue.mkdir(parents=True, exist_ok=True)
    while True:
        (queue / 'heartbeat').touch()
        reap_orphans(queue)
        for ready in sorted(queue.glob('*/ready')):
            job = ready.parent
            try:
                ready.unlink()
                request_path = job / 'request.json'
                if request_path.stat().st_size > 4096:
                    raise ValueError('PROCESS_INPUT')
                result = {'file': execute_task(job, json.loads(request_path.read_text()))}
            except Exception as error:
                code = str(error).split(':', 1)[0]
                result = {'error': code if code in {'PROCESS_INPUT','PROCESS_FORMAT','PROCESS_OPERATION','CONVERSION_FAILED','PROCESS_TIMEOUT','CANCELLED'} else 'CONVERSION_FAILED'}
            if job.exists():
                temporary = job / 'done.tmp'
                temporary.write_text(json.dumps(result))
                temporary.replace(job / 'done.json')
            (queue / 'heartbeat').touch()
        time.sleep(.1)


if __name__ == '__main__':
    main()
