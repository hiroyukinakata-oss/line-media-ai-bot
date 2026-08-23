import os
import sys
import time
import hashlib
import tempfile
import yt_dlp
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai
from google.genai.errors import APIError

app = Flask(__name__)

# 環境変数から設定を取得
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

def get_audio(url: str, output_dir: str) -> str:
    url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:10]
    out_path = os.path.join(output_dir, f"audio_{url_hash}.mp3")

    ydl_opts = {
        'format': 'ba/b',
        'outtmpl': os.path.join(output_dir, f"audio_{url_hash}.%(ext)s"),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '64',
        }],
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return out_path

def process_ai(file_path: str, prompt: str) -> str:
    uploaded_file = gemini_client.files.upload(file=file_path)
    
    while True:
        status = gemini_client.files.get(name=uploaded_file.name)
        if "ACTIVE" in str(status.state).upper():
            break
        elif "FAILED" in str(status.state).upper():
            raise Exception("AI側での音声解析に失敗しました。")
        time.sleep(3)

    response = gemini_client.models.generate_content(
        model='gemini-3.6-flash',
        contents=[uploaded_file, prompt],
    )
    gemini_client.files.delete(name=uploaded_file.name)
    return response.text

@app.route("/", methods=["GET"])
def health_check():
    return "LINE Media AI Bot is running!"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    
    if not (user_text.startswith("http://") or user_text.startswith("https://")):
        reply_msg = "動画のURL（YouTubeまたはX）を送信してください。\n自動で解析して要約・文字起こしを行います！"
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_msg)]
                )
            )
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            audio_path = get_audio(user_text, temp_dir)
            
            prompt = """
            添付された音声の内容を正確に理解し、以下の構成で自然な日本語で分かりやすく要約・解説してください。

            【1. 全体エグゼクティブサマリー】
            動画全体のメインテーマと結論を3〜4行で簡潔にまとめてください。

            【2. 重要ポイント詳細解説】
            語られている主要な論点、手法、事実を箇条書きで具体的に解説してください。英語の場合は自然な日本語に翻訳して解説してください。

            【3. 実践アクション・示唆】
            得られる教訓や具体的なアクションプランを整理してください。
            """
            
            summary_text = process_ai(audio_path, prompt)
            
            # LINEの1吹き出し上限（5000文字）に合わせて分割送信対応
            messages = []
            chunk_size = 4000
            for i in range(0, len(summary_text), chunk_size):
                messages.append(TextMessage(text=summary_text[i:i+chunk_size]))
                if len(messages) >= 5:
                    break

            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=messages
                    )
                )

        except Exception as e:
            with ApiClient(configuration) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"処理中にエラーが発生しました: {str(e)}")]
                    )
                )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
