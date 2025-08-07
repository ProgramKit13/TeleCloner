#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rotinas de encaminhamento (forward) e espelhamento (mirror) de histórico,
sempre via RAM (download em BytesIO → upload).
"""
import asyncio
import io
import sys
import traceback
from typing import Optional, Callable

from telethon import TelegramClient, events
from telethon.tl.types import Message, DocumentAttributeFilename
from telethon.errors import FloodWaitError


async def forward_history(
    client: TelegramClient,
    src,
    dst,
    *,
    topic_id: Optional[int] = None,
    strip_caption: bool = False,
    on_forward: Optional[Callable[[int], None]] = None
):
    """
    Encaminha TODO o histórico de `src` → `dst`, **via RAM**.
    - `topic_id`: filtra dentro de fórum (thread)
    - `strip_caption`: remove legendas de mídia
    - `on_forward`: callback(msg_id) após cada mensagem enviada
    """
    try:
        async for msg in client.iter_messages(src, reply_to=topic_id, reverse=True):
            caption = "" if strip_caption else (msg.text or "")
            await _safe_send(client, dst, msg, caption)
            if on_forward:
                on_forward(msg.id)
        print("✅ Encaminhamento concluído!")
    except FloodWaitError as e:
        print(f"\n⏳ FLOOD WAIT {e.seconds}s — aguardando…")
        await asyncio.sleep(e.seconds)
        # retoma do ponto onde parou
        await forward_history(client, src, dst,
                              topic_id=topic_id,
                              strip_caption=strip_caption,
                              on_forward=on_forward)
    except Exception:
        print("\n❌ Erro inesperado no encaminhamento:")
        traceback.print_exc(file=sys.stdout)


async def _safe_send(
    client: TelegramClient,
    dst,
    msg: Message,
    caption: str
):
    """
    1) Se houver mídia, faz download para um BytesIO e envia com send_file,
       passando o `filename` extraído corretamente.
    2) Se só houver texto, faz send_message.
    Mensagens completamente vazias são puladas.
    """
    # pula mensagens sem texto nem mídia
    if not msg.media and not caption:
        return

    try:
        if msg.media:
            # --- Baixa pra RAM ---
            bio = io.BytesIO()
            await msg.download_media(file=bio)
            bio.seek(0)

            # --- Extrai o nome do arquivo original ---
            filename = _extract_filename(msg)

            # --- Envia do buffer em RAM ---
            await client.send_file(
                dst,
                bio,
                filename=filename,
                caption=caption,
                parse_mode="md"
            )
        else:
            # só texto
            await client.send_message(dst, caption, parse_mode="md")

    except FloodWaitError:
        # repropaga para o caller lidar com flood
        raise
    except Exception:
        print("⚠️ Falha no envio de mensagem/mídia:")
        traceback.print_exc(file=sys.stdout)


def _extract_filename(msg: Message) -> str:
    """
    Tenta extrair o nome + extensão originais:
    1) DocumentAttributeFilename (para arquivos, stickers, pdfs, etc.)
    2) msg.file.name (usualmente para fotos com nome)
    3) usa msg.file.ext (extensão) e msg.id como fallback
    """
    # 1) DocumentAttributeFilename
    media = getattr(msg, "media", None)
    doc = getattr(media, "document", None)
    if doc and getattr(doc, "attributes", None):
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name

    # 2) msg.file.name
    name = getattr(msg.file, "name", None)
    if name:
        return name

    # 3) fallback msg.id + extensão
    ext = getattr(msg.file, "ext", "") or ""
    return f"{msg.id}{ext}"


def live_mirror(
    client: TelegramClient,
    src,
    dst,
    *,
    topic_id: Optional[int] = None,
    strip_caption: bool = False
):
    """
    Espelha em tempo real src → dst, **via RAM**.
    """

    @client.on(events.NewMessage(
        chats=src,
        func=lambda e: (e.message.reply_to_msg_id == topic_id)
                       if topic_id is not None else True
    ))
    async def _handler(event):
        msg: Message = event.message
        caption = "" if strip_caption else (msg.text or "")
        try:
            await _safe_send(client, dst, msg, caption)
        except FloodWaitError as e:
            print(f"\n⏳ FLOOD WAIT {e.seconds}s — aguardando…")
            await asyncio.sleep(e.seconds)
        except Exception:
            print("\n❌ Erro no espelhamento em tempo real:")
            traceback.print_exc(file=sys.stdout)
