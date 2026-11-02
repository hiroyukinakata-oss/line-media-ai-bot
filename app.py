import os
import threading
import json
import re
import uuid
import requests
from urllib.parse import urlparse
from flask import Flask, request, abort, render_template_string, Response
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

summary_db = {}

def get_expanded_url(url: str) -> str:
    try:
        with requests.get(url, timeout=10, stream=True, headers={'User-Agent': 'Mozilla/5.0'}) as res:
            return res.url
    except Exception:
        return url

def extract_x_data(url: str):
    text = ""
    expanded_urls = []
    try:
        parsed = urlparse(url)
        if parsed.netloc in ["twitter.com", "x.com", "www.twitter.com", "www.x.com"]:
            clean_path = parsed.path
            api_url = f"https://api.vxtwitter.com{clean_path}"
            with requests.get(api_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10) as res:
                if res.status_code == 200:
                    try:
                        data = res.json()
                        text = data.get("text", "")
                        if "qrtURL" in data: text += f" {data['qrtURL']}"
                        raw_urls = re.findall(r'(https?://[^\s]+)', text)
                        for r_url in raw_urls:
                            r_url = r_url.rstrip(').,;!?')
                            e_url = get_expanded_url(r_url)
                            if e_url not in expanded_urls:
                                expanded_urls.append(e_url)
                    except ValueError:
                        pass
    except Exception:
        pass
    return text, expanded_urls

def scrape_webpage(url: str) -> str:
    try:
        with requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10) as res:
            res.encoding = res.apparent_encoding
            soup = BeautifulSoup(res.text, 'html.parser')
            return soup.get_text(separator=' ', strip=True)
    except Exception as e:
        return f"（※記事取得失敗: {str(e)}）"

def get_youtube_transcript(url: str) -> str:
    try:
        match = re.search(r'(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|v/|shorts/|live/))([^&?/\s]+)', url)
        if not match: return "（※動画ID検出不可）"
        video_id = match.group(1)
        
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        try:
            # 英語または日本語の字幕を優先して取得
            transcript = transcript_list.find_transcript(['en', 'ja', 'en-US', 'en-GB'])
            return " ".join([t['text'] for t in transcript.fetch()])
        except:
            # 見つからなければ取得可能な最初の言語を返す
            for t in transcript_list:
                return " ".join([item['text'] for item in t.fetch()])
        return "（※字幕データが見つかりません）"
    except Exception:
        # エラーでクラッシュさせず、Xのテキスト解析へスムーズに移行する
        return "（※YouTube側のアクセス制限により字幕が取得できませんでした。Xの投稿本文や記事内容を中心に解析を行います）"

def async_process_ai(user_id: str, original_url: str, host_url: str):
    content_sources = []
    trace_log = [] 
    
    if "x.com" in original_url or "twitter.com" in original_url:
        x_text, extracted_urls = extract_x_data(original_url)
        if x_text:
            content_sources.append(f"【X(Twitter)の投稿内容】\n{x_text}")
        trace_log.append(f"🔍 検出URL: {extracted_urls}")
        
        for url in extracted_urls:
            if 'youtu' in url or 'youtube.com' in url:
                trace_log.append(f"▶ YouTube処理: {url}")
                ts = get_youtube_transcript(url)
                content_sources.append(f"【動画字幕({url})】\n{ts[:100000]}")
            else:
                trace_log.append(f"📄 外部記事処理: {url}")
                text = scrape_webpage(url)
                content_sources.append(f"【記事内容({url})】\n{text[:50000]}")
    else:
        trace_log.append(f"🔍 直接入力URL: {original_url}")
        if 'youtu' in original_url or 'youtube.com' in original_url:
            ts = get_youtube_transcript(original_url)
            content_sources.append(f"【動画字幕】\n{ts[:100000]}")
        else:
            text = scrape_webpage(original_url)
            content_sources.append(f"【記事内容】\n{text[:50000]}")

    content_sources.insert(0, f"【システム処理ログ】\n" + "\n".join(trace_log))
    combined_text = "\n\n".join(content_sources)

    # 越境EC事業・自動化を見据えた専用プロンプト
    prompt = f"""
    あなたは最先端のAIトレンドを分析し、事業実装をサポートする専門エージェントです。
    以下の抽出データ（Xの英語投稿、海外YouTube動画の字幕、Web記事など）を読み解き、予備知識のない状態でも完全に理解できる【深掘り解説レポート】を日本語で作成してください。

    主な目的は、この英語の最新情報を「越境ECや無在庫販売システム（eBay/Shopee等）の半自動化・AIエージェント活用事業」にどう応用できるかという視点を持つことです。

    【抽出データ】
    {combined_text}

    以下の構成でMarkdown形式で出力してください。
    # 1. 【要約】この情報が伝えたかった最大のテーマ
    # 2. 【詳細解説】具体的に何が語られていたか？（英語情報の日本語訳と徹底解剖）
    # 3. 【ビジネス応用】EC半自動化やAIエージェント構築への具体的な活用アイデア
    # 4. まとめとネクストアクション
    """
    
    try:
        response = gemini_client.models.generate_content(model='gemini-3.6-flash', contents=prompt)
        report_id = str(uuid.uuid4())
        summary_db[report_id] = {"summary": response.text, "raw_text": combined_text}
        report_url = f"{host_url.rstrip('/')}/report/{report_id}"
        reply_msg = f"✅ レポートが完成しました。\n以下の専用ページからご確認ください。\n{report_url}"
        
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=reply_msg)]))
    except Exception as e:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=f"エラー: {str(e)}")] ))

@app.route("/", methods=["GET"])
def health_check(): return "OK"

@app.route("/report/<report_id>", methods=["GET"])
def view_report(report_id):
    data = summary_db.get(report_id)
    if not data: return "<h1>レポートが見つかりません。</h1>", 404
    template = """
    <!DOCTYPE html>
    <html lang="ja">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AI 事業応用レポート</title>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <style>
            body { font-family: sans-serif; line-height: 1.8; padding: 15px; max-width: 900px; margin: 0 auto; background: #f4f7f6; color: #333;}
            .container { background: #fff; padding: 25px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.05); margin-bottom: 20px;}
            .action-card { background: #e8f5e9; border: 2px solid #a5d6a7; border-radius: 10px; padding: 15px; margin-bottom: 20px; }
            .action-title { font-weight: bold; font-size: 1.05em; color: #1b5e20; margin-bottom: 10px; }
            .btn-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }
            .btn { display: block; text-align: center; padding: 10px 8px; border-radius: 6px; font-weight: bold; font-size: 0.9em; text-decoration: none; cursor: pointer; border: none; transition: 0.2s; }
            .btn-primary { background: #2e7d32; color: #fff; }
            .btn-secondary { background: #37474f; color: #fff; }
            .btn-outline { background: #fff; color: #2e7d32; border: 1px solid #2e7d32; }
            h1 { font-size: 1.4em; border-bottom: 3px solid #3498db; padding-bottom: 8px; margin-top: 30px; color: #2c3e50;}
            h2 { font-size: 1.2em; border-left: 5px solid #3498db; padding-left: 12px; margin-top: 25px; color: #34495e;}
            #toast { visibility: hidden; min-width: 200px; background-color: #333; color: #fff; text-align: center; border-radius: 8px; padding: 12px; position: fixed; z-index: 9999; left: 50%; bottom: 30px; transform: translateX(-50%); font-size: 0.9em;}
            #toast.show { visibility: visible; }
        </style>
    </head>
    <body>
        <div class="action-card">
            <div class="action-title">⚡ アクションメニュー</div>
            <div class="btn-grid">
                <button class="btn btn-primary" onclick="copyText('summary')">📋 要約をコピー</button>
                <button class="btn btn-secondary" onclick="copyText('raw')">📋 生データをコピー</button>
                <a class="btn btn-outline" href="/download/{{ report_id }}/txt" target="_blank">📄 要約 (.txt) 保存</a>
                <a class="btn btn-outline" href="/download/{{ report_id }}/raw" target="_blank">📦 生データ 保存</a>
            </div>
        </div>
        <div class="container" id="content"></div>
        <div id="toast">コピーしました！</div>
        <textarea id="raw-markdown" style="display:none;">{{ summary }}</textarea>
        <textarea id="raw-data" style="display:none;">{{ raw_text }}</textarea>
        <script>
            const rawText = document.getElementById('raw-markdown').value;
            const rawData = document.getElementById('raw-data').value;
            document.getElementById('content').innerHTML = marked.parse(rawText);
            function showToast(msg) {
                const toast = document.getElementById("toast");
                toast.innerText = msg;
                toast.className = "show";
                setTimeout(() => { toast.className = toast.className.replace("show", ""); }, 2000);
            }
            function copyText(type) {
                const text = (type === 'summary') ? rawText : rawData;
                navigator.clipboard.writeText(text).then(() => { showToast(type === 'summary' ? "要約テキストをコピーしました！" : "生データをコピーしました！"); });
            }
        </script>
    </body>
    </html>
    """
    return render_template_string(template, report_id=report_id, summary=data["summary"], raw_text=data["raw_text"])

@app.route("/download/<report_id>/<file_type>", methods=["GET"])
def download_file(report_id, file_type):
    data = summary_db.get(report_id)
    if not data: return "Not Found", 404
    if file_type == "txt":
        content = data["summary"].replace("#", "").replace("*", "")
        filename = "ai_summary.txt"
    elif file_type == "raw":
        content = data["raw_text"]
        filename = "raw_extracted_data.txt"
    else: return "Bad Request", 400
    response = Response(content, mimetype="text/plain; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response

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
            MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="動画や記事のURLを送信してください。")]))
        return
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text="🔍 大元の動画・記事のデータを読み込み、徹底解説レポートを作成中です...\n（非常に深い内容を生成するため、1分〜2分ほどかかります。少々お待ちください）")]))
    threading.Thread(target=async_process_ai, args=(event.source.user_id, user_text, request.host_url)).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
