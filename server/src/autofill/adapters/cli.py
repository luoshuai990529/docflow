"""Command-line inbound adapter, preserving the `python -m autofill` contract."""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ..application.answering.match import MockMatcher
from ..config import PRESETS, init_config, load_config
from ..core import apply_pair, prepare_pair, run_pair
from ..models import document_from_dict


def main(argv=None):
    parser = argparse.ArgumentParser(prog='autofill')
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--questionnaire', type=Path, required=True)
    prepare.add_argument('--source', type=Path, action='append')
    prepare.add_argument('--template', type=Path)
    prepare.add_argument('--general', action='store_true')
    prepare.add_argument('--out', type=Path, required=True)
    prepare.add_argument('--answer-mode', choices=['grounded', 'autonomous'], default='grounded')
    apply = sub.add_parser('apply')
    apply.add_argument('--bundle', type=Path, required=True)
    apply.add_argument('--answers', type=Path, required=True)
    apply.add_argument('--mode', choices=['real', 'fictional_test'], required=True)
    apply.add_argument('--out', type=Path, required=True)
    mock = sub.add_parser('mock-answers')
    mock.add_argument('--bundle', type=Path, required=True)
    mock.add_argument('--mode', choices=['fictional_test'], required=True)
    mock.add_argument('--out', type=Path, required=True)
    run = sub.add_parser('run')
    for name in ('questionnaire', 'config', 'out'):
        run.add_argument('--' + name, type=Path, required=True)
    run.add_argument('--source', type=Path, action='append')
    run.add_argument('--template', type=Path)
    run.add_argument('--general', action='store_true')
    run.add_argument('--answer-mode', choices=['grounded', 'autonomous'], default='grounded')
    run.add_argument('--decision-config', type=Path)
    config = sub.add_parser('init-config')
    config.add_argument('--provider', choices=list(PRESETS), required=True)
    config.add_argument('--out', type=Path, required=True)
    for name in ('base-url', 'model', 'output-mode'):
        config.add_argument('--' + name)

    args = parser.parse_args(argv)
    try:
        if args.command == 'init-config':
            print(init_config(args.provider, args.out, base_url=args.base_url,
                              model=args.model, output_mode=args.output_mode))
            return 0
        if args.command == 'run':
            selected = load_config(args.config)
            if selected.protocol != 'openai_chat':
                raise ValueError('CONFIG_MODEL_ROLE')
            from .models.openai_chat import OpenAIChatClient
            if args.general or args.answer_mode == 'autonomous':
                from .models.jev_client import JevClient
                from ..general import run_general
                decision = load_config(args.decision_config) if args.decision_config else None
                if decision and (args.answer_mode != 'autonomous' or decision.protocol != 'typesafe_systemone'):
                    raise ValueError('CONFIG_MODEL_ROLE')
                result = run_general(args.questionnaire, args.source or [], selected, args.out,
                    OpenAIChatClient(selected), answer_mode=args.answer_mode,
                    decision_client=JevClient(decision) if decision else None)
            else:
                if not args.source or len(args.source) != 1 or args.template is None or args.decision_config:
                    raise ValueError('MATCH_INPUT_REQUIRED')
                result = run_pair(args.questionnaire, args.source[0], args.template,
                                  selected, args.out, OpenAIChatClient(selected))
            print(json.dumps({'output': str(result.output_path) if result.output_path else None,
                              'report': str(result.report_json), 'outcome': result.outcome}))
            return {'complete': 0, 'partial': 2, 'failed': 1}[result.outcome]
        if args.command == 'prepare':
            if args.general or args.answer_mode == 'autonomous':
                from ..general import prepare_general
                prepare_general(args.questionnaire, args.source or [], args.out,
                                answer_mode=args.answer_mode)
                print(args.out)
            else:
                if not args.source or len(args.source) != 1:
                    raise ValueError('MATCH_INPUT_REQUIRED')
                print(prepare_pair(args.questionnaire, args.source[0], args.template, args.out))
            return 0
        if args.command == 'apply':
            result = apply_pair(args.bundle, args.answers, args.out, args.mode)
            print(json.dumps({'output': str(result.output_path) if result.output_path else None,
                              'report': str(result.report_json), 'outcome': result.outcome}))
            return {'complete': 0, 'partial': 2, 'failed': 1}[result.outcome]
        if args.out.exists():
            raise ValueError('OUTPUT_EXISTS')
        if not args.out.name.startswith('TEST-SAMPLE'):
            raise ValueError('TEST_FILENAME_REQUIRED')
        q = document_from_dict(json.loads((args.bundle / 'questionnaire.json').read_text()))
        source = document_from_dict(json.loads((args.bundle / 'source.json').read_text()))
        payload = {'origin': 'synthetic_test', 'questionnaire_sha256': q.sha256,
                   'source_sha256': source.sha256,
                   'decisions': [asdict(item) for item in MockMatcher().match(q.questions, source.spans)]}
        with args.out.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        print(args.out)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        message = str(error)
        return 3 if any(code in message for code in
                        ('UNSUPPORTED', 'MATCH_INPUT_REQUIRED', 'NO_READABLE_TEXT', 'PASSWORD_REQUIRED')) else 1
