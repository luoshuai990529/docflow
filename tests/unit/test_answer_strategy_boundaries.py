"""Application answer policies stay separate from model transports."""

import ast
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / 'server' / 'src' / 'autofill'


class AnswerStrategyBoundaryTests(unittest.TestCase):
    def test_answer_policies_do_not_import_concrete_model_transports(self):
        forbidden = {'urllib', 'httpx', 'requests'}
        problems = []
        for path in sorted((SERVER / 'application' / 'answering').rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or '']
                else:
                    continue
                for module in modules:
                    if (module in forbidden or module.startswith('urllib.')
                            or module.startswith('.adapters') or module.startswith('autofill.adapters')
                            or module.endswith('.openai_chat') or module.endswith('.jev_client')):
                        problems.append(f'{path.relative_to(SERVER)} imports {module}')
        self.assertEqual(problems, [])


if __name__ == '__main__':
    unittest.main()
