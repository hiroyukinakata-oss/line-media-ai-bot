import os
import threading
import json
import urllib.request
import re
import uuid
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, abort, render_template_string
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai
from youtube_transcript_api import YouTubeTranscriptApi
from bs4 import BeautifulSoup

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 生成されたレポートを一時保存するインメモリデータベース
summary_db = {}

def extract_x_text(url: str) -> str:
    try:
        parsed = urlparse(url)
        if parsed.netloc in ["twitter.com", "x.com", "www.twitter.com", "www.x.com"]:
            api_url = f"https://api.vxtwitter.com{parsed.path}"
            req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as res:
                return json.loads(res.read().decode()).get("text", "")
    except Exception:
        pass
    return ""

def get_youtube_transcript(url: str) -> str:
    try:
        parsed = urlparse(url)
        video_id = None
        if parsed.netloc in ["youtu.be"]:
            video_id = parsed.path.lstrip('/')
        elif parsed.netloc in ["www.youtube.com", "youtube.com"]:
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        
        if video_id:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=['ja', 'en'])
            return " ".join([t['text'] for t in transcript])
    except Exception:
        pass
    return ""

def scrape_webpage(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as res:
            soup = BeautifulSoup(res.read().decode('utf-8'), 'html.parser')
            return soup.get_text(separator=' ', strip=True)[:5000]
    except Exception:
        return ""

def async_process_ai(user_id: str, original_url: str, host_url: str):
    content_sources = []
    x_text = extract_x_text(original_url)

    if x_text:
        content_sources.append(f"【Xの投稿内容】\n{x_text}")
        urls = re.findall(r'(https?://[^\s]+)', x_text)
        for url in urls:
            if 'youtu' in url:
                ts = get_youtube_transcript(url)
                if ts: content_sources.append(f"【動画字幕】\n{ts[:10000]}")
            else:
                text = scrape_webpage(url)
                if text: content_sources.append(f"【記事内容】\n{text}")
    else:
        if 'youtu' in original_url:
            ts = get_youtube_transcript(original_url)
            if ts: content_sources.append(f"【動画字幕】\n{ts[:10000]}")
        else:
            text = scrape_webpage(original_url)
            if text: content_sources.append(f"【記事内容】\n{text}")

    combined_text = "\n\n".join(content_sources) if content_sources else f"URL: {original_url}"

    prompt = f"""
    以下の抽出データを基に、コンテンツの全体像を正確に把握し、詳細に要約・解説してください。
    英語は自然な日本語に翻訳し、出力は構造化されたMarkdown形式（## などの見出しを活用）にしてください。

    {combined_text}

    ## 1. 全体エグゼクティブサマリー
    （メインテーマと結論）

    ## 2. 重要ポイント詳細解説
    （主要な論点、背景、事実を箇条書きで）

    ## 3. 実践アクション・示唆
    （得られる教訓やアクションプラン）
    """
    
    try:
        response = gemini_client.models.generate_content(model='gemini-3.6-flash', contents=prompt)
        report_id = str(uuid.uuid4())
        summary_db[report_id] = response.text
        report_url = f"{host_url.rstrip('/')}/report/{report_id}"
        
        reply_msg = f"✅ 解析が完了しました。\n以下の専用ページから詳細レポートをご確認ください。\n{report_url}"
        
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=reply_msg)]
            ))
    except Exception as e:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(
                to=user_id, messages=[TextMessage(text=f"エラー: {str(e)}")]
            ))

@app.route("/", methods=["GET"])
def health_check(): return "OK"

@app.route("/report/<report_id>", methods=["GET"])
def view_report(report_id):
    content = summary_db.get(report_id)
    if not content:
        return "<h1>レポートが見つかりません</h1>", 404
        
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AI 要約レポート</title>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <style>
            body { font-family: -apple-system, sans-serif; line-height: 1.6; padding: 20px; max-width: 800px; margin: 0 auto; background: #f9f9f9; color: #333;}
            .container { background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h2 { border-bottom: 1px solid #eee; padding-bottom: 5px; margin-top: 30px; color: #2c3e50;}
        </style>
    </head>
    <body>
        <div class="container" id="content"></div>
        <textarea id="raw-markdown" style="display:none;">{{ content }}</textarea>
        <script>
            document.getElementById('content').innerHTML = marked.parse(document.getElementById('raw-markdown').value);
        </script>
    </body>
    </html>
    """
    return render_template_string(template, content=content)

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    if not user_text.startswith("http"):
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="URL（YouTube、Xなど）を送信してください。")]
            ))
        return

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="🔍 隠れたリンク先も含めて解析中です...\n少々お待ちください（30〜60秒）")]
        ))
        
    threading.Thread(target=async_process_ai, args=(event.source.user_id, user_text, request.host_url)).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
