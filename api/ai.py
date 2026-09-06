from http.server import BaseHTTPRequestHandler
import json
import os
import time
import hashlib
import urllib.request

# Rate limiting: daily 8 requests per IP
RATE_LIMIT = 8
RATE_WINDOW = 86400  # 24 hours in seconds
RATE_FILE = "/tmp/ai_rate_limits.json"

def get_client_ip(handler):
    forwarded = handler.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return handler.client_address[0]

def check_rate_limit(ip):
    try:
        if os.path.exists(RATE_FILE):
            with open(RATE_FILE, 'r') as f:
                data = json.load(f)
        else:
            data = {}
    except:
        data = {}

    now = time.time()
    if ip not in data:
        data[ip] = {'count': 0, 'start': now}

    entry = data[ip]
    if now - entry['start'] > RATE_WINDOW:
        entry = {'count': 0, 'start': now}

    if entry['count'] >= RATE_LIMIT:
        return False, entry['count']

    entry['count'] += 1
    data[ip] = entry

    try:
        with open(RATE_FILE, 'w') as f:
            json.dump(data, f)
    except:
        pass

    return True, entry['count']

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))

            message = data.get('message', '').strip()
            if not message:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Mesin boş"}).encode('utf-8'))
                return

            ip = get_client_ip(self)
            allowed, count = check_rate_limit(ip)

            if not allowed:
                self.send_response(429)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    "error": "Günlük 8 mesaj limitine ulaşıldı. Yarın tekrar deneyin.",
                    "remaining": 0
                }).encode('utf-8'))
                return

            api_key = os.environ.get('GROQ_API_KEY', '')
            if not api_key:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "API anahtarı yapılandırılmamış"}).encode('utf-8'))
                return

            # Call Groq API
            groq_payload = json.dumps({
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "Sen aslan AI'sin. Kibar, bilgili ve samimi bir yapay zeka asistanısın. Türkçe yanıt veriyorsun."},
                    {"role": "user", "content": message}
                ],
                "temperature": 0.7,
                "max_tokens": 1024
            }).encode('utf-8')

            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=groq_payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )

            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    groq_response = json.loads(resp.read().decode('utf-8'))
                    reply = groq_response.get('choices', [{}])[0].get('message', {}).get('content', 'Yanıt alınamadı.')
            except urllib.error.HTTPError as e:
                error_body = e.read().decode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"Groq API hatası: {e.code}"}).encode('utf-8'))
                return

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                "reply": reply,
                "remaining": RATE_LIMIT - count
            }).encode('utf-8'))

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()