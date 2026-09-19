"""Text-only provider transport with durable request accounting and no blind retries."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request
import uuid
from .store import TZ, atomic_json, encode, fingerprint, now


class CloudBlocked(RuntimeError):
    pass


class CloudUncertain(RuntimeError):
    pass


def cloud_settings(root):
    path = Path(root) / 'private' / 'semantic.json'
    if not path.exists(): return {'enabled': False}
    return json.loads(path.read_text()) .get('cloud', {'enabled': False})


def cloud_status(store, config):
    month = dt.datetime.now(TZ).strftime('%Y-%m')
    spent = 0
    if store.path.exists():
        with store.db() as db:
            spent = db.execute("SELECT coalesce(sum(coalesce(actual_usd,reserved_usd)),0) FROM provider_attempts WHERE month=? AND state!='not_processed'", (month,)).fetchone()[0]
    enabled = bool(config.get('enabled') and config.get('price_verified'))
    stale = False
    if enabled:
        try:
            verified = dt.datetime.fromisoformat(config['verified_at'])
            stale = verified.tzinfo is None or (dt.datetime.now(TZ)-verified).total_seconds()>7*86400
        except (KeyError, ValueError, TypeError):
            stale = True
        enabled = not stale
    return {'enabled': enabled, 'model': config.get('model'), 'spent_usd': round(spent, 6),
            'budget_usd': config.get('monthly_budget_usd', 5),
            'message': '仅发送必要文字进行整理' if enabled else ('报价核验已过期；本地文字和原音仍可使用' if stale else '云摘要待配置；本地文字与原音仍可使用')}


def request_json(store, config, messages, stage):
    if not config.get('enabled') or not config.get('price_verified'):
        raise CloudBlocked('模型和当前报价未核验，云摘要保持关闭')
    # Only this pre-existing service is permitted; config cannot exfiltrate the key elsewhere.
    if config.get('base_url') != 'https://api.siliconflow.cn/v1':
        raise CloudBlocked('摘要服务地址不在允许范围')
    model = config.get('model')
    if not model or not config.get('verified_at'):
        raise CloudBlocked('模型核验信息缺失')
    verified = dt.datetime.fromisoformat(config['verified_at'])
    if verified.tzinfo is None or (dt.datetime.now(TZ) - verified).total_seconds() > 7 * 86400:
        raise CloudBlocked('模型报价核验超过7天，重新核对后再启用')
    input_rate = float(config['input_usd_per_million']); output_rate = float(config['output_usd_per_million'])
    if input_rate < 0 or output_rate < 0 or input_rate > 100 or output_rate > 100:
        raise CloudBlocked('报价无效')
    if input_rate + output_rate > 0 and not config.get('provider_limit_verified'):
        raise CloudBlocked('付费摘要需要先核验供应商额度')
    body = {'model': model, 'messages': messages, 'temperature': 0.1,
            'max_tokens': min(8192, int(config.get('max_output_tokens', 4096))),
            'response_format': {'type': 'json_object'}, 'enable_thinking': False, 'stream': True,
            'stream_options': {'include_usage': True}}
    # UTF-8 bytes conservatively upper-bound typical BPE input tokens, plus chat wrapper allowance.
    input_bound = len(encode(messages).encode()) + 1024
    if input_bound > int(config.get('max_input_bound', 48000)):
        raise CloudBlocked('输入超过核验的模型窗口')
    reserved = (input_bound * input_rate + body['max_tokens'] * output_rate) / 1_000_000
    key = fingerprint({'body': body, 'stage': stage, 'prompt_version': 1})
    raw_path = store.directory / 'runs' / key / 'response.json'
    with store.db(True) as db:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute('SELECT * FROM provider_attempts WHERE input_key=?', (key,)).fetchone()
        if existing:
            if raw_path.exists():
                return parse_response(json.loads(raw_path.read_text()))
            raise CloudUncertain('相同请求已有未完成记录，需核查后才可重试')
        month = dt.datetime.now(TZ).strftime('%Y-%m')
        used = db.execute("SELECT coalesce(sum(coalesce(actual_usd,reserved_usd)),0) FROM provider_attempts WHERE month=? AND state!='not_processed'", (month,)).fetchone()[0]
        total = db.execute("SELECT coalesce(sum(coalesce(actual_usd,reserved_usd)),0) FROM provider_attempts WHERE state!='not_processed'").fetchone()[0]
        request_count = db.execute('SELECT count(*) FROM provider_attempts WHERE month=?', (month,)).fetchone()[0]
        if request_count >= min(1000, int(config.get('monthly_request_limit', 1000))):
            raise CloudBlocked('摘要请求次数达到月度上限')
        if used + reserved > min(5, float(config.get('monthly_budget_usd', 5))) or total + reserved > float(config.get('trial_budget_usd', 1)):
            raise CloudBlocked('摘要费用预留达到上限')
        request_id = str(uuid.uuid4())
        price = {k: config.get(k) for k in ('input_usd_per_million','output_usd_per_million','verified_at','price_source')}
        db.execute('INSERT INTO provider_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (request_id, key, month, reserved, None, 'reserved', model, encode(price), None, now(), None))
    atomic_json(raw_path.parent / 'request-manifest.json', {'id': request_id, 'input_key': key, 'stage': stage,
                'model': model, 'price': price, 'input_bound': input_bound, 'max_output_tokens': body['max_tokens']})
    try:
        key_file = Path(config['key_file']).expanduser().resolve()
        if key_file.parent != (store.root / 'private').resolve():
            raise CloudBlocked('凭据路径需在本机private目录内')
        secret = key_file.read_text().strip()
        request = urllib.request.Request(config['base_url'] + '/chat/completions', data=encode(body).encode(),
                   headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + secret}, method='POST')
        # Redirects must not forward the credential to an unexpected host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs): return None
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        deadline = time.monotonic() + 300
        content, usage, finish_reason, size, complete = [], {}, None, 0, False
        with opener.open(request, timeout=90) as response:
            if 'text/event-stream' in response.headers.get('Content-Type', ''):
                for line in response:
                    if time.monotonic() > deadline: raise TimeoutError('summary stream deadline')
                    size += len(line)
                    if size > 4_000_000: raise ValueError('response too large')
                    if not line.startswith(b'data:'): continue
                    payload = line[5:].strip()
                    if payload == b'[DONE]':
                        complete = True; break
                    event = json.loads(payload)
                    if event.get('usage'): usage = event['usage']
                    for choice in event.get('choices', []):
                        text = choice.get('delta', {}).get('content')
                        if text: content.append(text)
                        if choice.get('finish_reason'): finish_reason = choice['finish_reason']
                if not complete or finish_reason != 'stop':
                    raise ValueError('stream did not complete')
                raw = {'choices': [{'message': {'content': ''.join(content)}, 'finish_reason': finish_reason}], 'usage': usage}
            else:
                raw_bytes = response.read(4_000_001)
                if len(raw_bytes) > 4_000_000: raise ValueError('response too large')
                raw = json.loads(raw_bytes)
        atomic_json(raw_path, raw)
        usage = raw.get('usage', {})
        actual = None
        if isinstance(usage.get('prompt_tokens'), int) and isinstance(usage.get('completion_tokens'), int):
            actual = (usage['prompt_tokens']*input_rate + usage['completion_tokens']*output_rate)/1_000_000
        with store.db(True) as db:
            db.execute('UPDATE provider_attempts SET state=?,actual_usd=?,usage_json=? WHERE input_key=?',
                       ('response_saved', actual, encode(usage), key))
        return parse_response(raw)
    except urllib.error.HTTPError as exc:
        status = exc.code; exc.close()
        with store.db(True) as db:
            db.execute('UPDATE provider_attempts SET state=?,error=? WHERE input_key=?',
                       ('not_processed' if status == 429 else 'needs_review', 'HTTP ' + str(status), key))
        raise CloudUncertain('模型请求失败，记录已保留且不会自动重复发送') from None
    except Exception as exc:
        if 'content' in locals() and content and not raw_path.exists():
            # Retain incomplete output for diagnosis, never treat it as a reusable result.
            atomic_json(raw_path.parent / 'incomplete-response.json', {
                'content': ''.join(content), 'usage': usage, 'finish_reason': finish_reason,
                'complete': False, 'error_type': type(exc).__name__})
        with store.db(True) as db:
            db.execute("UPDATE provider_attempts SET state='needs_review',error=? WHERE input_key=?", (type(exc).__name__, key))
        raise CloudUncertain('请求或解析未完成，记录已保留待核查') from None


def parse_response(raw):
    choice = raw['choices'][0]
    if choice.get('finish_reason') not in (None, 'stop'):
        raise ValueError('summary response incomplete')
    content = choice['message']['content'].strip()
    if content.startswith('```'):
        content = content.split('\n', 1)[1].rsplit('```', 1)[0]
    result = json.loads(content)
    if not isinstance(result, dict): raise ValueError('summary must be a JSON object')
    return result
