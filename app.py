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
        jina_url = f"https://r.jina.ai/{url}"
        headers = {
            "Accept": "text/plain",
            "X-Return-Format": "markdown",
            "X-No-Cache": "true"
        }
        res = requests.get(jina_url, headers=headers, timeout=25)
        if res.status_code == 200:
            text = res.text
            raw_urls = re.findall(r'(https?://[^\s]+)', text)
            for r_url in raw_urls:
                r_url = r_url.rstrip(').,;!?*')
                if 't.co' in r_url or 'youtube' in r_url or 'youtu.be' in r_url:
                    e_url = get_expanded_url(r_url)
                    if e_url not in expanded_urls and e_url != url:
                        expanded_urls.append(e_url)
    except Exception as e:
        text = f"（※Xデータ抽出エラー: {str(e)}）"
    return text, expanded_urls

def scrape_webpage(url: str) -> str:
    try:
        jina_url = f"https://r.jina.ai/{url}"
        res = requests.get(jina_url, headers={"Accept": "text/plain"}, timeout=20)
        if res.status_code == 200 and len(res.text) > 50:
            return res.text
    except Exception:
        pass
        
    try:
        with requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10) as res:
            res.encoding = res.apparent_encoding
            soup = BeautifulSoup(res.text, 'html.parser')
            return soup.get_text(separator=' ', strip=True)
    except Exception as e:
        return f"（※記事の取得に失敗しました: {str(e)}）"

def get_youtube_transcript(url: str) -> str:
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers = {
            "Accept": "text/plain",
            "X-Return-Format": "markdown",
            "X-No-Cache": "true"
        }
        res = requests.get(jina_url, headers=headers, timeout=45)
        
        if res.status_code == 200:
            text = res.text
            if len(text) > 100:
                return text.strip()
                
        return f"（※抽出APIエラー: ステータス {res.status_code}）"
    except Exception as e:
        return f"（※通信エラー: {str(e)}）"

def async_process_ai(user_id: str, original_url: str, host_url: str):
    content_sources = []
    trace_log = [] 
    has_valid_content = False
    
    if "x.com" in original_url or "twitter.com" in original_url:
        x_text, extracted_urls = extract_x_data(original_url)
        if x_text and not x_text.startswith("（※"):
            content_sources.append(f"【Xの投稿内容】\n{x_text}")
            has_valid_content = True
        trace_log.append(f"🔍 検出された内部URL: {extracted_urls}")
        
        for url in extracted_urls:
            if 'youtu' in url or 'youtube.com' in url:
                trace_log.append(f"▶ YouTubeデータ抽出: {url}")
                ts = get_youtube_transcript(url)
                if not ts.startswith("（※"):
                    content_sources.append(f"【動画データ({url})】\n{ts[:300000]}")
                    has_valid_content = True
            else:
                trace_log.append(f"📄 外部記事抽出: {url}")
                text = scrape_webpage(url)
                if not text.startswith("（※"):
                    content_sources.append(f"【記事内容({url})】\n{text[:100000]}")
                    has_valid_content = True
    else:
        trace_log.append(f"🔍 直接入力されたURL: {original_url}")
        if 'youtu' in original_url or 'youtube.com' in original_url:
            ts = get_youtube_transcript(original_url)
            if not ts.startswith("（※"):
                content_sources.append(f"【動画データ({original_url})】\n{ts[:300000]}")
                has_valid_content = True
        else:
            text = scrape_webpage(original_url)
            if not text.startswith("（※"):
                content_sources.append(f"【記事内容({original_url})】\n{text[:100000]}")
                has_valid_content = True
        
    if not has_valid_content:
        reply_msg = "⚠️ エラー：有効なテキストデータを取得できませんでした。処理を中断します。"
        with ApiClient(configuration) as api_
