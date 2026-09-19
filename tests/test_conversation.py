"""Acoustic boundaries must never be inferred from missing transcript text."""
import copy
import unittest
from test_semantic_store import fixture
from semantic.conversation import build_conversations


class ConversationTests(unittest.TestCase):
    def test_missing_words_do_not_mean_silence(self):
        sources, analyses = fixture()
        result = analyses[sources[0]['chunk_id']]['result']
        result['utterances'] = [result['utterances'][0], result['utterances'][-1]]
        result['utterances'][-1]['source_spans'][0].update(start_ms=290000,end_ms=295000)
        result['regions']=[{'start_ms':0,'end_ms':300000,'kind':'uncertain'}]
        docs=build_conversations(sources,analyses)
        self.assertEqual(len(docs),1)
        self.assertEqual(docs[0]['duration_seconds'],300)

    def test_only_confirmed_long_pause_splits(self):
        sources, analyses=fixture();r=analyses[sources[0]['chunk_id']]['result']
        r['utterances']=[r['utterances'][0],r['utterances'][-1]]
        r['utterances'][-1]['source_spans'][0].update(start_ms=290000,end_ms=295000)
        r['regions']=[{'start_ms':0,'end_ms':5300,'kind':'speech'},
                      {'start_ms':5300,'end_ms':289700,'kind':'silence'},
                      {'start_ms':289700,'end_ms':300000,'kind':'speech'}]
        self.assertEqual(len(build_conversations(sources,analyses)),2)

    def test_uncertain_empty_chunk_keeps_source(self):
        sources,analyses=fixture();other=copy.deepcopy(sources[0])
        other.update(chunk_id='0a8b33a7-225a-4d87-a451-04aa4b4b7f60',sequence=1,epoch=other['epoch']+300)
        analyses[other['chunk_id']]={'state':'succeeded','input_key':'next','result':{
            'utterances':[],'regions':[{'start_ms':0,'end_ms':300000,'kind':'uncertain'}]}}
        doc=build_conversations(sources+[other],analyses)[0]
        self.assertEqual(len(doc['chunks']),2)
        self.assertEqual(doc['duration_seconds'],600)
        self.assertTrue(any(r['chunk_id']==other['chunk_id'] for r in doc['regions']))

    def test_empty_region_result_is_not_silence(self):
        sources,analyses=fixture();analyses[sources[0]['chunk_id']]['result']={'utterances':[],'regions':[]}
        self.assertEqual(build_conversations(sources,analyses)[0]['status'],'needs_review')

if __name__=='__main__':unittest.main()
