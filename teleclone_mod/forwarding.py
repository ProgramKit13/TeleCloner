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
"""
import asyncio
import os
import sys
import tempfile
import traceback
from typing import Optional, Callable, Dict, Tuple, List

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.errors.rpcerrorlist import FilePartsInvalidError
from telethon.tl import functions
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
)

# ───────── Config por ambiente ─────────
SPOOL_LIMIT = int(os.getenv("TC_SPOOL_LIMIT_MB", "512")) * 1024 * 1024  # 512MB padrão
CONCURRENCY = max(1, int(os.getenv("TC_CONCURRENCY", "1")))             # 1 = sequencial (igual ao seu)


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


# ───────────────────── helpers ─────────────────────
async def _upload_handle(client: TelegramClient, fobj, filename: str):
    """Upload com part_size_kb ajustável e fallback para compatibilidade."""
    try:
        fobj.seek(0)
    except Exception:
        pass
    try:
        return await client.upload_file(fobj, file_name=filename, part_size_kb=512)
    except FilePartsInvalidError:
        fobj.seek(0)
        return await client.upload_file(fobj, file_name=filename, part_size_kb=256)


def _is_video(msg: Message) -> bool:
    doc = getattr(getattr(msg, "media", None), "document", None)
    if not doc:
        return False
    if any(isinstance(a, DocumentAttributeVideo) for a in getattr(doc, "attributes", []) or []):
        return True
    mt = getattr(doc, "mime_type", "") or ""
    return mt.startswith("video/")  # fallback


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

        # thumb (se houver)
        try:
            thumbs = getattr(doc, "thumbs", None) or []
            if thumbs:
                # baixa a menor miniatura para agilizar
                tbytes = await client.download_media(thumbs[0], file=bytes)
                if tbytes:
                    kwargs["thumb"] = tbytes
        except Exception:
            pass

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

    reply_to = int(dst_tid) if (dst_tid is not None and dst_tid != 0) else None

    if getattr(msg, "media", None):
        filename = _extract_filename(msg)

        # Spooling: RAM até SPOOL_LIMIT; > derrama pro disco
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as sp:
            await client.download_media(msg, file=sp)
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
    try:
        src_tid = await _resolve_like_core(client, src, topic_id)
        dst_tid = await _resolve_like_core(client, dst, dst_topic_id)

        im_kwargs = dict(reverse=True)
        if src_tid and src_tid != 0:
            im_kwargs["reply_to"] = int(src_tid)

        if CONCURRENCY == 1:
            async for msg in client.iter_messages(src, **im_kwargs):
                if resume_id is not None and msg.id <= resume_id:
                    continue
                while True:
                    try:
                        await _process_one_message(client, msg, dst, dst_tid, strip_caption)
                        if on_forward:
                            try:
                                on_forward(msg.id)
                            except Exception:
                                pass
                        break
                    except FloodWaitError as e:
                        secs = getattr(e, "seconds", None) or 60
                        print(f"\n⏳ FLOOD WAIT {secs}s — aguardando…")
                        await asyncio.sleep(secs)
                    except Exception:
                        print("⚠️ Falha ao enviar esta mensagem; pulando.")
                        traceback.print_exc(file=sys.stdout)
                        break
        else:
            sem = asyncio.Semaphore(CONCURRENCY)
            tasks = []

            async def worker(m: Message):
                async with sem:
                    while True:
                        try:
                            await _process_one_message(client, m, dst, dst_tid, strip_caption)
                            if on_forward:
                                try:
                                    on_forward(m.id)
                                except Exception:
                                    pass
                            break
                        except FloodWaitError as e:
                            secs = getattr(e, "seconds", None) or 60
                            print(f"\n⏳ FLOOD WAIT {secs}s — aguardando…")
                            await asyncio.sleep(secs)
                        except Exception:
                            print("⚠️ Falha ao enviar esta mensagem; pulando.")
                            traceback.print_exc(file=sys.stdout)
                            break

            async for msg in client.iter_messages(src, **im_kwargs):
                if resume_id is not None and msg.id <= resume_id:
                    continue
                tasks.append(asyncio.create_task(worker(msg)))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        print("✅ Encaminhamento concluído!")

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
                        filename = _extract_filename(event.message)
                        with tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as sp:
                            await event.message.download_media(file=sp)
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
