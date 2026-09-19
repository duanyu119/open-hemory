import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac'))
from semantic.quality import annotate_utterances

class QualityTests(unittest.TestCase):
    def make(self,text,i):return {'id':str(i),'text':text,'source_spans':[{'sha256':'a'*64,'chunk_id':'source','start_ms':i*1000,'end_ms':i*1000+1000}]}
    def test_repeated_decoder_phrase_annotates_without_deleting(self):
        values=[self.make('这是解码重复短语'*6,0),self.make('保留有效讨论',1)]
        output=annotate_utterances(values)
        self.assertEqual(len(output),2);self.assertEqual(output[0]['text'],values[0]['text'])
        self.assertTrue(output[0]['exclude_from_summary']);self.assertNotIn('exclude_from_summary',values[0])
        self.assertNotIn('exclude_from_summary',output[1])
    def test_short_acknowledgements_are_preserved(self):
        values=[self.make('嗯好',i) for i in range(8)]
        self.assertTrue(all(not u.get('exclude_from_summary') for u in annotate_utterances(values)))
    def test_teacher_review_requires_source_hash_match(self):
        values=[self.make('待核对的原文',0)]
        review={'0':{'source_sha256':['b'*64],'reasons':['教师评测疑似异常']}}
        self.assertNotIn('exclude_from_summary',annotate_utterances(values,review)[0])
        review['0']['source_sha256']=['a'*64]
        self.assertTrue(annotate_utterances(values,review)[0]['exclude_from_summary'])

if __name__=='__main__':unittest.main()
