"""Worker boundary integration with isolated SQLite and injected local functions."""
from array import array
import contextlib
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mac'))
from semantic.boundary import reconcile_boundaries
from semantic.store import SemanticStore, source_rows
import semantic_worker as worker


class WorkerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SemanticStore(self.root, initialize=True)
        self.session = str(uuid.uuid4())
        self.ids = []
        self.sha = hashlib.sha256(b'fixture-only').hexdigest()
        self.config = {'local': {}, 'cloud': {'enabled': True, 'model': 'fixture'}}
        self.calls = []
        self.summaries = []
        with contextlib.closing(sqlite3.connect(self.root / 'queue.sqlite3')) as db, db:
            db.execute('CREATE TABLE chunks(chunk_id TEXT,metadata TEXT,duration REAL,status TEXT)')
        self.add_chunk()
        self.add_chunk()

    def tearDown(self):
        self.temp.cleanup()

    def add_chunk(self):
        cid = str(uuid.uuid4())
        sequence = len(self.ids)
        self.ids.append(cid)
        path = self.root / 'chunks' / cid / (cid + '.m4a')
        path.parent.mkdir(parents=True)
        path.write_bytes(b'fixture-only')
        meta = {'chunk_id': cid, 'session_id': self.session, 'sequence': sequence, 'sha256': self.sha,
                'started_at': (dt.datetime(2026, 9, 19, 12, tzinfo=dt.timezone(dt.timedelta(hours=8)))
                               + dt.timedelta(seconds=sequence)).isoformat()}
        with contextlib.closing(sqlite3.connect(self.root / 'queue.sqlite3')) as db, db:
            db.execute('INSERT INTO chunks VALUES(?,?,?,?)', (cid, json.dumps(meta), 1, 'done'))
        return cid

    def analyzer(self, path, source, config):
        return {'duration_ms': 1000, 'regions': [{'start_ms': 0, 'end_ms': 1000, 'kind': 'speech'}],
                'utterances': [{'id': source['chunk_id'] + '-u', 'text': '片' + str(source['sequence']),
                                'source_spans': [{'chunk_id': source['chunk_id'], 'sha256': self.sha,
                                                  'start_ms': 0, 'end_ms': 1000}]}]}

    def summary(self, store, doc, config):
        self.summaries.append(copy.deepcopy(doc))
        output = copy.deepcopy(doc)
        output.update(status='ready', title='fixture summary')
        return output

    def reconciler(self, sources, analyses, config, root):
        self.calls.append(tuple(s['chunk_id'] for s in sources))
        originals = [analyses[s['chunk_id']]['result']['utterances'][0] for s in sources]
        ids = [u['id'] for u in originals]
        return {'utterances': [{'id': 'joined-' + sources[0]['chunk_id'],
                                'text': ''.join(u['text'] for u in originals),
                                'source_spans': [copy.deepcopy(u['source_spans'][0]) for u in originals],
                                'boundary_status': 'reconciled', 'review_required': False,
                                'replaces_ids': ids, 'related_ids': ids}],
                'replaced_ids': ids, 'metrics': {'asr_calls': 1}}

    def run_worker(self, reconciler=None, cloud=False):
        with patch.object(worker, 'summarize', self.summary):
            worker.process(self.store, self.config, no_cloud=not cloud,
                           analyzer=self.analyzer, boundary_reconciler=reconciler)

    def originals(self):
        self.run_worker()
        return source_rows(self.root), self.store.analyses()

    def view(self, sources, analyses, local=None, model=None, reconciler=None, manual=()):
        return worker.boundary_view(self.store, sources, analyses, local or {}, model or {},
                                    reconciler or self.reconciler, manual)

    def utterances(self, view):
        return [u for a in view.values() for u in a['result']['utterances']]

    def reviews(self):
        return [json.loads(p.read_text()) for p in (self.store.directory / 'runs').glob('*/boundary-review.json')]

    def test_real_boundary_contract_with_stub_pcm_and_transcriber(self):
        sources, analyses = self.originals()
        def local_boundary(pair, entries, config, root):
            return reconcile_boundaries(pair, entries, {
                '_boundary_audio_loader': lambda source, start, end, config, root: array('f', [0.0]) * ((end-start)*16),
                '_boundary_transcriber': lambda pcm, config: {'segments': [{'start': 0, 'end': 2, 'text': '片0片1'}]},
            }, root)
        view = self.view(sources, analyses, reconciler=local_boundary)
        us = self.utterances(view)
        self.assertEqual(len(us), 1)
        self.assertEqual(us[0]['boundary_status'], 'reconciled')
        self.assertEqual([s['chunk_id'] for s in us[0]['source_spans']], self.ids)
        self.assertEqual([(s['start_ms'], s['end_ms']) for s in us[0]['source_spans']], [(0, 1000), (0, 1000)])
        self.assertEqual(self.store.analyses(), analyses)

    def test_reconciled_view_enters_summary_once_without_changing_analyses(self):
        _, analyses = self.originals()
        for _ in range(4):
            self.run_worker(self.reconciler, cloud=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.summaries), 1)
        self.assertEqual(len(self.summaries[0]['utterances']), 1)
        self.assertEqual(self.summaries[0]['utterances'][0]['text'], '片0片1')
        self.assertEqual(self.store.analyses(), analyses)
        doc = self.store.detail(self.store.list()['items'][0]['id'])
        self.assertEqual(doc['status'], 'ready')
        self.assertEqual(doc['duration_seconds'], 2)
        self.assertEqual(len(doc['chunks']), 2)

    def test_context_candidates_stay_out_of_transcript_and_summary(self):
        def candidate(*args):
            result = self.reconciler(*args)
            result['utterances'][0].update(boundary_status='context_candidate',
                                           text='不能进入主摘要的候选', replaces_ids=[])
            # Even an inconsistent top-level list must not authorize candidate removal.
            return result
        self.run_worker(candidate, cloud=True)
        self.assertEqual([u['text'] for u in self.summaries[0]['utterances']], ['片0', '片1'])
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.reviews()[0]['utterances'][0]['text'], '不能进入主摘要的候选')

    def test_appending_chunk_only_computes_new_adjacent_pair(self):
        sources, analyses = self.originals()
        self.view(sources, analyses)
        self.view(sources, analyses)
        self.assertEqual(len(self.calls), 1)
        self.add_chunk()
        sources, analyses = self.originals()
        self.view(sources, analyses)
        self.assertEqual(self.calls, [tuple(self.ids[:2]), tuple(self.ids[1:])])

    def test_cache_invalidates_only_pairs_with_changed_original_identity(self):
        self.add_chunk()
        sources, analyses = self.originals()
        self.view(sources, analyses)
        self.assertEqual(len(self.calls), 2)
        changed = copy.deepcopy(analyses)
        changed[self.ids[0]]['input_key'] += '-changed'
        self.view(sources, changed)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1], tuple(self.ids[:2]))
        changed[self.ids[0]]['result']['utterances'][0]['text'] = 'changed while key is stable'
        self.view(sources, changed)
        self.assertEqual(len(self.calls), 4)
        changed[self.ids[0]]['result']['metrics']['elapsed_seconds'] += 100
        self.view(sources, changed)
        self.assertEqual(len(self.calls), 4)

    def test_model_config_and_boundary_code_each_invalidate_cache(self):
        sources, analyses = self.originals()
        self.view(sources, analyses)
        self.view(sources, analyses, local={'boundary_context_ms': 5000})
        self.view(sources, analyses, model={'model_hash': 'new'})
        real_read = Path.read_bytes
        def changed_module(path):
            content = real_read(path)
            return content + b'\n# fixture code identity' if path.name == 'boundary.py' else content
        with patch.object(Path, 'read_bytes', changed_module):
            self.view(sources, analyses)
        self.assertEqual(len(self.calls), 4)

    def test_adjacent_cached_replacements_cannot_delete_same_original_twice(self):
        self.add_chunk()
        sources, analyses = self.originals()
        view = self.view(sources, analyses)
        us = self.utterances(view)
        self.assertEqual({u['id'] for u in us}, {'joined-' + self.ids[0], self.ids[2] + '-u'})
        review = self.reviews()[0]['utterances'][0]
        self.assertEqual(review['boundary_status'], 'context_candidate')
        self.assertEqual(review['replaces_ids'], [])
        self.assertEqual(review['reason'], 'conflicting_cached_boundaries')
        self.assertEqual(self.view(sources, analyses), view)
        self.assertEqual(len(self.calls), 2)

    def test_manual_card_preserved_and_late_chunk_not_absorbed_by_boundary(self):
        self.run_worker()
        doc = self.store.detail(self.store.list()['items'][0]['id'])
        self.store.correction(doc['id'], doc['revision'], 'rename', {'title': '人工保留'})
        manual = self.store.detail(doc['id'])
        late = self.add_chunk()
        for _ in range(3):
            self.run_worker(self.reconciler, cloud=True)
        self.assertEqual(self.store.detail(doc['id']), manual)
        self.assertEqual(len(self.store.list()['items']), 2)
        self.assertEqual(len(self.summaries), 1)
        self.assertEqual({s['chunk_id'] for u in self.summaries[0]['utterances'] for s in u['source_spans']}, {late})
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(u['boundary_status'] == 'context_candidate' for r in self.reviews() for u in r['utterances']))

    def test_split_then_merge_preserves_human_cards_and_supplements(self):
        self.run_worker()
        original = self.store.detail(self.store.list()['items'][0]['id'])
        self.store.correction(original['id'], original['revision'], 'split',
                              {'at_utterance_id': self.ids[1] + '-u'})
        human = {item['id']: self.store.detail(item['id']) for item in self.store.list()['items']}
        late = self.add_chunk()
        for _ in range(2):
            self.run_worker(self.reconciler)
        self.assertEqual(len(self.store.list()['items']), 3)
        for cid, doc in human.items():
            self.assertEqual(self.store.detail(cid), doc)
        supplement = next(self.store.detail(i['id']) for i in self.store.list()['items'] if i['id'] not in human)
        self.assertEqual([u['id'] for u in supplement['utterances']], [late + '-u'])
        target = human[original['id']]
        self.store.correction(target['id'], target['revision'], 'merge',
                              {'other_id': supplement['id'], 'other_revision': supplement['revision']})
        merged = self.store.detail(target['id'])
        for _ in range(2):
            self.run_worker(self.reconciler)
        self.assertEqual(len(self.store.list()['items']), 2)
        self.assertEqual(self.store.detail(target['id']), merged)
        self.assertFalse(self.store.detail(supplement['id'])['active'])

    def test_cache_unavailable_analysis_becomes_eligible_when_it_succeeds(self):
        sources, analyses = self.originals()
        pending = copy.deepcopy(analyses)
        pending[self.ids[0]].update(state='pending', result=None)
        calls = []
        def reconcile_when_ready(pair, entries, config, root):
            calls.append(True)
            if any(a['state'] != 'succeeded' for a in entries.values()):
                return {'utterances': [], 'replaced_ids': [], 'metrics': {}}
            return self.reconciler(pair, entries, config, root)
        self.view(sources, pending, reconciler=reconcile_when_ready)
        view = self.view(sources, analyses, reconciler=reconcile_when_ready)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.utterances(view)), 1)

    def test_pair_error_is_cached_without_repeated_local_inference(self):
        sources, analyses = self.originals()
        calls = []
        def error_result(*args):
            calls.append(True)
            return {'utterances': [], 'replaced_ids': [], 'metrics': {'errors': [{'error_type': 'ValueError'}]}}
        for _ in range(3):
            self.assertEqual(self.view(sources, analyses, reconciler=error_result), analyses)
        self.assertEqual(len(calls), 1)

    def test_injected_analyzer_never_selects_real_boundary_model(self):
        with patch('semantic.boundary.reconcile_boundaries', side_effect=AssertionError('real model called')):
            self.run_worker()


if __name__ == '__main__':
    unittest.main()
