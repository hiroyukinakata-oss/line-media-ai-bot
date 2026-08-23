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
            return soup.get_text(separator=' ', strip=True)
    except Exception:
        return ""

def async_process_ai(user_id: str, original_url: str, host_url: str):
    content_sources = []
    x_text = extract_x_text(original_url)

    # ★変更点：文字数上限を300,000文字（約10時間分）、Web記事を100,000文字に引き上げ
    if x_text:
        content_sources.append(f"【Xの投稿内容】\n{x_text}")
        urls = re.findall(r'(https?://[^\s]+)', x_text)
        for url in urls:
            if 'youtu' in url or 'youtube.com' in url:
                ts = get_youtube_transcript(url)
                if ts: content_sources.append(f"【動画字幕（講義内容のすべて）】\n{ts[:300000]}")
            else:
                text = scrape_webpage(url)
                if text: content_sources.append(f"【記事内容】\n{text[:100000]}")
    else:
        if 'youtu' in original_url or 'youtube.com' in original_url:
            ts = get_youtube_transcript(original_url)
            if ts: content_sources.append(f"【動画字幕（講義内容のすべて）】\n{ts[:300000]}")
        else:
            text = scrape_webpage(original_url)
            if text: content_sources.append(f"【記事内容】\n{text[:100000]}")

    combined_text = "\n\n".join(content_sources) if content_sources else f"URL: {original_url}"

    prompt = f"""
    以下の抽出データ（Xの投稿、長時間の動画字幕、Web記事など）の内容をベースに、超特大ボリュームで、予備知識のない小学生や初心者でも「なるほど！そういう仕組みなんだ！」と完璧に理解できるレベルの【詳細な深掘り解説レポート】を作成してください。

    出力文字数や時間に制限はありません。出し惜しみせず、単なる「目次の紹介」ではなく、「動画や記事の中で具体的に語られていた中身の授業」を、具体例や例え話を交えて徹底的に解説してください。

    【抽出データ】
    {combined_text}

    以下の構成でMarkdown形式で出力してください。

    # 1. 【超図解】この記事・動画が伝えたかった最大のテーマ
    （なぜこの技術や話題が今すごいのか、何が画期的なのかを、わかりやすい例え話を使って解説してください。）

    # 2. 【深掘り解説】具体的に何が語られていたか？（重要ポイントの徹底解剖）
    （動画や記事の中で解説されている具体的な技術、仕組み、アーキテクチャ、学習プロセスなどを、一つ一つ丁寧に、初心者向けに噛み砕いて長文で解説してください。用語の解説も必ず入れてください。例えば「データ収集」や「ポストトレーニング」とは具体的に何をしているのか、講義の中身を抽出して語ってください。）

    # 3. 最新のAI・エージェント動向と、私たちへの影響
    （このコンテンツから読み取れる、最新のAIトレンドや、今後のAI社会がどうなっていくのかという洞察を深く記載してください。）

    # 4. 今後のアクション・まとめ
    （この情報を踏まえて、読者は何を学ぶべきかを提示してください。）
    """
    
    try:
        response = gemini_client.models.generate_content(model='gemini-3.6-flash', contents=prompt)
        report_id = str(uuid.uuid4())
        summary_db[report_id] = response.text
        report_url = f"{host_url.rstrip('/')}/report/{report_id}"
        
        reply_msg = f"✅ 【深掘り解説】の生成が完了しました。\n以下の専用ページから詳細レポートをご確認ください。\n{report_url}"
        
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
        return "<h1>レポートが見つかりません。再生成してください。</h1>", 404
        
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AI 深掘り解説レポート</title>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <style>
            body { font-family: "Helvetica Neue", Arial, "Hiragino Kaku Gothic ProN", "Hiragino Sans", Meiryo, sans-serif; line-height: 1.8; padding: 20px; max-width: 900px; margin: 0 auto; background: #f4f7f6; color: #333;}
            .container { background: #fff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }
            h1 { font-size: 1.6em; border-bottom: 3px solid #3498db; padding-bottom: 10px; margin-top: 40px; color: #2c3e50;}
            h2 { font-size: 1.3em; border-left: 5px solid #3498db; padding-left: 15px; margin-top: 35px; color: #34495e;}
            h3 { font-size: 1.1em; color: #7f8c8d; margin-top: 25px;}
            p { font-size: 1.05em; margin-bottom: 20px; }
            ul, ol { font-size: 1.05em; margin-bottom: 20px; padding-left: 25px;}
            li { margin-bottom: 10px; }
            strong { color: #e74c3c; background: transparent;}
            code { background: #f8f9fa; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; color: #e83e8c;}
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
                messages=[TextMessage(text="動画や記事のURLを送信してください。")]
            ))
        return

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text="🔍 大元の動画・記事のデータを読み込み、徹底解説レポートを作成中です...\n（4時間超えの動画など非常に深い内容を生成する場合、2〜3分ほどかかる場合があります）")]
        ))
        
    threading.Thread(target=async_process_ai, args=(event.source.user_id, user_text, request.host_url)).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
