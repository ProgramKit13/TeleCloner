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
from telethon.tl.custom.message import Message
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import FilePartsInvalidError


async def forward_history(
    client: TelegramClient,
    src,
    dst,
    *,
    topic_id: Optional[int] = None,
    strip_caption: bool = False,
    resume_id: Optional[int] = None,               # ← voltou
    on_forward: Optional[Callable[[int], None]] = None
):
    """
    Encaminha TODO o histórico de `src` → `dst`, **via RAM**.
    - topic_id: filtra dentro de fórum (thread)
    - strip_caption: remove legendas de mídia
    - resume_id: pula mensagens com id <= resume_id
    - on_forward: callback(msg_id) após cada mensagem enviada
    """
    try:
        # Construção de kwargs para compatibilidade entre versões do Telethon
        im_kwargs = dict(reverse=True)
        if topic_id is not None:
            im_kwargs["reply_to"] = topic_id

        async for msg in client.iter_messages(src, **im_kwargs):
            if resume_id is not None and msg.id <= resume_id:
                continue

            caption = "" if strip_caption else (msg.text or "")

            # Tenta enviar a mensagem; se der FLOOD, espera e tenta de novo.
            while True:
                try:
                    sent = await _safe_send(client, dst, msg, caption)
                    if sent and on_forward:
                        try:
                            on_forward(msg.id)
                        except Exception:
                            # callback do usuário não deve quebrar o loop
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


async def _safe_send(
    client: TelegramClient,
    dst,
    msg: Message,
    caption: str
) -> bool:
    """
    Envia uma única mensagem:
      - Se tiver mídia: baixa para bytes → BytesIO → upload_file (part_size) → send_file.
      - Se só texto: send_message.
    Retorna True se algo foi enviado; False se a mensagem era vazia.
    Propaga FloodWaitError para o caller tratar.
    """
    # Mensagem sem texto e sem mídia = nada a fazer
    if not getattr(msg, "media", None) and not caption:
        return False

    try:
        if getattr(msg, "media", None):
            # Baixa mídia para bytes (tamanho confiável)
            data: bytes = await msg.download_media(file=bytes)
            if not data:
                print("⚠️ Mídia vazia/indisponível; pulando.")
                return False

            bio = io.BytesIO(data)
            filename = _extract_filename(msg)
            bio.name = filename
            bio.seek(0)

            # Upload explícito (evita FilePartsInvalid)
            try:
                handle = await client.upload_file(bio, file_name=filename, part_size_kb=512)
            except FilePartsInvalidError:
                bio.seek(0)
                handle = await client.upload_file(bio, file_name=filename, part_size_kb=256)

            await client.send_file(
                dst,
                handle,
                file_name=filename,   # preserva nome e extensão
                caption=caption,
                parse_mode="md"
            )
            return True

        else:
            await client.send_message(dst, caption, parse_mode="md")
            return True

    except FloodWaitError:
        raise
    except Exception:
        print("⚠️ Falha no envio de mensagem/mídia:")
        traceback.print_exc(file=sys.stdout)
        return False


def _extract_filename(msg: Message) -> str:
    """
    Extrai um nome de arquivo seguro com extensão.
    1) DocumentAttributeFilename (para arquivos, stickers, pdfs, etc.)
    2) msg.file.name (quando existir)
    3) fallback: msg.id + .ext (ou .bin)
    """
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
        func=(lambda e: (e.message.reply_to_msg_id == topic_id)) if topic_id is not None else (lambda _e: True)
    ))
    async def _handler(event):
        msg: Message = event.message
        caption = "" if strip_caption else (msg.text or "")
        try:
            # mesma política de flood do histórico
            while True:
                try:
                    await _safe_send(client, dst, msg, caption)
                    break
                except FloodWaitError as e:
                    secs = getattr(e, "seconds", None) or 60
                    print(f"\n⏳ FLOOD WAIT {secs}s — aguardando…")
                    await asyncio.sleep(secs)
        except Exception:
            print("\n❌ Erro no espelhamento em tempo real:")
            traceback.print_exc(file=sys.stdout)
