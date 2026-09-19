"""Conservative conversation candidates, independent of five-minute file boundaries."""
from __future__ import annotations
import datetime as dt
from .store import stable_id, fingerprint, TZ, recompute


def build_conversations(sources, analyses):
    groups, current = [], []
    for source in sources:
        if source.get('synthetic'):
            continue
        if current:
            previous = current[-1]
            gap = source['epoch'] - previous['epoch'] - previous['duration_seconds']
            if source['session_id'] != previous['session_id'] or source['sequence'] != previous['sequence'] + 1 or gap > 2 or gap < -2:
                groups.append(current); current = []
        current.append(source)
    if current:
        groups.append(current)
    output = []
    for sources in groups:
        chunks, utterances, regions = [], [], []
        all_ready, failed = True, False
        for s in sources:
            chunks.append({'chunk_id': s['chunk_id'], 'started_at': dt.datetime.fromtimestamp(s['epoch'], TZ).isoformat(),
                           'duration': s['duration_seconds'], 'sha256': s['sha256'], 'sequence': s['sequence'], 'session_id': s['session_id']})
            analysis = analyses.get(s['chunk_id'], {})
            if analysis.get('state') != 'succeeded':
                all_ready = False
                failed = failed or analysis.get('state') in ('failed', 'needs_review')
                continue
            result = analysis['result']
            for u in result.get('utterances', []):
                utterances.append({**u, 'epoch': s['epoch'] + u['source_spans'][0]['start_ms'] / 1000})
            regions.extend({**r, 'chunk_id': s['chunk_id']} for r in result.get('regions', []))
        utterances.sort(key=lambda u: (u['epoch'], u['id']))
        # Silence alone is not a conversation. Uncertain/no-ASR is visible as a quality issue.
        if all_ready and not utterances and regions and all(r['kind'] in ('silence', 'non_speech') for r in regions):
            continue
        epochs = {s['chunk_id']: s['epoch'] for s in sources}
        quiet = sorted((epochs[r['chunk_id']] + r['start_ms']/1000,
                        epochs[r['chunk_id']] + r['end_ms']/1000)
                       for r in regions if r['kind'] in ('silence', 'non_speech'))
        def confirmed_pause(left, right):
            # Missing ASR text is not acoustic silence. Require coverage of the
            # whole interior gap, with at most the 300 ms VAD padding at either end.
            if right-left < 90: return False
            cursor=left+0.3
            for start,end in quiet:
                if end<=cursor: continue
                if start>cursor: return False
                cursor=max(cursor,end)
                if cursor>=right-0.3: return True
            return False
        parts, part = [], []
        for u in utterances:
            if part:
                prev = part[-1]
                prev_end = max(epochs[s['chunk_id']] + s['end_ms']/1000 for s in prev['source_spans'])
                if confirmed_pause(prev_end, u['epoch']):
                    parts.append(part); part = []
            part.append(u)
        if part: parts.append(part)
        if not parts: parts = [[]]
        for index, us in enumerate(parts):
            anchor = sources[0]['chunk_id'] if index == 0 else us[0]['id']
            cid = stable_id('conversation:' + sources[0]['session_id'] + ':' + anchor)
            doc = {'id': cid, 'title': '对话 · 正在本地转写' if not all_ready else '对话 · 待整理',
                   'overview': '原音已保留，正在生成带时间戳的文字。' if not all_ready else '本地文字已生成，等待主题整理。',
                   'started_at': chunks[0]['started_at'],
                   'ended_at': dt.datetime.fromtimestamp(sources[-1]['epoch'] + sources[-1]['duration_seconds'], TZ).isoformat(),
                   'duration_seconds': sources[-1]['epoch'] + sources[-1]['duration_seconds'] - sources[0]['epoch'],
                   'speech_seconds': 0, 'tags': [], 'topic_count': 0,
                   'status': 'needs_review' if failed or (all_ready and not us) else ('transcribed' if all_ready else 'pending'),
                   'boundary_status': '暂定边界 · 可能仍有录音待同步' if len(parts) == 1 else '长停顿后的候选边界 · 可合并',
                   'chunks': chunks, 'utterances': us, 'regions': regions, 'topics': [], 'key_points': [],
                   'decisions': [], 'action_candidates': [], 'open_questions': [], 'source_sessions': [sources[0]['session_id']]}
            if us:
                doc['topics'] = [{'id': stable_id(cid + ':unclassified'), 'title': '待归类', 'key_points': [], 'utterance_ids': [u['id'] for u in us]}]
                recompute(doc)
                if len(parts) == 1:
                    # Keep uncertain / empty-ASR source chunks reachable from the
                    # whole conversation, including its leading and trailing audio.
                    doc['chunks'] = chunks
                    doc['regions'] = regions
                    doc['started_at'] = chunks[0]['started_at']
                    doc['ended_at'] = dt.datetime.fromtimestamp(sources[-1]['epoch'] + sources[-1]['duration_seconds'], TZ).isoformat()
                    doc['duration_seconds'] = sources[-1]['epoch'] + sources[-1]['duration_seconds'] - sources[0]['epoch']
            if all_ready and not us:
                doc['title'] = '录音 · 文字待核对'
                doc['overview'] = '保留了疑似人声，但未识别到可靠文字。可回听原音。'
            # Source provenance and model versions participate in invalidation.
            doc['input_key'] = fingerprint({'sources': [(s['chunk_id'], s['sha256'], analyses.get(s['chunk_id'], {}).get('input_key')) for s in sources],
                                            'utterances': us, 'group_version': 2, 'part': index})
            output.append(doc)
    return output
