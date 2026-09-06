from http.server import BaseHTTPRequestHandler
import json
import asyncio
import edge_tts
import io

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            text = data.get('text', '').strip()
            voice = data.get('voice', 'tr-TR-AhmetNeural')

            if not text:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Metin boş"}).encode('utf-8'))
                return

            async def generate_speech():
                communicate = edge_tts.Communicate(text, voice)
                out = io.BytesIO()
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        out.write(chunk["data"])
                return out.getvalue()

            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            audio_bytes = loop.run_until_complete(generate_speech())

            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(audio_bytes)

        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
