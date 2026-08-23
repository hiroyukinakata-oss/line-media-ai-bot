import os
import threading
import json
import urllib.request
from urllib.parse import urlparse
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

def extract_x_text(url: str) -> str:
    """XのURLから投稿テキストを直接抽出する（API制限回避の裏技）"""
    try:
        parsed_url = urlparse(url)
        if parsed_url.netloc in ["twitter.com", "x.com", "www.twitter.com", "www.x.com"]:
            api_url = f"https://api.vxtwitter.com{parsed_url.path}"
            req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                text = data.get("text", "")
                return text
    except Exception:
        return ""
    return ""

def async_process_ai(user_id: str, url: str):
    # XのURLだった場合、専用機能でテキストを抽出する
    extracted_text = extract_x_text(url)

    if extracted_text:
        # Xからテキスト抽出成功した場合のプロンプト
        prompt = f"""
        以下のX（旧Twitter）の投稿内容（英語の場合は正確に和訳）を読み取り、以下の構成で分かりやすく要約・解説してください。

        【取得した投稿テキスト】
        {extracted_text}

        【1. 全体エグゼクティブサマリー】
        メインテーマと結論を3〜4行で簡潔にまとめてください。

        【2. 重要ポイント詳細解説】
        主要な論点、背景、手法、事実を箇条書きで具体的に解説してください。

        【3. 実践アクション・示唆】
        得られる教訓や具体的なアクションプランを整理してください。
        """
    else:
        # YouTube等、その他のURLの場合のプロンプト
        prompt = f"""
        以下のコンテンツ（URL: {url}）について、内容を分かりやすく要約・解説してください。英語の場合は自然な日本語に翻訳してください。

        【1. 全体エグゼクティブサマリー】
        メインテーマと結論を3〜4行で簡潔にまとめてください。

        【2. 重要ポイント詳細解説】
        主要な論点、背景、手法、事実を箇条書きで具体的に解説してください。

        【3. 実践アクション・示唆】
        得られる教訓や具体的なアクションプランを整理してください。
        """

    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt
        )
        summary_text = response.text

        messages = []
        chunk_size = 4000
        for i in range(0, len(summary_text), chunk_size):
            messages.append(TextMessage(text=summary_text[i:i+chunk_size]))
            if len(messages) >= 5:
                break

        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=messages
                )
            )
    except Exception as e:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=f"解析エラーが発生しました: {str(e)}")]
                )
            )

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
    user_id = event.source.user_id

    if not (user_text.startswith("http://") or user_text.startswith("https://")):
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="動画や記事のURL（YouTube、Xなど）を送信してください。\n自動でAI要約を行います！")]
                )
            )
        return

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="🔍 コンテンツを解析中です...\n少々お待ちください（15〜30秒程度）")]
            )
        )

    threading.Thread(target=async_process_ai, args=(user_id, user_text)).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
