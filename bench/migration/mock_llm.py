"""OpenAI 互換の模擬 LLM。0.7.2 の Open WebUI に本物の形のチャットを作らせるためだけに使う。
/v1/models と /v1/chat/completions（ストリームあり・なし）に応える。標準ライブラリだけで動く。
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = (
    'ご質問の件ですが、結論から言うと二つ目の方法をおすすめします。\n\n'
    'The migration reads every chat row and expands the message history into a new table.\n\n'
    '```python\nfor row in rows:\n    total += row["count"]\nprint(total)\n```\n'
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({'object': 'list', 'data': [{'id': 'mock-model', 'object': 'model', 'owned_by': 'mock'}]})

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        usage = {'prompt_tokens': 120, 'completion_tokens': 80, 'total_tokens': 200}
        base = {'id': 'chatcmpl-mock', 'created': int(time.time()), 'model': req.get('model', 'mock-model')}
        if not req.get('stream'):
            # タイトル生成などの裏の処理は JSON を期待するので、それらしい JSON を返す
            text = json.dumps({'title': '模擬の会話', 'tags': ['test'], 'follow_ups': ['続きは?'], 'queries': []}, ensure_ascii=False)
            self._json({**base, 'object': 'chat.completion', 'usage': usage,
                        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text}}]})
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        for i in range(0, len(ANSWER), 12):
            chunk = {**base, 'object': 'chat.completion.chunk',
                     'choices': [{'index': 0, 'delta': {'content': ANSWER[i:i + 12]}, 'finish_reason': None}]}
            self.wfile.write(f'data: {json.dumps(chunk)}\n\n'.encode())
            self.wfile.flush()
            time.sleep(0.02)
        end = {**base, 'object': 'chat.completion.chunk', 'usage': usage,
               'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}
        self.wfile.write(f'data: {json.dumps(end)}\n\ndata: [DONE]\n\n'.encode())
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args):
        pass


ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
