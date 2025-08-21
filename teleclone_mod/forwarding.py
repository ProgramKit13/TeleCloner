#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forward/Mirror via RAM — mesma estratégia do core.py:
- Mapeia escolha do usuário -> topic_id como em core.py:get_topics
- Filtra ORIGEM com reply_to=<topic_id> (apenas se != 0)
- Posta DESTINO com reply_to=<topic_id> (apenas se != 0)
- Ignora mensagens vazias (sem texto e sem mídia)
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
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo, DocumentAttributeAudio

# ───────── Config por ambiente ─────────
SPOOL_LIMIT = int(os.getenv("TC_SPOOL_LIMIT_MB", "512")) * 1024 * 1024  # 512MB
CONCURRENCY = max(1, int(os.getenv("TC_CONCURRENCY", "1")))             # 1 = sequencial

# ───────────────────── tópicos ─────────────────────
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
    except (RPCError, TypeError):
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
def _detect_media_type(msg: Message) -> str:
    """
    Retorna o tipo de mídia para envio adequado:
    - "video", "photo", "audio", "document"
    """
    if msg.photo:
        return "photo"
    if msg.video or any(isinstance(a, DocumentAttributeVideo) for a in getattr(msg.document, "attributes", [])):
        return "video"
    if msg.voice or msg.audio or any(isinstance(a, DocumentAttributeAudio) for a in getattr(msg.document, "attributes", [])):
        return "audio"
    return "document"

def _extract_filename(msg: Message) -> str:
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
        ext = '.bin'
    return f"{msg.id}{ext}"

async def _upload_handle(client: TelegramClient, fobj, filename: str):
    """
    Sobe o conteúdo de fobj e retorna InputFile com nome.
    Tenta part_size_kb=512 e refaz com 256 se necessário.
    """
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

async def _send_media_resilient(client: TelegramClient, dst, sp, filename: str, caption: str, reply_to: Optional[int], is_video: bool):
    """
    Envia mídia a partir de um buffer (SpooledTemporaryFile) com três tentativas:
      1) send_file(sp, file_name=...) direto
      2) rewind + send_file(sp, file_name=...) novamente (em alguns casos resolve)
      3) upload_file(..) -> handle -> send_file(handle, file_name=...) (fallback)
    """
    # 1) tentativa direta (Telethon decide o part size)
    try:
        try:
            sp.seek(0)
        except Exception:
            pass
        await client.send_file(
            dst,
            sp,
            file_name=filename,
            caption=caption,
            parse_mode="md",
            supports_streaming=is_video,
            reply_to=(int(reply_to) if (reply_to is not None and reply_to != 0) else None),
        )
        return
    except FilePartsInvalidError:
        pass  # vamos para a 2ª/3ª tentativa
    except Exception:
        # outros erros continuam para tentativa 2/3
        pass

    # 2) tentar novamente após rewind (há casos esporádicos em que resolve)
    try:
        try:
            sp.seek(0)
        except Exception:
            pass
        await client.send_file(
            dst,
            sp,
            file_name=filename,
            caption=caption,
            parse_mode="md",
            supports_streaming=is_video,
            reply_to=(int(reply_to) if (reply_to is not None and reply_to != 0) else None),
        )
        return
    except FilePartsInvalidError:
        pass
    except Exception:
        pass

    # 3) fallback via upload_file (com fallback de part size) → handle
    handle = await _upload_handle(client, sp, filename)
    await client.send_file(
        dst,
        handle,
        file_name=filename,
        caption=caption,
        parse_mode="md",
        supports_streaming=is_video,
        reply_to=(int(reply_to) if (reply_to is not None and reply_to != 0) else None),
    )

async def _process_one_message(client: TelegramClient, msg: Message, dst, dst_tid: Optional[int], strip_caption: bool):
    caption = "" if strip_caption else (msg.text or "")
    if not getattr(msg, "media", None) and not caption:
        return

    if getattr(msg, "media", None):
        filename = _extract_filename(msg)
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_LIMIT, mode="w+b") as sp:
            await client.download_media(msg, file=sp)

            # verifica se algo foi baixado
            try:
                size = sp.tell()
            except Exception:
                try:
                    sp.seek(0, os.SEEK_END)
                    size = sp.tell()
                except Exception:
                    size = 1  # assume >0 para tentar o envio

            if not size:
                # nada baixado → não tenta fazer upload para evitar FilePartsInvalidError
                if caption:
                    await client.send_message(
                        dst, caption,
                        parse_mode="md",
                        reply_to=(int(dst_tid) if (dst_tid is not None and dst_tid != 0) else None)
                    )
                return

            media_type = _detect_media_type(msg)
            is_video = media_type == "video"

            await _send_media_resilient(
                client=client,
                dst=dst,
                sp=sp,
                filename=filename,
                caption=caption,
                reply_to=dst_tid,
                is_video=is_video
            )
    else:
        await client.send_message(
            dst,
            caption,
            parse_mode="md",
            reply_to=(int(dst_tid) if (dst_tid is not None and dst_tid != 0) else None)
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
                except FilePartsInvalidError:
                    # última barreira: pula mensagem problemática
                    print("⚠️ Falha ao enviar (FilePartsInvalidError); pulando esta mensagem.")
                    break
                except Exception:
                    print("⚠️ Falha ao enviar esta mensagem; pulando.")
                    traceback.print_exc(file=sys.stdout)
                    break

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

                            try:
                                size = sp.tell()
                            except Exception:
                                try:
                                    sp.seek(0, os.SEEK_END)
                                    size = sp.tell()
                                except Exception:
                                    size = 1

                            if not size:
                                if caption:
                                    await client.send_message(
                                        dst, caption,
                                        parse_mode="md",
                                        reply_to=(int(_dst_tid) if (_dst_tid is not None and _dst_tid != 0) else None)
                                    )
                                break

                            media_type = _detect_media_type(event.message)
                            is_video = media_type == "video"

                            await _send_media_resilient(
                                client=client,
                                dst=dst,
                                sp=sp,
                                filename=filename,
                                caption=caption,
                                reply_to=_dst_tid,
                                is_video=is_video
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
        except FilePartsInvalidError:
            print("⚠️ Falha ao enviar (FilePartsInvalidError) no espelhamento; pulando mensagem.")
        except Exception:
            print("\n❌ Erro no espelhamento em tempo real:")
            traceback.print_exc(file=sys.stdout)
