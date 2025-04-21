"""
ChatTransrator.py

YouTube ライブチャット翻訳ツール（無料版）
Author: 雨丸 <deadhuman17@outlook.jp>
Created: 2025-04-22
Last Modified: 2025-04-22
Version: 1.0.0

Description:
  - 指定チャンネルのライブ配信チャットを自動検出し取得
  - googletrans / LibreTranslate のいずれかでリアルタイム翻訳
  - Tkinter GUI で表示＆CSV にログ出力
  - OBS-WebSocket 経由で配信画面にオーバーレイ（任意）

Usage:
  python ChatTransrator.py

Requirements:
  - google-api-python-client
  - googletrans==4.0.0-rc1
  - requests
  - obs-websocket-py
  - tkinter (標準ライブラリ)
"""

import os
import json
import threading
import time
import csv
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font as tkfont
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests
from googleapiclient.discovery import build
from googletrans import Translator
from obswebsocket import obsws, requests as obs_requests

# === 設定ファイル読み込み ===
with open("config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)

YOUTUBE_API_KEY = cfg["youtube_api_key"]
CHANNEL_ID      = cfg["channel_id"]
OBS_CONFIG      = cfg.get("obs", {})
LANG_OPTIONS    = cfg.get("lang_options", {})
TRANSLATORS     = cfg.get("translators", {})
LIBRE_URL       = cfg.get("libre_url")
COLORS          = cfg.get("colors", {})

# === YouTube クライアント ===
youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

def get_live_video_id(channel_id: str) -> str:
    """
    Search API で現在ライブ中の動画を取得。
    └ eventType='live', type='video'
    """
    resp = youtube.search().list(
        part="id",
        channelId=channel_id,
        eventType="live",
        type="video",
        maxResults=1
    ).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("現在ライブ配信中の動画が見つかりません")
    return items[0]["id"]["videoId"]

def get_live_chat_id(video_id: str) -> str:
    resp = youtube.videos().list(
        part="liveStreamingDetails",
        id=video_id
    ).execute()
    details = resp["items"][0]["liveStreamingDetails"]
    chat_id = details.get("activeLiveChatId")
    if not chat_id:
        raise RuntimeError("ライブチャットID を取得できませんでした")
    return chat_id

class ChatTranslatorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Auto Live Chat Translator (Free)")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Translator Clients
        self.trans_google = Translator()

        # OBS WebSocket（任意）
        try:
            h = OBS_CONFIG.get("host","localhost")
            p = OBS_CONFIG.get("port",4455)
            w = OBS_CONFIG.get("password","")
            self.obs = obsws(h,p,w)
            self.obs.connect()
        except Exception:
            self.obs = None

        # UI 変数
        self.chk_vars       = { name: tk.BooleanVar(value=(code in ("ja","en","id")))
                                for name,code in LANG_OPTIONS.items() }
        self.translator_var = tk.StringVar(value=list(TRANSLATORS.values())[0])
        self.is_running     = False
        self.next_page_token= None

        # CSV ログ準備
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_file   = open(f"chatlog_{ts}.csv","w",newline="",encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["timestamp","author","lang","original","translated"])

        # GUI
        self.font   = tkfont.Font(family="Consolas", size=11)
        self.colors=COLORS
        self._build_ui()

    def _build_ui(self):
        frm = ttk.Frame(self,padding=10); frm.grid(row=0,column=0,sticky="nsew")

        # ライブ自動取得ボタン
        ttk.Button(frm, text="ライブを検出・開始", command=self.start).grid(row=0, column=0, padx=5)

        # 翻訳言語チェック
        ttk.Label(frm, text="翻訳先言語:").grid(row=1,column=0,sticky="nw", pady=(10,0))
        lf = ttk.Frame(frm); lf.grid(row=1,column=1,sticky="w", pady=(10,0))
        for i,(n,v) in enumerate(self.chk_vars.items()):
            ttk.Checkbutton(lf,text=n,variable=v).grid(row=0,column=i,padx=5)

        # 翻訳エンジン選択
        ttk.Label(frm,text="翻訳エンジン:").grid(row=2,column=0,sticky="w", pady=(10,0))
        ef=ttk.Frame(frm); ef.grid(row=2,column=1,sticky="w", pady=(10,0))
        for i,(lbl,val) in enumerate(TRANSLATORS.items()):
            ttk.Radiobutton(ef,text=lbl,variable=self.translator_var,value=val).grid(row=0,column=i,padx=5)

        # ログ表示
        self.txt_log = scrolledtext.ScrolledText(
            self,width=100,height=25,state="disabled",font=self.font
        )
        self.txt_log.grid(row=1,column=0,columnspan=2,padx=10,pady=(60,10))
        for tag,color in self.colors.items():
            self.txt_log.tag_configure(tag, foreground=color)

    def log(self, msg:str, tag="original"):
        def _append():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", msg+"\n", tag)
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.after(0,_append)

    def start(self):
        if self.is_running: return
        try:
            vid = get_live_video_id(CHANNEL_ID)
            chat_id = get_live_chat_id(vid)
        except Exception as e:
            messagebox.showerror("エラー", str(e))
            return

        self.is_running=True
        threading.Thread(target=self._poll_loop, args=(chat_id,), daemon=True).start()
        self.log(f"[INFO] ライブ検出 OK — videoId={vid}", tag="original")

    def _poll_loop(self, chat_id):
        seen=set()
        while self.is_running:
            resp = youtube.liveChatMessages().list(
                liveChatId=chat_id,
                part="authorDetails,snippet",
                pageToken=self.next_page_token or ""
            ).execute()
            self.next_page_token = resp.get("nextPageToken")
            interval = float(resp.get("pollingIntervalMillis",2000))/1000

            for item in resp.get("items",[]):
                mid=item["id"]
                if mid in seen: continue
                seen.add(mid)

                author   = item["authorDetails"]["displayName"]
                original = item["snippet"]["displayMessage"]
                ts       = datetime.now().isoformat()

                # ログ & CSV
                self.log(f"▶ {author}: {original}", tag="original")
                self.csv_writer.writerow([ts,author,"original",original,original])

                # OBS overlay（任意）
                if self.obs:
                    try: self.obs.call(obs_requests.SetText("LiveChatOverlay",f"{author}: {original}"))
                    except: pass

                # 翻訳
                targets=[c for n,c in LANG_OPTIONS.items() if self.chk_vars[n].get()]
                for lang in targets:
                    tr=self._translate_free(original, lang)
                    self.log(f"   → [{lang}]: {tr}", tag=lang)
                    self.csv_writer.writerow([ts,author,lang,original,tr])

            time.sleep(interval)

    def _translate_free(self, text, target_lang):
        engine=self.translator_var.get()
        if engine=="googletrans":
            return self.trans_google.translate(text, dest=target_lang.lower()).text
        elif engine=="libre":
            r=requests.post(LIBRE_URL, data={
                "q":text,"source":"auto","target":target_lang,"format":"text"
            })
            return r.json().get("translatedText","")
        else:
            raise RuntimeError("未対応の翻訳エンジンです")

    def on_close(self):
        self.is_running=False
        time.sleep(0.5)
        try: self.obs.disconnect()
        except: pass
        self.csv_file.close()
        self.destroy()

if __name__ == "__main__":
    app=ChatTranslatorApp()
    app.mainloop()
