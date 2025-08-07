#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI wrapper para o Teleclone Mod, com suporte a checkpoint para retomar encaminhamento.
"""
import asyncio
import sys
import json
from pathlib import Path
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import GetForumTopicsRequest

from teleclone_mod import core, forwarding as fw, users as us
from teleclone_mod.core import load_creds

# ───────────────────── Checkpoint CLI ─────────────────────
CKPT_FILE = Path("cli_checkpoint.json")

def load_cli_checkpoint() -> dict:
    if CKPT_FILE.exists():
        return json.loads(CKPT_FILE.read_text(encoding="utf-8"))
    return {}

def save_cli_checkpoint(data: dict):
    CKPT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_checkpoint(src_id: int, topic_id: int) -> int:
    data = load_cli_checkpoint()
    return data.get(str(src_id), {}).get(str(topic_id), 0)

def update_checkpoint(src_id: int, topic_id: int, message_id: int):
    data = load_cli_checkpoint()
    grp = data.setdefault(str(src_id), {})
    grp[str(topic_id)] = message_id
    save_cli_checkpoint(data)

# ───────────────────── Credenciais ─────────────────────
api_id, api_hash, session_name = load_creds()
client = TelegramClient(session_name, api_id, api_hash)

# ───────────────────── Helpers CLI ─────────────────────
async def _list_dialogs(client):
    dialogs = [
        d for d in await client.get_dialogs(limit=None)
        if d.is_group or d.is_channel
    ]
    linhas = [f"[{i}] - {d.entity.title}" for i, d in enumerate(dialogs)]
    print("\n=== Chats disponíveis ===")
    core._print_columns(linhas)
    print()
    return dialogs

async def _choose_dialog(client, papel):
    dialogs = await _list_dialogs(client)
    try:
        idx = int(input(f"Índice do {papel}: "))
        ent = dialogs[idx].entity
    except (ValueError, IndexError):
        print("❌ Índice inválido.")
        return None, None

    topic_id = None
    if getattr(ent, "forum", False):
        topics = await client(GetForumTopicsRequest(
            channel      = ent,
            offset_date  = datetime.utcfromtimestamp(0),
            offset_id    = 0,
            offset_topic = 0,
            limit        = 100
        ))
        if topics.topics:
            print("\n--- Tópicos ---")
            for j, t in enumerate(topics.topics):
                print(f"{j:>3}: {t.title}")
            opt = input("Índice do tópico (vazio = todo grupo): ").strip()
            if opt:
                try:
                    topic_id = topics.topics[int(opt)].id
                except (ValueError, IndexError):
                    print("❌ Índice inválido.")
    return ent, topic_id

# ───────────────────── Menu principal ─────────────────────
async def main():
    await client.start()
    try:
        while True:
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
            print(BANNER)
            print("✅ Conectado!\n")
            print(
                "\n1-Encaminhar histórico"
                "\n2-Espelhar em tempo-real"
                "\n3-Migrar usuários"
                "\n4-Outras opcoes"
                "\n0-Sair"
            )
            op = input("Escolha: ").strip()

            if op == "1":  # ── CLONAR HISTÓRICO ──
                src, th_src = await _choose_dialog(client, "ORIGEM")
                if not src: continue
                dst, _ = await _choose_dialog(client, "DESTINO")
                if not dst: continue
                strip = input("❓ Remover legendas das mídias? (s/N): ").lower().startswith('s')

                # Exibe até onde já foi encaminhado
                last_id = get_checkpoint(src.id, th_src)
                if last_id:
                    print(f"🔄 Você já encaminhou até a mensagem ID {last_id}.")
                    if input("   Limpar esse ponto e recomeçar do início? (s/N): ").lower().startswith("s"):
                        # limpa o checkpoint CLI
                        data = load_cli_checkpoint()
                        data.get(str(src.id), {}).pop(str(th_src), None)
                        save_cli_checkpoint(data)

                # → chama sem o resume_id, deixando o próprio forward_history gerenciar
                await fw.forward_history(
                    client, src, dst,
                    topic_id=th_src,
                    strip_caption=strip,
                    on_forward=lambda mid: update_checkpoint(src.id, th_src, mid)
                )

            elif op == "2":  # ── ESPELHAR VIVO ──
                src, th_src = await _choose_dialog(client, "ORIGEM")
                if not src: continue
                dst, _ = await _choose_dialog(client, "DESTINO")
                if not dst: continue
                strip = input("❓ Remover legendas ao espelhar? (s/N): ").lower().startswith('s')
                fw.live_mirror(client, src, dst,
                               topic_id=th_src,
                               strip_caption=strip)
                print("🔄 Espelhando… CTRL+C p/ parar.")
                await client.run_until_disconnected()

            elif op == "3":  # ── MIGRAR USUÁRIOS ──
                src, _ = await _choose_dialog(client, "ORIGEM")
                if not src: continue
                dst, _ = await _choose_dialog(client, "DESTINO")
                if not dst: continue
                await us.copy_users(client, src, dst)

            elif op == "4":  # ── APP ORIGINAL ──
                await core.main(client)

            elif op == "0":
                break

            else:
                print("Opção inválida.")

    finally:
        await client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\n⚠️  Interrompido pelo usuário.")
    except Exception as e:
        print("\n❌ Erro inesperado:")
        import traceback; traceback.print_exception(e, file=sys.stdout)
