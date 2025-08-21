#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# core.py — Telecloner (menus legíveis + busca; mídia enviada como mídia)

import asyncio
import os
import contextlib
import json
import sys
import time
import traceback
import html
import shutil
import getpass
import re

from pathlib import Path
from typing import Dict, Optional, Any, Tuple, List
from datetime import datetime, timezone
from tkinter import Tk, filedialog
from bs4 import BeautifulSoup
from telethon.errors import RPCError, FloodWaitError
from telethon.tl.functions.channels import GetForumTopicsRequest
from telethon.tl.types import Channel, Message
from telethon import TelegramClient

# ───────────────────── 0. UTILITÁRIOS ─────────────────────
def clear_screen():
    """Limpa a tela do terminal."""
    os.system('cls' if os.name == 'nt' else 'clear')

def pause(msg="Pressione ENTER para continuar..."):
    try:
        input(msg)
    except (EOFError, KeyboardInterrupt):
        pass

# ───────────────────── 1. CREDENCIAIS ─────────────────────
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CRED_FILE = DATA_DIR / "creds.json"

def load_creds() -> Tuple[int, str, str]:
    if CRED_FILE.exists():
        d = json.loads(CRED_FILE.read_text("utf-8"))
        return d["api_id"], d["api_hash"], d["session"]
    while True:
        try:
            api_id = int(input("🔑 API ID Telegram: "))
            break
        except ValueError:
            print("❌ API ID deve ser número.")
    api_hash = getpass.getpass("🔑 API HASH Telegram: ").strip()
    session = input("📁 Nome da sessão: ").strip() or "minha_conta"
    CRED_FILE.write_text(json.dumps({
        "api_id": api_id, "api_hash": api_hash, "session": session
    }, indent=2), encoding="utf-8")
    print(f"✅ Credenciais salvas em {CRED_FILE}\n")
    return api_id, api_hash, session

api_id, api_hash, session_name = load_creds()

# ───────────────────── 2. CONFIGS GERAIS ─────────────────────
CHECKPOINT_FILE = "checkpoint.json"
BAR_LEN, SLOTS = 30, 5
DELAY_BETWEEN_UPLOADS = 2
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".3gp", ".avi"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".flac", ".wav"}

# ─────────────── 3. HTML TEMPLATE ───────────────
HTML_HEAD_TPL = (
    "<!DOCTYPE html><html lang='pt-br'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>{title}</title>"
    "<style>body{{background:#f0f4f7;font-family:-apple-system,BlinkMacSystemFont,"
    "'Segoe UI',sans-serif;margin:0;padding:10px}}"
    ".chat-container{{display:flex;flex-direction:column;gap:12px;max-width:800px;margin:auto}}"
    ".message{{background:#fff;border-radius:18px;padding:8px 15px;max-width:95%;word-wrap:break-word;"
    "box-shadow:0 1px 2px rgba(0,0,0,.1)}}"
    ".sent{{align-self:flex-end;background:#e1ffc7}}"
    ".received{{align-self:flex-start}}"
    ".sender{{font-weight:bold;color:#3b8ac4;margin-bottom:4px}}"
    ".content{{font-size:1rem}}"
    ".timestamp{{font-size:.75rem;color:#888;text-align:right;margin-top:5px}}"
    ".btn{{display:inline-block;margin-top:6px;padding:4px 8px;border:1px solid #3b8ac4;border-radius:6px;"
    "background:#fff;color:#3b8ac4;font-size:.8rem;text-decoration:none}}"
    ".btn:hover{{background:#3b8ac4;color:#fff}}</style></head>"
    "<body><div class='chat-container'>"
)
HTML_FOOT = "</div></body></html>"

# ───────────────────── 4. GLOBAIS DE PROGRESSO ─────────────────────
dl_size = dl_done = 0
time_start = time.time()
bar_lock = asyncio.Lock()

# ───────────────────── 5. AUXILIARES ─────────────────────
def sanitize(t: str, n: int = 150) -> str:
    s = re.sub(r"[^\w\s\-.()]+", "_", t).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:n] or "sem_nome").strip()

def permalink(ent: Channel, mid: int) -> str:
    return (
        f"https://t.me/{ent.username}/{mid}"
        if getattr(ent, "username", None)
        else f"https://t.me/c/{str(abs(ent.id)).removeprefix('100')}/{mid}"
    )

def load_ckpt(path: Path) -> Dict:
    fp = path / CHECKPOINT_FILE
    if fp.exists():
        with contextlib.suppress(Exception):
            return json.loads(fp.read_text("utf-8"))
    return {"done_ids": [], "bytes": 0}

def save_ckpt(path: Path, ck: Dict):
    with contextlib.suppress(Exception):
        (path / CHECKPOINT_FILE).write_text(json.dumps(ck, ensure_ascii=False, indent=2))

def ask_directory() -> Optional[Path]:
    Tk().withdraw()
    folder = filedialog.askdirectory()
    return Path(folder) if folder else None

# ───────────────────── 6. LISTAGEM LEGÍVEL + BUSCA ─────────────────────
def _paginate(items: List[Any], per_page: int, page: int) -> Tuple[List[Any], int]:
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    i0 = (page - 1) * per_page
    i1 = i0 + per_page
    return items[i0:i1], total_pages

def _print_header(title: str, subtitle: Optional[str] = None):
    clear_screen()
    print("=" * 72)
    print(f"{title}".center(72))
    if subtitle:
        print(subtitle.center(72))
    print("=" * 72)
    print()

def _print_list(lines: List[str]):
    # Mais espaçamento e alinhamento; duas colunas quando couber
    cols = shutil.get_terminal_size((100, 24)).columns
    gap = 6
    if cols >= 100:
        col_w = (cols - gap) // 2
        for i in range(0, len(lines), 2):
            left = lines[i]
            right = lines[i + 1] if i + 1 < len(lines) else ""
            print(left.ljust(col_w) + (" " * gap) + right)
    else:
        for ln in lines:
            print(ln)
    print()

async def list_dialogs(client: TelegramClient) -> List[Any]:
    # Apenas grupos e canais (inclui supergrupos)
    dialogs = [d for d in await client.get_dialogs(limit=None) if (d.is_group or d.is_channel)]
    # Ordena por título “humanizado”
    def _name(d):
        ent = d.entity
        return (getattr(ent, "title", None) or getattr(ent, "username", "") or str(ent.id)).lower()
    dialogs.sort(key=_name)
    return dialogs

def _title_of_dialog(d) -> str:
    ent = d.entity
    title = getattr(ent, "title", None) or getattr(ent, "username", None) or str(ent.id)
    return str(title)

async def select_dialog_with_search(client: TelegramClient, prompt_title: str) -> Optional[Any]:
    """
    Lista chats/grupos/canais com:
      • paginação (20 por página)
      • busca case-insensitive por título (digite '/texto')
      • voltar com 'b'
    Retorna o objeto Dialog.entity selecionado ou None ao voltar/cancelar.
    """
    per_page = 20
    dialogs = await list_dialogs(client)
    filtered = dialogs[:]  # lista corrente (pode ser reduzida pela busca)
    page = 1

    while True:
        _print_header(prompt_title, "Digite NÚMERO para selecionar • '/texto' para buscar • n/p para navegar • b para voltar")
        show, total_pages = _paginate(filtered, per_page, page)
        if not show:
            print("Nenhum chat/grupo encontrado.\n")
        lines = []
        base_idx = (page - 1) * per_page
        for i, d in enumerate(show, 1):
            idx = base_idx + i
            name = sanitize(_title_of_dialog(d), 60)
            lines.append(f"[{idx:3d}]  {name}")
        _print_list(lines)
        print(f"Página {page}/{total_pages}\n")

        try:
            s = input("➡️  Escolha: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not s:
            # ENTER cancela (voltar)
            return None

        if s.lower() in ("b", "voltar"):
            return None
        if s.lower() in ("n", "next", ">"):
            if page < total_pages: page += 1
            continue
        if s.lower() in ("p", "prev", "<"):
            if page > 1: page -= 1
            continue
        if s.startswith("/"):
            term = s[1:].strip().lower()
            if not term:
                filtered = dialogs[:]
            else:
                filtered = [d for d in dialogs if term in _title_of_dialog(d).lower()]
            page = 1
            continue
        if s.isdigit():
            sel = int(s)
            i0 = (page - 1) * per_page
            # Permite selecionar tanto pelo índice global mostrado quanto pelo relativo na página
            if i0 < sel <= i0 + len(show):
                return show[sel - i0 - 1].entity
            # fallback: seleção absoluta na lista filtrada
            if 1 <= sel <= len(filtered):
                return filtered[sel - 1].entity
            print("❌ Índice fora do intervalo.")
            pause()
            continue

        print("❌ Entrada inválida. Use número, '/busca', n/p, ou 'b' para voltar.")
        pause()

async def get_topics(client: TelegramClient, chan: Channel) -> Dict[int, str]:
    topics = {0: "Geral"}
    try:
        ent = await client.get_input_entity(chan)
        off_id = off_tid = 0
        off_date = datetime.now(timezone.utc)
        while True:
            res = await client(GetForumTopicsRequest(
                channel=ent, offset_date=off_date,
                offset_id=off_id, offset_topic=off_tid, limit=100
            ))
            if not res.topics:
                break
            for t in res.topics:
                topics[int(t.id)] = t.title or f"Tópico {t.id}"
            last = res.topics[-1]
            off_id, off_tid, off_date = last.top_message, int(last.id), last.date
            if len(res.topics) < 100:
                break
    except RPCError:
        pass
    return topics

async def select_topic_with_search(client: TelegramClient, chan: Channel, prompt_title: str) -> Tuple[int, str]:
    """
    Seleciona tópico com:
      • paginação (20 por página)
      • busca '/texto'
      • opção 'b' para voltar (retorna (0,'Geral') como padrão)
    Retorna (topic_id, topic_title).
    """
    tops = await get_topics(client, chan)
    items: List[Tuple[int, str]] = list(tops.items())  # [(id, title), ...]
    # Ordena por título, mantendo Geral (0) no topo
    base = [(tid, tname) for tid, tname in items if tid != 0]
    base.sort(key=lambda x: (x[1] or "").lower())
    items = [(0, "Geral")] + base

    per_page = 20
    filtered = items[:]  # pode sofrer busca
    page = 1

    while True:
        _print_header(prompt_title, "Digite NÚMERO • '/texto' para buscar • n/p para navegar • b para voltar")
        show, total_pages = _paginate(filtered, per_page, page)
        if not show:
            print("Nenhum tópico encontrado.\n")
        lines = []
        base_idx = (page - 1) * per_page
        for i, (tid, tname) in enumerate(show, 1):
            idx = base_idx + i
            lines.append(f"[{idx:3d}]  {sanitize(tname, 60)}  (id={tid})")
        _print_list(lines)
        print(f"Página {page}/{total_pages}\n")

        try:
            s = input("➡️  Escolha: ").strip()
        except (EOFError, KeyboardInterrupt):
            return (0, "Geral")

        if not s:
            return (0, "Geral")
        if s.lower() in ("b", "voltar"):
            return (0, "Geral")
        if s.lower() in ("n", "next", ">"):
            if page < total_pages: page += 1
            continue
        if s.lower() in ("p", "prev", "<"):
            if page > 1: page -= 1
            continue
        if s.startswith("/"):
            term = s[1:].strip().lower()
            if not term:
                filtered = items[:]
            else:
                filtered = [(tid, tname) for (tid, tname) in items if term in (tname or "").lower()]
            page = 1
            continue
        if s.isdigit():
            sel = int(s)
            i0 = (page - 1) * per_page
            if i0 < sel <= i0 + len(show):
                return show[sel - i0 - 1]
            if 1 <= sel <= len(filtered):
                return filtered[sel - 1]
            print("❌ Índice fora do intervalo.")
            pause()
            continue

        print("❌ Entrada inválida. Use número, '/busca', n/p, ou 'b' para voltar.")
        pause()

# ───────────────────── 7. BARRA DE PROGRESSO ─────────────────────
async def refresh_download_bar(topic: str):
    async with bar_lock:
        elapsed = max(1e-6, time.time() - time_start)
        speed = dl_done / elapsed
        speed_k = speed / 1024
        pct = (dl_done / dl_size * 100) if dl_size else 0
        bar_len = int(BAR_LEN * pct / 100)
        bar = '█' * bar_len + '-' * (BAR_LEN - bar_len)
        remain = dl_size - dl_done
        eta = remain / speed if speed else 0
        h, m, s = int(eta // 3600), int((eta % 3600) // 60), int(eta % 60)
        sys.stdout.write(
            f"\rBaixando {sanitize(topic)[:28]:28} |{bar}| {pct:6.2f}% "
            f"{speed_k:8.2f} KB/s ETA {h:02d}:{m:02d}:{s:02d}"
        )
        sys.stdout.flush()

# ───────────────────── 8A. GERAR chat.html (SEM DOWNLOAD) ─────────────────────
async def generate_html_only(client: TelegramClient, grp: Channel,
                             tid: Optional[int], tname: str) -> Path:
    base = Path(sanitize(grp.title))
    base.mkdir(exist_ok=True)
    tdir = base / sanitize(tname)
    tdir.mkdir(exist_ok=True)
    mdir = tdir / "media"
    mdir.mkdir(exist_ok=True)
    html_path = tdir / "chat.html"

    print(f"\n📝 Gerando chat.html de '{tname}' (sem novos downloads)…")
    all_msgs = [m async for m in client.iter_messages(grp, reverse=True)]
    if tid:
        msgs = [m for m in all_msgs if m.id == tid or getattr(m, "reply_to_msg_id", None) == tid]
    else:
        msgs = all_msgs

    total = len(msgs)
    pad = len(str(total))

    html_path.write_text(HTML_HEAD_TPL.format(title=html.escape(tname)), "utf-8")
    for seq, msg in enumerate(msgs, 1):
        if msg.file:
            ext = msg.file.ext or ""
            orig = sanitize(msg.file.name) if msg.file.name else f"media{ext}"
            fname = f"{str(seq).zfill(pad)}_{orig}"
            file_exists = (mdir / fname).exists()
        else:
            ext = fname = ""
            file_exists = False

        sender = await msg.get_sender()
        sname = html.escape(
            (f"{getattr(sender,'first_name','')} {getattr(sender,'last_name','')}".strip())
            or getattr(sender, "username", "?")
        )
        cont = html.escape(msg.text or "").replace("\n", "<br>")

        img_tag = ""
        media_btn = ""
        if msg.file:
            if ext.lower() in IMG_EXTS and file_exists:
                img_tag = f"<img src='media/{fname}' style='max-width:100%;border-radius:8px;margin:6px 0'>"
            media_btn = (
                f"<a href='media/{fname}' class='btn'>{html.escape(fname)}</a> "
                if file_exists else
                "<a class='btn' style='opacity:0.5;text-decoration:line-through'>MÍDIA AUSENTE</a> "
            )

        ts = msg.date.astimezone().strftime("%d/%m/%Y %H:%M")

        with html_path.open("a", encoding="utf-8") as h:
            h.write(
                f"<div class='message {'sent' if msg.out else 'received'}'>"
                f"<div class='sender'>{sname}</div>"
                f"<div class='content'>{cont}</div>"
                f"{img_tag}{media_btn}"
                f"<a href='{permalink(grp,msg.id)}' class='btn'>Link</a>"
                f"<div class='timestamp'>{ts}</div></div>\n"
            )

    html_path.write_text(html_path.read_text("utf-8") + HTML_FOOT, "utf-8")
    print("✅ chat.html gerado!\n")
    return tdir

# ───────────────────── 8B. DOWNLOAD COMPLETO ─────────────────────
async def export_topic(client: TelegramClient, grp: Channel, tid: Optional[int],
                       tname: str, limit_bytes: int,
                       max_size_per_file: Optional[int] = None) -> Path:
    global dl_size, dl_done, time_start
    dl_done = 0
    time_start = time.time()

    base = Path(sanitize(grp.title))
    base.mkdir(exist_ok=True)
    tdir = base / sanitize(tname)
    tdir.mkdir(exist_ok=True)
    mdir = tdir / "media"
    mdir.mkdir(exist_ok=True)
    ck = load_ckpt(tdir)
    html_path = tdir / "chat.html"

    print(f"\n🔍 Coletando mensagens de '{tname}'…")
    msgs = [m async for m in client.iter_messages(grp, reply_to=tid, reverse=True)]
    total = len(msgs)
    pad = len(str(total))

    done_pos = [i for i, m in enumerate(msgs, 1) if m.id in ck["done_ids"]]
    if done_pos:
        last_i = max(done_pos)
        last_m = msgs[last_i - 1]
        ext = last_m.file.ext or ""
        orig = sanitize(last_m.file.name) if last_m.file and last_m.file.name else f"media{ext}"
        print(f"\nVocê parou no arquivo '{str(last_i).zfill(pad)}_{orig}'.")
        if input("➡️  Continuar desse ponto? (1-Sim, 2-Não) ").strip() != "1":
            while True:
                s = input(f"➡️  Número inicial ({last_i+1}-{total}): ")
                if s.isdigit() and last_i+1 <= int(s) <= total:
                    last_i = int(s) - 1
                    break
                print("❌ inválido.")
        start_idx = last_i + 1
    else:
        start_idx = 1

    pend: list[Tuple[int, Message]] = [
        (seq, m) for seq, m in enumerate(msgs, 1)
        if seq >= start_idx
           and m.file
           and m.id not in ck["done_ids"]
           and (max_size_per_file is None or (m.file.size or 0) <= max_size_per_file)
    ]
    if not pend:
        print("✅ Nada a baixar.")
        return tdir

    sel, acc = [], 0
    remain = None if not limit_bytes else limit_bytes - ck["bytes"]
    for seq, m in pend:
        sz = m.file.size or 0
        if remain and acc + sz > remain:
            break
        sel.append((seq, m))
        acc += sz
    dl_size = acc

    print(f"📁 Baixando {len(sel)} arquivos ({acc/1024**3:.2f} GB).")
    if not html_path.exists():
        html_path.write_text(HTML_HEAD_TPL.format(title=html.escape(tname)), "utf-8")

    async def worker(seq: int, msg: Message):
        nonlocal_dl = None
        global dl_done
        ext = msg.file.ext or ".bin"
        orig = sanitize(msg.file.name) if msg.file and msg.file.name else f"media{ext}"
        fname = f"{str(seq).zfill(pad)}_{orig}"
        prog = 0

        def cb(curr, tot):
            nonlocal prog
            global dl_done
            dl_done += curr - prog
            prog = curr
            asyncio.get_running_loop().call_soon_threadsafe(
                lambda: asyncio.create_task(refresh_download_bar(tname))
            )

        try:
            path = await msg.download_media(file=mdir / fname, progress_callback=cb)
            success = path and Path(path).exists()
        except Exception as e:
            print(f"\n❌ Erro em '{fname}': {e}")
            success = False

        try:
            sender = await msg.get_sender()
            sname = html.escape(
                (f"{getattr(sender,'first_name','')} {getattr(sender,'last_name','')}".strip())
                or getattr(sender,"username","?")
            )
            cont = html.escape(msg.text or "").replace("\n","<br>")
            img_tag = ""
            if ext.lower() in IMG_EXTS and success:
                img_tag = f"<img src='media/{fname}' style='max-width:100%;border-radius:8px;margin:6px 0'>"
            media_btn = (
                f"<a href='media/{fname}' class='btn'>{html.escape(fname)}</a> "
                if success else
                "<a class='btn' style='opacity:0.5;text-decoration:line-through'>MÍDIA AUSENTE</a> "
            )
            ts = msg.date.astimezone().strftime("%d/%m/%Y %H:%M")
            with html_path.open("a", encoding="utf-8") as h:
                h.write(
                    f"<div class='message {'sent' if msg.out else 'received'}'>"
                    f"<div class='sender'>{sname}</div>"
                    f"<div class='content'>{cont}</div>"
                    f"{img_tag}{media_btn}"
                    f"<a href='{permalink(grp,msg.id)}' class='btn'>Link</a>"
                    f"<div class='timestamp'>{ts}</div></div>\n"
                )
            if success:
                ck["done_ids"].append(msg.id)
                ck["bytes"] += Path(path).stat().st_size
                save_ckpt(tdir, ck)
        except Exception as e:
            print(f"\n❌ Falha HTML '{fname}': {e}")

    sem = asyncio.Semaphore(SLOTS)

    async def sem_worker(pair):
        async with sem:
            await worker(*pair)

    await asyncio.gather(*(sem_worker(p) for p in sel))

    if not html_path.read_text("utf-8").endswith(HTML_FOOT):
        html_path.write_text(html_path.read_text("utf-8") + HTML_FOOT, "utf-8")
    print("\n✅ Download concluído!\n")
    return tdir

# ───────────────────── 9. UPLOAD – envia mídia como mídia ─────────────────────
async def upload_from_export(client: TelegramClient, src_folder: Path,
                             dest_grp: Channel, dest_tid: Optional[int]):
    print("\n" + "=" * 60)
    print(f"📤 UPLOAD DA PASTA '{src_folder.name}' → '{dest_grp.title}'")
    print("=" * 60)

    chat_html = src_folder / "chat.html"
    if not chat_html.exists():
        print("❌ 'chat.html' não encontrado.")
        return

    soup = BeautifulSoup(chat_html.read_text("utf-8"), "html.parser")
    msgs = soup.find_all("div", class_="message")

    resp = input("➡️  ENTER = começo no 1º, ou digite prefixo (ex: 4 para '004_'): ").strip()
    if resp == "":
        start_idx = 0
    else:
        try:
            start_pref = int(resp)
        except ValueError:
            print("❌ Prefixo inválido.")
            return
        start_idx = next(
            (i for i, div in enumerate(msgs)
             if (mp := _extract_media_path(div, str(src_folder)))
             and int(Path(mp).name.split('_', 1)[0]) == start_pref),
            None
        )
        if start_idx is None:
            print(f"❌ Prefixo {start_pref} não encontrado.")
            return

    to_send = msgs[start_idx:]
    total = len(to_send)
    print(f"🚀 Enviando {total} mensagens a partir da posição {start_idx+1}.")

    extra = {"reply_to": dest_tid} if dest_tid else {}

    for i, div in enumerate(to_send, 1):
        abs_idx = start_idx + i
        media_path = _extract_media_path(div, str(src_folder))
        content_div = div.find("div", class_="content")
        text = ""
        if content_div:
            text = html.unescape(_clean_message_content_for_upload(content_div).get_text("\n").strip())

        try:
            if media_path:
                original_name = Path(media_path).name
                clean_name = re.sub(r'^\d+_', '', original_name)
                ext = Path(clean_name).suffix.lower()
                is_video = ext in VIDEO_EXTS

                sys.stdout.write(f"\r📤 [{i}/{total}] {clean_name[:30]:30} ...")
                sys.stdout.flush()
                await client.send_file(
                    dest_grp,
                    file=media_path,
                    filename=clean_name,
                    caption=text,
                    parse_mode="md",
                    force_document=False,          # ← mídia quando aplicável
                    supports_streaming=is_video,   # ← vídeos com player
                    **extra
                )
            elif text:
                preview = text.replace("\n", " ")[:30]
                sys.stdout.write(f"\r📤 [{i}/{total}] '{preview}' ...")
                sys.stdout.flush()
                await client.send_message(dest_grp, text, parse_mode="md", **extra)
            else:
                print(f"\n⚠️ Msg {abs_idx} sem conteúdo → pulando")
                continue

            sys.stdout.write(" " * 10 + "\r")
            print(f"✅ {i}/{total}")
            await asyncio.sleep(DELAY_BETWEEN_UPLOADS)

        except FloodWaitError as e:
            print(f"\n⏳ FLOOD WAIT {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"\n❌ Erro na msg {abs_idx}: {e}")
            if input("🛑 Continuar? (s/n): ").lower() != "s":
                break

    print("\n🎉 UPLOAD CONCLUÍDO!")

# helpers de leitura/HTML
def _extract_media_path(div, src_folder: str) -> Optional[str]:
    a = div.find('a', href=re.compile(r'^media/'))
    if a:
        full = Path(src_folder) / a['href']
        return str(full) if full.exists() else None
    return None

def _clean_message_content_for_upload(content_div):
    for btn in content_div.find_all('a', class_='btn'):
        btn.decompose()
    return content_div

# ───────────────────── 10. ATUALIZAR chat.html ─────────────────────
def update_chat_html(folder: Path):
    chat_path = folder / "chat.html"
    media_dir = folder / "media"
    if not chat_path.exists() or not media_dir.is_dir():
        print("❌ Pasta inválida.")
        return

    soup = BeautifulSoup(chat_path.read_text("utf-8"), "html.parser")
    msgs = soup.find_all("div", class_="message")

    seq_to_file: Dict[int, Path] = {}
    for f in media_dir.iterdir():
        if f.is_file() and re.match(r'^\d+_', f.name):
            seq = int(f.name.split("_", 1)[0])
            seq_to_file[seq] = f

    faltando = criados = thumbs = 0
    for idx, div in enumerate(msgs, 1):
        a_media = div.find("a", href=re.compile(r'^media/'))
        expected_file = seq_to_file.get(idx)
        has_thumb = bool(div.find("img", src=re.compile(r'^media/')))

        if expected_file:
            fname = expected_file.name
            if a_media:
                a_media["href"] = f"media/{fname}"
                a_media.string = fname
                a_media.attrs.pop("style", None)
            else:
                a_media = soup.new_tag("a", href=f"media/{fname}", **{"class": "btn"})
                a_media.string = fname
                ts_div = div.find("div", class_="timestamp")
                (ts_div or div).insert_before(a_media)
                criados += 1

            if expected_file.suffix.lower() in IMG_EXTS and not has_thumb:
                img = soup.new_tag("img", src=f"media/{fname}")
                img["style"] = "max-width:100%;border-radius:8px;margin:6px 0"
                a_media.insert_before(img)
                thumbs += 1
        else:
            if a_media:
                a_media.string = "MÍDIA AUSENTE"
                a_media["style"] = "opacity:0.5;text-decoration:line-through"
            faltando += 1

    chat_path.write_text(str(soup), "utf-8")
    print(f"\n✅ chat.html atualizado "
          f"(links criados: {criados}, thumbs: {thumbs}, ausentes: {faltando})\n")

# ───────────────────── 11. MENU PRINCIPAL ─────────────────────
async def main(client: TelegramClient | None = None):
    """
    • Se 'client' for None → cria e gerencia o próprio cliente.
    • Se receber um TelegramClient já conectado → usa-o sem abrir nova sessão.
    """
    close_when_done = False
    if client is None:
        client = TelegramClient(session_name, api_id, api_hash)
        await client.start()
        close_when_done = True

    BANNER = r"""
        .--------.
       / .------. \
      / /        \ \
      | |  ____  | |
     _| |_/ __ \_| |_
    .' |_   
    '._____ ____ _____.'
    |     .'____'.     |
    '.__.'.'    '.'.__.'
    '.__  | LOCK |  __.'
    |   '.'.____.'.'   |
    '.____'.____.'____.'
    '.________________.'

            G R O U P  -  S T E A L E R 
                    !!BY XN30N!!
    """.lstrip("\n")

    while True:
        clear_screen()
        print(BANNER)
        print("✅ Conectado!\n")
        print(
            "[1] Baixar conteúdo\n"
            "[2] Baixar e enviar\n"
            "[3] Enviar por pasta\n"
            "[4] Gerar clone html\n"
            "[5] Atualizar clone html\n"
            "[0] Voltar/Sair\n"
        )
        op = input("➡️  Escolha: ").strip()

        if op in {'1', '2'}:
            src_grp = await select_dialog_with_search(client, "📥 SELECIONE A ORIGEM (grupos/canais)")
            if not src_grp:
                continue
            src_tid, src_name = await select_topic_with_search(client, src_grp, "📌 SELECIONE O TÓPICO DA ORIGEM")
            if op == '1':
                await export_topic(client, src_grp, src_tid, src_name, limit_bytes=0)
                pause()
            else:
                await export_topic(client, src_grp, src_tid, src_name, limit_bytes=0)
                dest_grp = await select_dialog_with_search(client, "📤 SELECIONE O DESTINO (grupos/canais)")
                if not dest_grp:
                    continue
                dest_tid, _ = await select_topic_with_search(client, dest_grp, "📌 TÓPICO DO DESTINO")
                await upload_from_export(client, Path(sanitize(src_name)), dest_grp, dest_tid)
                pause()

        elif op == '3':
            p = ask_directory()
            if not p or not (p.is_dir() and (p / "chat.html").exists()):
                print("❌ Pasta inválida.")
                pause()
                continue
            dest_grp = await select_dialog_with_search(client, "📤 SELECIONE O DESTINO (grupos/canais)")
            if not dest_grp:
                continue
            dest_tid, _ = await select_topic_with_search(client, dest_grp, "📌 TÓPICO DO DESTINO")
            await upload_from_export(client, p, dest_grp, dest_tid)
            pause()

        elif op == '4':
            src_grp = await select_dialog_with_search(client, "📥 SELECIONE A ORIGEM (grupos/canais)")
            if not src_grp:
                continue
            src_tid, src_name = await select_topic_with_search(client, src_grp, "📌 SELECIONE O TÓPICO DA ORIGEM")
            await generate_html_only(client, src_grp, src_tid, src_name)
            pause()

        elif op == '5':
            folder = ask_directory()
            if folder and (folder / "chat.html").exists():
                update_chat_html(folder)
            else:
                print("❌ Pasta inválida.")
            pause()

        elif op == '0':
            break
        else:
            print("❌ Opção inválida.")
            pause()

    if close_when_done:
        await client.disconnect()

# ───────────────────── 12. RUN ─────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\n⚠️  Interrompido pelo usuário.")
    except Exception as e:
        print("\n❌ Erro inesperado:")
        traceback.print_exception(e, file=sys.stdout)
