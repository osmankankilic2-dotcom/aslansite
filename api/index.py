from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
import asyncio
import edge_tts
import io

app = Flask(__name__)
CORS(app)

@app.route('/api/tts', methods=['POST'])
def tts():
    try:
        data = request.get_json(force=True, silent=True) or {}
        text = data.get('text', '').strip()
        voice = data.get('voice', 'tr-TR-AhmetNeural')

        if not text:
            return jsonify({"error": "Metin boş"}), 400

        async def generate():
            communicate = edge_tts.Communicate(text, voice)
            out = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    out.write(chunk["data"])
            out.seek(0)
            return out

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        audio_io = loop.run_until_complete(generate())
        loop.close()

        return send_file(
            audio_io,
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name="speech.mp3"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

