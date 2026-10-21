"""Evidence aggregation may complete answers, but may never invent text."""
import json
import unittest
from dataclasses import asdict, replace
from autofill.models import Decision, Question, SourceSpan
from autofill.application.answering.match import match_bounded, LLMMatcher, chunks, _source_payload
from autofill.domain.decisions import requires_review, validate_decisions, text_answer_supported
from autofill.adapters.models.openai_chat import LLMResponse, LLMFailure
from autofill.application.answering.source_context import source_markdown


class CandidateClient:
    def __init__(self, replies, reconciliation=None, reviews=None):
        self.replies = list(replies)
        self.reconciliation = reconciliation
        self.reconciliation_calls = 0
        self.reconciliation_bodies = []
        self.matching_bodies = []
        self.review_calls = []
        self.reviews = reviews

    def generate_json(self, prompt, schema):
        body = json.loads(prompt.split('\n', 1)[1])
        if body.get('phase') in ('reconcile', 'reconcile_correction'):
            self.reconciliation_calls += 1
            self.reconciliation_bodies.append(body)
            reply = self.reconciliation.pop(0) if isinstance(self.reconciliation, list) else self.reconciliation
            if isinstance(reply, Exception):
                raise reply
            return LLMResponse(reply)
        if body.get('phase') == 'answer_support_review':
            self.review_calls.append(body)
            source = [SourceSpan(**span) for span in body['source_spans']]
            reviews = self.reviews if self.reviews is not None else [dict(
                question_id=item['question']['id'], verdict='supported', transformations=[]
                if item['question']['kind'] != 'text' or text_answer_supported(Decision(**item['answer']), source)
                else ['paraphrase'],
                reason='本地夹具的全部题目要求与候选原文对应。', calculations=[])
                for item in body['questions']]
            return LLMResponse({'reviews': reviews})
        if body.get('phase') == 'answer_revision':
            # No omitted response time exists in this fixture. A revision may
            # not manufacture it merely to obtain an accepted verdict.
            return LLMResponse({'decisions': [dict(question_id=q['question']['id'], status='not_found',
                value=None, option_ids=[], evidence=[], reason='缺少所需响应时限原文。')
                for q in body['questions']]})
        if body.get('phase') == 'answer_correction':
            reply = self.last_reply
        else:
            self.matching_bodies.append(body)
            reply = self.last_reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(reply)


def ready(value, span):
    return asdict(Decision('q1', 'ready', value, [], [span], value, '原文支持'))


def final_answer(spans, value=None, options=None, status='ready'):
    return {'decisions': [dict(question_id='q1', status=status, value=value, option_ids=options or [],
        evidence=[dict(span_id=s.id, quote=s.text) for s in spans] if status == 'ready' else [],
        reason='逐条原文支持最终回答。')]}


class MultiSourceMatching(unittest.TestCase):
    def setUp(self):
        self.questions = [Question('q1', '断电与漏水分别多久响应？', 'text', [], ['t1'])]
        self.spans = [SourceSpan('s1', {'source_name': '电力规范'}, '断电30分钟内上门。', 'a'),
                      SourceSpan('s2', {'source_name': '供水规范'}, '漏水1小时内到场。', 'b')]
        self.replies = [{'decisions': [ready(span.text, span.id)]} for span in self.spans]

    def test_final_review_keeps_rejected_candidate_sources_for_conflict_check(self):
        q=Question('q1','甲公司2025年营业收入是多少？','text',[],['t1'])
        spans=[SourceSpan('s1',{},'甲公司2025年营业收入为100万元。','a'),SourceSpan('s2',{},'甲公司2025年营业收入为200万元。','b')]
        class Reviewer(CandidateClient):
            def generate_json(inner,prompt,schema):
                body=json.loads(prompt.split('\n',1)[1])
                if body['phase']=='answer_support_review':
                    inner.review_calls.append(body)
                    return LLMResponse({'reviews':[dict(question_id='q1',verdict='unsupported' if len(body['source_spans'])==2 else 'supported',reason='同一年度收入存在未解决冲突。')]})
                return super().generate_json(prompt,schema)
        client=Reviewer([{'decisions':[ready(s.text,s.id)]} for s in spans],final_answer(spans[:1],spans[0].text))
        result,_=match_bounded(client,[q],spans,chunk_chars=5)
        self.assertEqual({s['id'] for s in client.review_calls[0]['source_spans']},{'s1','s2'})
        self.assertEqual(result[0].status,'needs_review')

    def test_complementary_sources_use_model_final_answer_with_per_span_citations(self):
        final_value = '电力：断电30分钟内上门。\n供水：漏水1小时内到场。'
        client = CandidateClient(self.replies, final_answer(self.spans, final_value))
        decisions, candidates = match_bounded(client, self.questions, self.spans, chunk_chars=5)
        answer = decisions[0]
        self.assertEqual(answer.status, 'ready')
        self.assertEqual(answer.value, final_value)
        self.assertIn('source_markdown', client.matching_bodies[0])
        self.assertEqual(client.matching_bodies[0]['source_markdown'], source_markdown(self.spans[:1]))
        self.assertEqual(client.reconciliation_bodies[0]['phase'], 'reconcile')
        self.assertIn('source_markdown', client.reconciliation_bodies[0])
        self.assertEqual(answer.span_ids, ['s1', 's2'])
        self.assertEqual(client.reconciliation_calls, 1)
        self.assertEqual(len(client.review_calls), 1)
        self.assertEqual(len(candidates['q1']), 2)
        self.assertFalse(requires_review(self.questions[0], answer, self.spans, check_text=False))
        literal = replace(answer, value='断电30分钟内上门。\n漏水1小时内到场。')
        self.assertEqual(validate_decisions([literal], self.questions, self.spans, 'real')[0].value, literal.value)
        legacy = LLMMatcher(CandidateClient([{'decisions': [asdict(literal)]}])).match(self.questions, self.spans)
        self.assertEqual(legacy[0].status, 'ready')
        for invalid in [replace(answer, value=answer.value+'\n均符合服务要求'), replace(answer, span_ids=['s1'])]:
            with self.assertRaises(ValueError):
                validate_decisions([invalid], self.questions, self.spans, 'real')
        choices = [Question('q1', '支持哪些服务？', 'multiple_choice', ['A', 'B'], ['t1'])]
        replies = [{'decisions': [asdict(Decision('q1','ready',None,[option],[span.id],span.text,''))]}
                   for option, span in zip(['A', 'B'], self.spans)]
        result, _ = match_bounded(CandidateClient(replies, final_answer(self.spans, options=['A', 'B'])), choices, self.spans, chunk_chars=5)
        self.assertEqual(result[0].status, 'ready')
        self.assertEqual(result[0].option_ids, ['A', 'B'])
        self.assertEqual(result[0].span_ids, ['s1', 's2'])

    def test_incomplete_chunks_do_not_erase_supported_answers_or_claim_no_evidence(self):
        # This case asks only the part covered by the valid source. A missing
        # second document must not erase that answer; completeness is judged
        # separately for a question asking for both response times.
        questions = [replace(self.questions[0], text='断电多久响应？')]
        malformed_missing = asdict(Decision('q1', 'not_found', None, [], [], None, '未找到'))
        malformed_missing['quote'] = []
        broken = [None, {'decisions': []}, {'unexpected': []}, {'decisions': [{'question_id': 'q1'}]}, {'decisions': [malformed_missing]}, LLMFailure('LLM_BAD_JSON', {'stage': 'answer'})]
        for bad in broken:
            with self.subTest(bad=str(bad)):
                client = CandidateClient([self.replies[0], bad])
                decisions, _ = match_bounded(client, questions, self.spans, chunk_chars=5)
                self.assertEqual(decisions[0].status, 'ready')
                self.assertIn('部分资料', decisions[0].reason)
                coverage = {}
                decisions, _ = match_bounded(CandidateClient([bad, bad]), questions, self.spans, chunk_chars=5, coverage=coverage)
                self.assertNotEqual(decisions[0].status, 'not_found')
                self.assertFalse(coverage['questions']['q1']['complete'])
        missing = {'decisions': [asdict(Decision('q1', 'not_found', None, [], [], None, '未找到'))]}
        coverage = {}
        decisions, _ = match_bounded(CandidateClient([missing, missing]), self.questions, self.spans, chunk_chars=5, coverage=coverage)
        self.assertEqual(decisions[0].status, 'not_found')
        self.assertTrue(coverage['questions']['q1']['complete'])
        uncertain = {'decisions': [asdict(Decision('q1', 'needs_review', None, [], [], None, '本块没有相关答案'))]}
        client = CandidateClient([self.replies[0], uncertain])
        decisions, _ = match_bounded(client, questions, self.spans, chunk_chars=5)
        self.assertEqual(decisions[0].status, 'ready')
        # Literal provenance does not prove that a compound question is complete.
        review = dict(question_id='q1', verdict='uncertain', transformations=[],
                      reason='缺少漏水响应时限，答案不完整。', calculations=[])
        client = CandidateClient([self.replies[0], missing], reviews=[review])
        decisions, _ = match_bounded(client, self.questions, self.spans, chunk_chars=5)
        self.assertEqual(decisions[0].status, 'needs_review')
        self.assertEqual(len(client.review_calls), 1)
        numeric = SourceSpan('n', {'format_status':'unsupported','raw_value':'0.125','number_format':'0.0#%'}, '0.125', 'hash')
        numeric_question = Question('q1', '违约比例', 'text', [], ['t1'])
        self.assertTrue(requires_review(numeric_question, Decision('q1','ready','0.125',[],['n'],'0.125',''), [numeric]))
        known = replace(numeric, text='12.5%', locator={'format_status':'normalized'})
        self.assertFalse(requires_review(numeric_question, Decision('q1','ready','12.5%',[],['n'],'12.5%',''), [known]))
        # A rejected OCR candidate must not veto another reliable source.
        low = SourceSpan('low', {'confidence':50}, 'Acme', 'hash')
        high = SourceSpan('high', {'confidence':100}, 'Acme', 'hash')
        result, _ = match_bounded(CandidateClient([{'decisions':[ready('Acme',s.id)]} for s in (low,high)]),
                                 [Question('q1','Company','text',[],['t1'])], [low,high], chunk_chars=5)
        self.assertEqual(result[0].status, 'ready');self.assertEqual(result[0].span_ids, ['high'])
        large = [SourceSpan(f's{i}', {}, str(i)+'x'*400, 'hash') for i in range(3)]
        batches = list(chunks(large, 1800))
        self.assertEqual([s.id for batch in batches for s in batch], ['s0', 's1', 's2'])
        for batch in batches:
            actual = json.dumps(_source_payload(batch), ensure_ascii=False)
            self.assertLessEqual(len(actual), 1800)
        # Native rows stay together when they fit; oversized rows retain
        # heading and column context on every fragment without altering IDs.
        row = [SourceSpan(f'row{i}', {'source_name':'table.docx', 'part':'word/document.xml', 'paragraph':i,
            'context': {'kind':'table_cell', 'table':1, 'row':2, 'cell':i+1, 'heading':'响应规则',
                'column_label':f'字段{i}', **({'row_cells':['a','b','c']} if i == 0 else {})}}, 'x' * 400, 'h') for i in range(3)]
        original = [asdict(s) for s in row]
        native = list(chunks([SourceSpan('intro', {}, 'x' * 500, 'h'), *row],
            len(json.dumps(_source_payload(row), ensure_ascii=False)) + 30))
        self.assertTrue(any({s.id for s in row} <= {s.id for s in batch} for batch in native))
        fragmented = list(chunks(row, 1700))
        self.assertGreater(len(fragmented), 1)
        for batch in fragmented:
            payload = _source_payload(batch)
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), 1700)
            self.assertIn('响应规则', payload['source_markdown'])
            self.assertIn('同一行上下文', payload['source_markdown'])
            self.assertNotIn('row_cells', payload['source_spans'][0]['locator']['context'])
        self.assertEqual([s.id for batch in fragmented for s in batch], [s.id for s in row])
        self.assertEqual([asdict(s) for s in row], original)

    def test_invalid_reconciliation_and_explicit_conflict_never_choose_a_winner(self):
        invalid = [{'decisions': []}, final_answer([], status='conflict'),
                   {'decisions': [{'question_id': 'q1'}]},
                   final_answer([replace(self.spans[0], id='foreign')], 'invented'),
                   final_answer([replace(self.spans[0], text='invented')], 'invented'),
                   LLMFailure('LLM_BAD_JSON', {'stage': 'answer'})]
        for response in invalid:
            with self.subTest(response=str(response)):
                client = CandidateClient(self.replies, response)
                coverage = {}
                decisions, _ = match_bounded(client, self.questions, self.spans, chunk_chars=5, coverage=coverage)
                expected = 'conflict' if response == final_answer([], status='conflict') else 'needs_review'
                self.assertEqual(decisions[0].status, expected)
                self.assertEqual(client.reconciliation_calls, 1 if expected == 'conflict' else 4)
                if expected != 'conflict':
                    self.assertEqual(coverage['validation_events'][0]['review_stage'], 'reconcile')
                    self.assertEqual(coverage['validation_events'][0]['attempt_count'], 4)
        # A corrected final answer is accepted without requesting the two
        # already valid source answers again; no program concatenation occurs.
        corrected = final_answer(self.spans, '断电30分钟内上门。\n漏水1小时内到场。')
        client = CandidateClient(self.replies, [invalid[2], corrected])
        result, _ = match_bounded(client, self.questions, self.spans, chunk_chars=5)
        self.assertEqual(result[0].status, 'ready')
        self.assertEqual(client.reconciliation_calls, 2)
        self.assertEqual(len(client.matching_bodies), 2)
        self.assertEqual(client.reconciliation_bodies[1]['phase'], 'reconcile_correction')
        conflict = {'decisions': [asdict(Decision('q1', 'conflict', None, [], [], None, '存在冲突'))]}
        decisions, _ = match_bounded(CandidateClient([self.replies[0], conflict]), self.questions, self.spans, chunk_chars=5)
        self.assertEqual(decisions[0].status, 'conflict')
        for kind in ('multiple_choice', 'single_choice', 'boolean'):
            with self.subTest(kind=kind):
                question = Question('q1', '支持哪些服务？', kind, ['A','B'], ['t1'])
                replies = [{'decisions':[asdict(Decision('q1','ready',None,[option],[span.id],span.text,''))]}
                           for option,span in zip(['A','B'], self.spans)]
                result, _ = match_bounded(CandidateClient(replies, final_answer([], status='conflict')), [question], self.spans, chunk_chars=5)
                self.assertEqual(result[0].status, 'conflict')
        source = [SourceSpan('signed', {}, '公司已签署保密承诺书。', 'hash')]
        answer = Decision('q1','ready',None,['是'],['signed'],'公司已签署保密承诺书。','')
        for text, expected in [('公司是否已签署保密承诺书？',False), ('Have you signed the confidentiality commitment?',False),
                               ('请签署保密承诺书',False), ('公司是否已签署保密承诺书？请在下方签名。',False), ('Signature:',False)]:
            with self.subTest(text=text):
                # Free-text keywords are not a program evidence decision;
                # the final semantic review judges whether this is a fact or
                # a request to generate a new commitment/signature.
                self.assertEqual(requires_review(Question('q1',text,'boolean',['是','否'],['t1']), answer, source), expected)

    def test_large_sources_route_evidence_and_fall_back_without_false_conflict(self):
        q=Question('q1','Acme response time?','text',[],['t1'])
        spans=[SourceSpan(f's{i}',{},f'Unrelated policy {i}.','hash') for i in range(30)]
        spans[20]=SourceSpan('s20',{},'Acme response 30 minutes.','hash')
        class RoutedClient:
            def __init__(inner,fail_first=False):inner.calls=[];inner.fail_first=fail_first;inner.reviews=0
            def generate_json(inner,prompt,schema):
                body=json.loads(prompt.split('\n',1)[1]);inner.calls.append(body['phase'])
                if body['phase']=='answer_revision':return LLMResponse({'decisions':[dict(question_id='q1',status='not_found',value=None,option_ids=[],evidence=[],reason='本轮不修正')]})
                if body['phase']=='answer_support_review':
                    inner.reviews+=1
                    return LLMResponse({'reviews':[dict(question_id='q1',verdict='uncertain' if inner.fail_first and inner.reviews==1 else 'supported',transformations=[],calculations=[],reason='完整性核对')]})
                source=body['source_spans'][0]
                if source['id']=='s20':return LLMResponse({'decisions':[ready(source['text'],source['id'])]})
                return LLMResponse({'decisions':[asdict(Decision('q1','not_found',None,[],[],None,'无相关内容'))]})
        for fallback in (False,True):
            with self.subTest(fallback=fallback):
                client=RoutedClient(fallback);coverage={}
                result,_=match_bounded(client,[q],spans,chunk_chars=1,coverage=coverage)
                self.assertEqual(result[0].status,'ready')
                self.assertEqual(result[0].value,'Acme response 30 minutes.')
                if fallback:self.assertEqual(coverage['fallback_questions'],['q1'])
                else:self.assertLess(client.calls.count('answer_matching'),len(spans))
        # Equivalent candidates can reduce in bounded groups beyond the old 12 limit.
        class Merge:
            def generate_json(inner,prompt,schema):
                body=json.loads(prompt.split('\n',1)[1])
                return LLMResponse(final_answer([spans[20]], 'Acme response 30 minutes.'))
        from autofill.application.answering.match import reconcile_candidates
        value=Decision('q1','ready','Acme response 30 minutes.',[],['s20'],'Acme response 30 minutes.','')
        self.assertEqual(reconcile_candidates(Merge(),q,[value]*13,spans).value,value.value)
