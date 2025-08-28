#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forward/Mirror via RAM — mesma estratégia do core.py:
- Mapeia escolha do usuário -> topic_id como em core.py:get_topics
- Filtra ORIGEM com reply_to=<topic_id> (apenas se != 0)
- Posta DESTINO com reply_to=<topic_id> (apenas se != 0)
- Ignora mensagens vazias (sem texto e sem mídia)

Notas de envio de mídia (correção iOS):
- Para vídeos preservamos mime_type, attributes (DocumentAttributeVideo, etc.)
  e passamos supports_streaming=True, force_document=False e miniatura (thumb).
- Para fotos/áudios/documentos normais, o Telethon já identifica corretamente.

Novidade:
- Barra de progresso geral no encaminhamento de histórico (%, msgs/s, ETA)
- Correção FileReferenceExpiredError via recaptura e retentativas
- Skip de mídia autodestrutiva (TTL)
"""
import asyncio
import os
import sys
import tempfile
import time
import traceback
from typing import Optional, Callable, Dict, Tuple, List

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError, FileReferenceExpiredError
from telethon.errors.rpcerrorlist import FilePartsInvalidError
from telethon.tl import functions
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    MessageMediaDocument,
)

# ───────── Config por ambiente ─────────
SPOOL_LIMIT = int(os.getenv("TC_SPOOL_LIMIT_MB", "512")) * 1024 * 1024  # 512MB padrão
CONCURRENCY = max(1, int(os.getenv("TC_CONCURRENCY", "1")))             # 1 = sequencial (igual ao seu)

# ───────────────────── util da barra ─────────────────────
def _make_total_bar(prefix: str, total: int, width: int = 34):
    """
    Barra total baseada em contagem de mensagens.
    Retorna (update(done:int), close(ok:bool)).
    """
    start = time.time()

    def _fmt(done: int):
        pct = (done / total * 100) if total else 0.0
        filled = int(width * pct / 100)
        bar = '█' * filled + '░' * (width - filled)
        elapsed = max(1e-6, time.time() - start)
        speed = done / elapsed  # msgs/s
        remain = max(0, total - done)
        eta = remain / speed if speed > 0 else 0.0
        h, m = int(eta // 3600), int((eta % 3600) // 60)
        s = int(eta % 60)
        return f"\r{prefix[:26]:26} │{bar}│ {pct:6.2f}%  {speed:5.2f} msg/s  ETA {h:02d}:{m:02d}:{s:02d}"

    def update(done: int):
        sys.stdout.write(_fmt(done))
        sys.stdout.flush()

    def close(ok: bool = True):
        sys.stdout.write(" ✅\n" if ok else " ❌\n")
        sys.stdout.flush()

    return update, close

# ───────────────────── tópicos (idêntico ao core.py) ─────────────────────
async def _get_topics_like_core(client: TelegramClient, chan) -> Dict[int, str]:
    topics: Dict[int, str] = {0: "Geral"}
    try:
        ent = await client.get_input_entity(chan)
        off_id = off_tid = 0
        off_date = None
        while True:
            res = await client(functions.channels.GetForumTopicsRequest(
                channel=ent, offset_date=off_date,
                offset_id=off_id, offset_topic=off_tid, limit=100, q=""
            ))
            if not res.topics:
                break
            for t in res.topics:
                topics[int(t.id)] = t.title or f"Tópico {t.id}"
            last = res.topics[-1]
            off_id, off_tid, off_date = last.top_message, last.id, last.date
            if len(res.topics) < 100:
                break
    except (RPCError, TypeError):  # chat/grupo sem fórum
        pass
    return topics

async def _resolve_like_core(client: TelegramClient, chat, user_value: Optional[int]) -> Optional[int]:
    if user_value is None:
        return None
    sel = int(user_value)

    tops = await _get_topics_like_core(client, chat)
    items: List[Tuple[int, str]] = list(tops.items())

    if sel == 0:
        return 0
    if sel in tops:
        return sel
    if 0 <= sel < len(items):
        return int(items[sel][0])
    if 1 <= sel <= len(items):
        return int(items[sel - 1][0])

    raise ValueError(f"Índice/topic_id inválido: {user_value}")

# ───────────────────── helpers de robustez ─────────────────────
async def _upload_handle(client: TelegramClient, fobj, filename: str):
    """Upload com part_size_kb ajustável e fallback para compatibilidade."""
    try:
        fobj.seek(0)
    except Exception:
        pass
    try:
        return await client.upload_file(fobj, file_name=filename, part_size_kb=512)
    except FilePartsInvalidError:
        try:
            fobj.seek(0)
        except Exception:
            pass
        return await client.upload_file(fobj, file_name=filename, part_size_kb=256)

def _is_video(msg: Message) -> bool:
    doc = getattr(getattr(msg, "media", None), "document", None)
    if not doc:
        return False
    if any(isinstance(a, DocumentAttributeVideo) for a in getattr(doc, "attributes", []) or []):
        return True
    mt = getattr(doc, "mime_type", "") or ""
    return mt.startswith("video/")  # fallback

def _has_ttl_media(msg: Message) -> bool:
    """
    Detecta mídia autodestrutiva (TTL). Essas não podem ser reenviadas/baixadas novamente.
    """
    media = getattr(msg, "media", None)
    if not media:
        return False
    # Fotos/vídeos com ttl_seconds exposto diretamente
    if hasattr(media, "ttl_seconds") and getattr(media, "ttl_seconds", None):
        return True
    # Documentos podem trazer ttl em atributos
    doc = getattr(media, "document", None)
    if isinstance(media, MessageMediaDocument) and doc and getattr(doc, "attributes", None):
        for attr in doc.attributes:
            if hasattr(attr, "ttl_seconds") and getattr(attr, "ttl_seconds", None):
                return True
    return False

async def _safe_refetch_message(client: TelegramClient, msg: Message) -> Message:
    """
    Recarrega a mesma mensagem do servidor para renovar o file_reference.
    """
    return await client.get_messages(msg.chat_id, ids=msg.id)

async def _safe_download_media(
    client: TelegramClient,
    msg: Message,
    *,
    file,
    max_retries: int = 3,
    retry_sleep: float = 1.5
):
    """
    Faz download com retentativas automáticas.
    Se o file_reference estiver expirado, recarrega a mensagem e tenta novamente.
    Trata FloodWait respeitando o tempo informado.
    """
    last_err = None
    cur_msg = msg
    for attempt in range(1, max_retries + 1):
        try:
            return await client.download_media(cur_msg, file=file)
        except FileReferenceExpiredError as e:
            last_err = e
            # recarrega e tenta de novo
            cur_msg = await _safe_refetch_message(client, cur_msg)
        except FloodWaitError as e:
            await asyncio.sleep(getattr(e, "seconds", 1) + 1)
        except Exception as e:
            last_err = e
        await asyncio.sleep(retry_sleep * attempt)
    if last_err:
        raise last_err

async def _download_thumb_best_effort(client: TelegramClient, msg: Message):
    """
    Tenta baixar uma miniatura (thumb) do documento, mas ignora erros.
    Retorna bytes ou None.
    """
    try:
        doc = getattr(getattr(msg, "media", None), "document", None)
        thumbs = getattr(doc, "thumbs", None) or []
        if not thumbs:
            return None
        # usar a menor thumb
        return await client.download_media(thumbs[0], file=bytes)
    except Exception:
        return None

async def _build_send_kwargs_for_media(client: TelegramClient, msg: Message, filename: str) -> dict:
    """
    Constrói kwargs para send_file de modo que:
    - vídeos sejam enviados como 'vídeo' (preview retangular e tocável em iOS)
    - preserva attributes e mime_type do documento original
    - inclui miniatura (thumb) quando existir
    """
    kwargs: dict = {
        "file_name": filename,
        "parse_mode": "md",
        "force_document": False,      # importante: tenta não forçar como arquivo
    }

    # Se for vídeo, preservar attrs/mime e streaming
    if _is_video(msg):
        doc = msg.media.document
        kwargs["supports_streaming"] = True
        kwargs["attributes"] = list(getattr(doc, "attributes", []) or [])
        mt = getattr(doc, "mime_type", None)
        if mt:
            kwargs["mime_type"] = mt

        # garante extensão .mp4 quando for vídeo mp4 (iOS é chato com isso)
        if (mt or "").lower() == "video/mp4" and not filename.lower().endswith(".mp4"):
            kwargs["file_name"] = filename + ".mp4"

        # thumb (best-effort)
        tbytes = await _download_thumb_best_effort(client, msg)
        if tbytes:
            kwargs["thumb"] = tbytes

    return kwargs

def _extract_filename(msg: Message) -> str:
    """Nome “seguro” com fallback de extensão."""
    name: Optional[str] = None
    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None)
    if doc and getattr(doc, "attributes", None):
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                name = attr.file_name
                break
    if not name:
        name = getattr(getattr(msg, "file", None), "name", None)
    if name:
        safe = str(name).split("/")[-1].split("\\")[-1].strip()
        if safe:
            return safe
    ext = getattr(getattr(msg, "file", None), "ext", "") or ""
    if ext and not ext.startswith('.'):
        ext = f'.{ext}'
    if not ext:
        # se for vídeo e não houver ext, use .mp4 para ajudar iOS
        ext = '.mp4' if _is_video(msg) else '.bin'
    return f"{msg.id}{ext}"

# ───────────────────── processamento (1 msg) ─────────────────────
async def _process_one_message(
    client: TelegramClient,
    msg: Message,
    dst,
    dst_tid: Optional[int],
    strip_caption: bool
):
    caption = "" if strip_caption else (msg.text or "")
    if not getattr(msg, "media", None) and not caption:
        return  # realmente vazia

    # pular mídia autodestrutiva
    if getattr(msg, "media", None) and _has_ttl_media(msg):
        print("⚠️  Mídia com TTL detectada; pulando.")
        return

    reply_to = int(dst_tid) if (dst_tid is not None and dst_tid != 0) else None

    if getattr(msg, "media", None):
        filename = _extract_filename(msg)

        # Spooling: RAM até SPOOL_LIMIT; > derrama pro disco
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as sp:
            try:
                # download robusto (recaptura se ref expirar)
                await _safe_download_media(client, msg, file=sp)
            except FileReferenceExpiredError:
                # redundante (já tenta recapturar), mas fica como fallback
                msg = await _safe_refetch_message(client, msg)
                await _safe_download_media(client, msg, file=sp)

            handle = await _upload_handle(client, sp, filename)

        send_kwargs = await _build_send_kwargs_for_media(client, msg, filename)
        await client.send_file(
            dst,
            handle,
            caption=caption,
            reply_to=reply_to,
            **send_kwargs
        )
    else:
        await client.send_message(
            dst,
            caption,
            parse_mode="md",
            reply_to=reply_to
        )

# ───────────────────── encaminhamento ─────────────────────
async def forward_history(
    client: TelegramClient,
    src,
    dst,
    *,
    topic_id: Optional[int] = None,       # ORIGEM: índice do menu OU topic_id real
    dst_topic_id: Optional[int] = None,   # DESTINO: índice do menu OU topic_id real
    strip_caption: bool = False,
    resume_id: Optional[int] = None,
    on_forward: Optional[Callable[[int], None]] = None
):
    """
    Encaminha o histórico de mensagens com barra de progresso geral.
    A barra reflete *mensagens processadas* (enviadas/puladas/falhas).
    """
    try:
        src_tid = await _resolve_like_core(client, src, topic_id)
        dst_tid = await _resolve_like_core(client, dst, dst_topic_id)

        # Filtro base
        im_kwargs = dict(reverse=True)
        if src_tid and src_tid != 0:
            im_kwargs["reply_to"] = int(src_tid)

        # ── Passo 1: contar quantas mensagens serão consideradas ──
        total = 0
        async for m in client.iter_messages(src, **im_kwargs):
            if resume_id is not None and m.id <= resume_id:
                continue
            cap = "" if strip_caption else (m.text or "")
            if getattr(m, "media", None) or cap:
                total += 1

        update_bar, close_bar = _make_total_bar("Encaminhando", total)
        done = 0
        lock = asyncio.Lock()  # p/ CONCURRENCY>1

        # ── Passo 2: processar de fato ──
        async def _tick():
            nonlocal done
            async with lock:
                done += 1
                update_bar(done)

        if CONCURRENCY == 1:
            async for msg in client.iter_messages(src, **im_kwargs):
                if resume_id is not None and msg.id <= resume_id:
                    continue
                try:
                    await _process_one_message(client, msg, dst, dst_tid, strip_caption)
                    if on_forward:
                        try:
                            on_forward(msg.id)
                        except Exception:
                            pass
                except FloodWaitError as e:
                    secs = getattr(e, "seconds", None) or 60
                    print(f"\n⏳ FLOOD WAIT {secs}s — aguardando…")
                    await asyncio.sleep(secs)
                except Exception:
                    print("⚠️ Falha ao enviar esta mensagem; pulando.")
                    traceback.print_exc(file=sys.stdout)
                finally:
                    await _tick()

        else:
            sem = asyncio.Semaphore(CONCURRENCY)
            tasks = []

            async def worker(m: Message):
                async with sem:
                    try:
                        await _process_one_message(client, m, dst, dst_tid, strip_caption)
                        if on_forward:
                            try:
                                on_forward(m.id)
                            except Exception:
                                pass
                    except FloodWaitError as e:
                        secs = getattr(e, "seconds", None) or 60
                        print(f"\n⏳ FLOOD WAIT {secs}s — aguardando…")
                        await asyncio.sleep(secs)
                    except Exception:
                        print("⚠️ Falha ao enviar esta mensagem; pulando.")
                        traceback.print_exc(file=sys.stdout)
                    finally:
                        await _tick()

            async for msg in client.iter_messages(src, **im_kwargs):
                if resume_id is not None and msg.id <= resume_id:
                    continue
                # aplica mesmo critério de contagem (mensagens “vazias” já foram excluídas no total)
                cap = "" if strip_caption else (msg.text or "")
                if not getattr(msg, "media", None) and not cap:
                    continue
                tasks.append(asyncio.create_task(worker(msg)))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        close_bar(True)
        print("\n✅ Encaminhamento concluído!\n")

    except Exception:
        print("\n❌ Erro inesperado no encaminhamento:")
        traceback.print_exc(file=sys.stdout)

# ───────────────────── espelhamento em tempo real ─────────────────────
def live_mirror(
    client: TelegramClient,
    src,
    dst,
    *,
    topic_id: Optional[int] = None,
    dst_topic_id: Optional[int] = None,
    strip_caption: bool = False
):
    _dst_tid: Optional[int] = None

    async def _init():
        nonlocal _dst_tid
        _dst_tid = await _resolve_like_core(client, dst, dst_topic_id)

    asyncio.get_event_loop().create_task(_init())

    @client.on(events.NewMessage(chats=src))
    async def _handler(event):
        caption = "" if strip_caption else (event.message.text or "")
        if not getattr(event.message, "media", None) and not caption:
            return
        try:
            while True:
                try:
                    if getattr(event.message, "media", None):
                        # mídia TTL? pular
                        if _has_ttl_media(event.message):
                            print("⚠️  Mídia com TTL detectada (live); pulando.")
                            break

                        filename = _extract_filename(event.message)
                        with tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as sp:
                            try:
                                await _safe_download_media(client, event.message, file=sp)
                            except FileReferenceExpiredError:
                                fresh = await _safe_refetch_message(client, event.message)
                                await _safe_download_media(client, fresh, file=sp)

                            handle = await _upload_handle(client, sp, filename)

                        send_kwargs = await _build_send_kwargs_for_media(client, event.message, filename)
                        await client.send_file(
                            dst,
                            handle,
                            caption=caption,
                            reply_to=(int(_dst_tid) if (_dst_tid is not None and _dst_tid != 0) else None),
                            **send_kwargs
                        )
                    else:
                        await client.send_message(
                            dst,
                            caption,
                            parse_mode="md",
                            reply_to=(int(_dst_tid) if (_dst_tid is not None and _dst_tid != 0) else None)
                        )
                    break
                except FloodWaitError as e:
                    secs = getattr(e, "seconds", None) or 60
                    print(f"\n⏳ FLOOD WAIT {secs}s — aguardando…")
                    await asyncio.sleep(secs)
        except Exception:
            print("\n❌ Erro no espelhamento em tempo real:")
            traceback.print_exc(file=sys.stdout)
