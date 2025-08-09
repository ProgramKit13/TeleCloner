#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Forward/Mirror via RAM — MESMA estratégia do core.py:
- Mapear escolha do usuário -> topic_id como em core.py:get_topics
- Filtrar origem com reply_to=<topic_id> (apenas se != 0)
- Postar destino com reply_to=<topic_id> (apenas se != 0)
- Ignorar mensagens vazias (sem texto e sem mídia)

Ajustes mínimos:
- part_size_kb fixo em 512 (fallback 256 apenas se necessário).
- Tolerar TypeError do Telethon quando chat não tem fórum (mantém “Geral” ao invés de crashar).
"""
import asyncio
import io
import sys
import os
import traceback
from typing import Optional, Callable, Dict, Tuple, List

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.errors.rpcerrorlist import FilePartsInvalidError
from telethon.tl import functions
from telethon.tl.custom.message import Message
from telethon.tl.types import DocumentAttributeFilename


# ───────────────────── tópicos (idêntico ao core.py) ─────────────────────

async def _get_topics_like_core(client: TelegramClient, chan) -> Dict[int, str]:
    """Retorna {0:'Geral', topic_id:title, ...} na MESMA ordem do core.py."""
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
    """
    Interpreta igual ao menu do core.py:
      - None → None
      - 0 → 0 (Geral)
      - se 'user_value' for uma CHAVE em tops → usa direto (topic_id real)
      - senão, se couber como ÍNDICE de list(tops.items()) (0-based; e aceita 1-based por tolerância) → pega a CHAVE
    """
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
    Encaminha TODO o histórico src → dst via RAM, postando no tópico escolhido.
    """
    try:
        src_tid = await _resolve_like_core(client, src, topic_id)
        dst_tid = await _resolve_like_core(client, dst, dst_topic_id)

        # Filtra no tópico de ORIGEM somente se != 0 (Geral)
        im_kwargs = dict(reverse=True)
        if src_tid and src_tid != 0:
            im_kwargs["reply_to"] = int(src_tid)

        async for msg in client.iter_messages(src, **im_kwargs):
            if resume_id is not None and msg.id <= resume_id:
                continue

            caption = "" if strip_caption else (msg.text or "")

            # Skip mensagens realmente vazias (sem texto e sem mídia)
            if not getattr(msg, "media", None) and not caption:
                continue

            while True:
                try:
                    if getattr(msg, "media", None):
                        data: bytes = await msg.download_media(file=bytes)
                        if not data:
                            break  # mídia indisponível
                        bio = io.BytesIO(data)
                        filename = _extract_filename(msg)
                        bio.name = filename
                        bio.seek(0)
                        try:
                            # FIXO 512 KB; fallback 256 só se o servidor recusar
                            handle = await client.upload_file(bio, file_name=filename, part_size_kb=512)
                        except FilePartsInvalidError:
                            bio.seek(0)
                            handle = await client.upload_file(bio, file_name=filename, part_size_kb=256)

                        await client.send_file(
                            dst,
                            handle,
                            file_name=filename,
                            caption=caption,
                            parse_mode="md",
                            reply_to=(int(dst_tid) if (dst_tid is not None and dst_tid != 0) else None)
                        )
                    else:
                        # somente texto (não vazio)
                        await client.send_message(
                            dst,
                            caption,
                            parse_mode="md",
                            reply_to=(int(dst_tid) if (dst_tid is not None and dst_tid != 0) else None)
                        )

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

        print("✅ Encaminhamento concluído!")

    except Exception:
        print("\n❌ Erro inesperado no encaminhamento:")
        traceback.print_exc(file=sys.stdout)


# ───────────────────── espelhamento em tempo real (mesma regra) ─────────────────────

def live_mirror(
    client: TelegramClient,
    src,
    dst,
    *,
    topic_id: Optional[int] = None,       # índice OU topic_id
    dst_topic_id: Optional[int] = None,   # índice OU topic_id
    strip_caption: bool = False
):
    """
    Espelha src → dst em tempo real. Posta no tópico com reply_to=<topic_id> se != 0.
    (sem InputReplyToMessage, sem top_msg_id)
    """
    _dst_tid: Optional[int] = None

    async def _init():
        nonlocal _dst_tid
        _dst_tid = await _resolve_like_core(client, dst, dst_topic_id)

    asyncio.get_event_loop().create_task(_init())

    @client.on(events.NewMessage(chats=src))
    async def _handler(event):
        caption = "" if strip_caption else (event.message.text or "")
        # pular mensagens vazias
        if not getattr(event.message, "media", None) and not caption:
            return
        try:
            while True:
                try:
                    if getattr(event.message, "media", None):
                        data: bytes = await event.message.download_media(file=bytes)
                        if not data:
                            return
                        bio = io.BytesIO(data)
                        filename = _extract_filename(event.message)
                        bio.name = filename
                        bio.seek(0)
                        try:
                            # FIXO 512 KB; fallback 256 só se necessário
                            handle = await client.upload_file(bio, file_name=filename, part_size_kb=512)
                        except FilePartsInvalidError:
                            bio.seek(0)
                            handle = await client.upload_file(bio, file_name=filename, part_size_kb=256)

                        await client.send_file(
                            dst,
                            handle,
                            file_name=filename,
                            caption=caption,
                            parse_mode="md",
                            reply_to=(int(_dst_tid) if (_dst_tid is not None and _dst_tid != 0) else None)
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


# ───────────────────── util: nome de arquivo ─────────────────────

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
