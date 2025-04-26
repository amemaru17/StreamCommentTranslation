#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatTransrator.py

YouTube ライブチャット翻訳ツール（無料版）
Author: 雨丸 <deadhuman17@outlook.jp>
Created: 2025-04-22
Last Modified: 2025-04-27
Version: 1.0.1

Description:
  - 指定チャンネルのライブ配信チャットを自動検出し取得
  - googletrans / LibreTranslate のいずれかでリアルタイム翻訳
  - Tkinter GUI で表示＆CSV にログ出力
  - OBS-WebSocket 経由で配信画面にオーバーレイ（任意）

Usage:
  python chat_translator_auto.py

Requirements:
  - google-api-python-client
  - googletrans==4.0.0-rc1
  - requests
  - obs-websocket-py
  - tkinter (標準)

Modified Function Version:1.0.1 
  - 翻訳対象言語および翻訳除外言語を config.json で選択可能
  - コメント言語自動検出で除外言語コメントはスキップ
  - ThreadPoolExecutor を使った並列翻訳で高速化

"""
import os
import json
import threading
import time
import csv
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, font as tkfont
from datetime import datetime
import concurrent.futures

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

# フィルタリング設定
ENABLED_LANGS = cfg.get("enabled_langs", list(LANG_OPTIONS.values()))
DEFAULT_LANGS = cfg.get("default_langs", ENABLED_LANGS)
SKIP_LANGS    = cfg.get("skip_langs", [])
# 小文字で扱う除外リスト
SKIP_LANGS_LOWER = [lang.lower() for lang in SKIP_LANGS]

# === YouTube クライアント ===
youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

def get_live_video_id(channel_id: str) -> str:
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

        # 翻訳クライアント & ThreadPool
        self.trans_google = Translator()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

        # OBS WebSocket（任意）
        try:
            h = OBS_CONFIG.get("host", "localhost")
            p = OBS_CONFIG.get("port", 4455)
            w = OBS_CONFIG.get("password", "")
            self.obs = obsws(h, p, w)
            self.obs.connect()
        except Exception:
            self.obs = None

        # UI 用変数設定
        self.chk_vars = {
            name: tk.BooleanVar(value=(code in DEFAULT_LANGS))
            for name, code in LANG_OPTIONS.items()
            if code in ENABLED_LANGS
        }
        self.skip_vars = {
            name: tk.BooleanVar(value=(code in SKIP_LANGS))
            for name, code in LANG_OPTIONS.items()
            if code in ENABLED_LANGS
        }
        self.translator_var = tk.StringVar(value=list(TRANSLATORS.values())[0])
        self.is_running = False
        self.next_page_token = None

        # CSV ログ準備
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_file = open(f"chatlog_{ts}.csv", "w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(["timestamp", "author", "lang", "original", "translated"])

        # GUI 構築
        self.font = tkfont.Font(family="Consolas", size=11)
        self.colors = COLORS
        self._build_ui()

    def _build_ui(self):
        frm = ttk.Frame(self, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Button(frm, text="ライブを検出・開始", command=self.start).grid(row=0, column=0, padx=5)

        # 翻訳する言語
        ttk.Label(frm, text="翻訳する言語:").grid(row=1, column=0, sticky="nw", pady=(10,0))
        tf = ttk.Frame(frm)
        tf.grid(row=1, column=1, sticky="w", pady=(10,0))
        for i, (name, var) in enumerate(self.chk_vars.items()):
            ttk.Checkbutton(tf, text=name, variable=var).grid(row=0, column=i, padx=5)

        # 除外する言語
        ttk.Label(frm, text="除外する言語:").grid(row=2, column=0, sticky="nw", pady=(10,0))
        sf = ttk.Frame(frm)
        sf.grid(row=2, column=1, sticky="w", pady=(10,0))
        for i, (name, var) in enumerate(self.skip_vars.items()):
            ttk.Checkbutton(sf, text=name, variable=var).grid(row=0, column=i, padx=5)

        # 翻訳エンジン選択
        ttk.Label(frm, text="翻訳エンジン:").grid(row=3, column=0, sticky="w", pady=(10,0))
        ef = ttk.Frame(frm)
        ef.grid(row=3, column=1, sticky="w", pady=(10,0))
        for i, (lbl, val) in enumerate(TRANSLATORS.items()):
            ttk.Radiobutton(ef, text=lbl, variable=self.translator_var, value=val).grid(row=0, column=i, padx=5)

        # ログ表示
        self.txt_log = scrolledtext.ScrolledText(
            self, width=100, height=25, state="disabled", font=self.font
        )
        self.txt_log.grid(row=4, column=0, columnspan=2, padx=10, pady=(10,10))
        for tag, color in self.colors.items():
            self.txt_log.tag_configure(tag, foreground=color)

    def log(self, msg: str, tag="original"):
        def _append():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", msg + "\n", tag)
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        self.after(0, _append)

    def start(self):
        if self.is_running:
            return
        try:
            vid = get_live_video_id(CHANNEL_ID)
            chat_id = get_live_chat_id(vid)
        except Exception as e:
            messagebox.showerror("エラー", str(e))
            return
        self.is_running = True
        threading.Thread(target=self._poll_loop, args=(chat_id,), daemon=True).start()
        self.log(f"[INFO] ライブ検出 OK — videoId={vid}", tag="original")

    def _detect_language(self, text: str) -> str:
        det = self.trans_google.detect(text)
        return det.lang.lower()

    def _poll_loop(self, chat_id):
        seen = set()
        while self.is_running:
            resp = youtube.liveChatMessages().list(
                liveChatId=chat_id,
                part="authorDetails,snippet",
                pageToken=self.next_page_token or ""
            ).execute()
            self.next_page_token = resp.get("nextPageToken")
            interval = float(resp.get("pollingIntervalMillis", 2000)) / 1000

            for item in resp.get("items", []):
                if item["id"] in seen:
                    continue
                seen.add(item["id"])

                author   = item["authorDetails"]["displayName"]
                original = item["snippet"]["displayMessage"]
                ts       = datetime.now().isoformat()

                # 言語検出
                detected = self._detect_language(original)
                # 除外リスト UI と config の両方からチェック
                ui_skip = [code.lower() for name, code in LANG_OPTIONS.items()
                           if name in self.skip_vars and self.skip_vars[name].get()]
                if detected in SKIP_LANGS_LOWER or detected in ui_skip:
                    self.log(f"[SKIP] Detected '{detected}', skipping", tag="original")
                    self.csv_writer.writerow([ts, author, detected, original, original])
                    continue

                # 原文ログ
                self.log(f"▶ {author}: {original}", tag="original")
                self.csv_writer.writerow([ts, author, "original", original, original])

                # OBS オーバーレイ
                if self.obs:
                    try:
                        self.obs.call(obs_requests.SetText("LiveChatOverlay", f"{author}: {original}"))
                    except:
                        pass

                # 翻訳対象言語 filters
                target_codes = [code for name, code in LANG_OPTIONS.items()
                                if name in self.chk_vars and self.chk_vars[name].get()]
                futures = {self.executor.submit(self._translate_free, original, code): code
                           for code in target_codes}
                for fut in concurrent.futures.as_completed(futures):
                    code = futures[fut]
                    try:
                        tr = fut.result()
                    except Exception as e:
                        tr = f"[Error] {e}"
                    self.log(f"   → [{code}]: {tr}", tag=code)
                    self.csv_writer.writerow([ts, author, code, original, tr])

            time.sleep(interval)

    def _translate_free(self, text: str, target_lang: str) -> str:
        engine = self.translator_var.get()
        if engine == "googletrans":
            return self.trans_google.translate(text, dest=target_lang).text
        elif engine == "libre":
            r = requests.post(LIBRE_URL, data={
                "q": text,
                "source": "auto",
                "target": target_lang,
                "format": "text"
            })
            return r.json().get("translatedText", "")
        else:
            raise RuntimeError("Unsupported translator")

    def on_close(self):
        self.is_running = False
        time.sleep(0.5)
        if self.obs:
            try:
                self.obs.disconnect()
            except:
                pass
        self.csv_file.close()
        self.destroy()

if __name__ == "__main__":
    app = ChatTranslatorApp()
    app.mainloop()