"""Generate or validate the checked-in OpenAPI contract from FastAPI routes."""

import argparse
from pathlib import Path

import yaml

from .app import create_app


DEFAULT_OUTPUT = Path(__file__).resolve().parents[5] / 'docs' / 'api' / 'openapi.yaml'


def rendered_openapi() -> str:
    return yaml.safe_dump(create_app().openapi(), allow_unicode=True, sort_keys=False, width=120)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='autofill-openapi')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    rendered = rendered_openapi()
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding='utf-8') != rendered:
            raise SystemExit('OpenAPI contract is stale; regenerate with python -m autofill.adapters.http.export_openapi')
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
