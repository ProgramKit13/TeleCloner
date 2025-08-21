#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI wrapper para o Teleclone Mod, com:
- lista de chats em duas colunas, mais legível
- busca por título (p → procurar)
- listagem de TÓPICOS organizada: duas colunas, busca, paginação e "voltar"
- carrega TODOS os tópicos (GetForumTopicsRequest paginado)
- fallback seguro para _print_columns
- checkpoint para retomar encaminhamento
- correção: passar o tópico do DESTINO ao encaminhar/espelhar
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Tuple

from telethon import TelegramClient
from telethon.tl.functions.channels import GetForumTopicsRequest

from teleclone_mod import core, forwarding as fw, users as us
from teleclone_mod.core import load_creds

# ───────────────────── Windows: event loop mais estável ─────────────────────
if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# ───────────────────── Checkpoint CLI ─────────────────────
CKPT_FILE = Path("cli_checkpoint.json")

def load_cli_checkpoint() -> dict:
    if CKPT_FILE.exists():
        return json.loads(CKPT_FILE.read_text(encoding="utf-8"))
    return {}

def save_cli_checkpoint(data: dict):
    CKPT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_checkpoint(src_id: int, topic_id: int) -> Optional[int]:
    data = load_cli_checkpoint()
    return data.get(str(src_id), {}).get(str(topic_id), None)

def update_checkpoint(src_id: int, topic_id: int, message_id: int):
    data = load_cli_checkpoint()
    grp = data.setdefault(str(src_id), {})
    grp[str(topic_id)] = message_id
    save_cli_checkpoint(data)

# ───────────────────── Credenciais ─────────────────────
api_id, api_hash, session_name = load_creds()
client = TelegramClient(session_name, api_id, api_hash)

# ───────────────────── Helpers de UI ─────────────────────
def _print_columns_local(lines: List[str], gap: int = 6):
    """Imprime em duas colunas com espaçamento confortável."""
    cols = shutil.get_terminal_size((120, 24)).columns
    col_w = max(28, (cols - gap) // 2)
    for i in range(0, len(lines), 2):
        left  = lines[i]
        right = lines[i + 1] if i + 1 < len(lines) else ""
        print(f"{left.ljust(col_w)}{' ' * gap}{right}")
    print()

def _print_columns_safe(lines: List[str]):
    """
    Usa a função do core se existir; senão, usa a versão local.
    Evita AttributeError quando core._print_columns não está disponível.
    """
    try:
        fn = getattr(core, "_print_columns", _print_columns_local)
    except Exception:
        fn = _print_columns_local
    fn(lines)

def _filter_casefold(seq: List[Tuple[int, str]], term: str) -> List[Tuple[int, str]]:
    q = (term or "").strip().casefold()
    if not q:
        return seq
    return [(i, t) for (i, t) in seq if q in (t or "").casefold()]

# ───────────────────── Listagem/Escolha de CHATS com busca ─────────────────────
async def _list_dialogs(client):
    dialogs = [
        d for d in await client.get_dialogs(limit=None)
        if d.is_group or d.is_channel
    ]
    print("\n=== Chats disponíveis ===\n")
    linhas = [f"[{i:>3}]  {d.entity.title}" for i, d in enumerate(dialogs)]
    _print_columns_safe(linhas)
    return dialogs

async def _choose_dialog(client, papel):
    dialogs = await _list_dialogs(client)

    while True:
        print("Digite o número, 'p' para procurar, ou ENTER para voltar.")
        opt = input(f"Índice do {papel}: ").strip()

        # cancelar/voltar
        if opt == "":
            print("↩️  Operação cancelada.")
            return None, None

        # procurar por título
        if opt.lower() == "p" or opt.startswith("/"):
            termo = opt[1:] if opt.startswith("/") else input("🔎 Título contém: ").strip()
            if termo == "":
                dialogs = await _list_dialogs(client)
                continue

            filtrados = [d for d in dialogs if termo.casefold() in (d.entity.title or "").casefold()]
            if not filtrados:
                print("❌ Nada encontrado. Pressione ENTER para voltar.")
                input()
                dialogs = await _list_dialogs(client)
                continue

            print("\n=== Resultados ===\n")
            linhas = [f"[{i:>3}]  {d.entity.title}" for i, d in enumerate(filtrados)]
            _print_columns_safe(linhas)

            escolha = input("Número do resultado (ou ENTER p/ voltar): ").strip()
            if escolha == "":
                dialogs = await _list_dialogs(client)
                continue
            try:
                idx = int(escolha)
                ent = filtrados[idx].entity
            except (ValueError, IndexError):
                print("❌ Índice inválido.")
                continue

            topic_id, _ = await _choose_topic_in_forum(ent, titulo="TÓPICO (opcional)")
            return ent, topic_id

        # seleção direta por índice
        try:
            idx = int(opt)
            ent = dialogs[idx].entity
        except (ValueError, IndexError):
            print("❌ Índice inválido.")
            continue

        topic_id, _ = await _choose_topic_in_forum(ent, titulo="TÓPICO (opcional)")
        return ent, topic_id

# ───────────────────── Listagem/Escolha de TÓPICOS (novo) ─────────────────────
async def _fetch_all_topics(ent) -> List[Tuple[int, str]]:
    """
    Busca TODOS os tópicos do fórum, paginando com offsets.
    Retorna lista de tuplas (topic_id, title).
    """
    if not getattr(ent, "forum", False):
        return []

    out: List[Tuple[int, str]] = []
    off_id = 0
    off_tid = 0
    off_date = datetime.now(timezone.utc)  # começar do mais novo

    while True:
        res = await client(GetForumTopicsRequest(
            channel=ent,
            offset_date=off_date,
            offset_id=off_id,
            offset_topic=off_tid,
            limit=100
        ))
        if not res.topics:
            break

        for t in res.topics:
            out.append((int(t.id), t.title or f"Tópico {t.id}"))

        last = res.topics[-1]
        off_id = last.top_message
        off_tid = int(last.id)
        off_date = last.date

        if len(res.topics) < 100:
            break

    # ordena alfabeticamente (título), mantendo coerência
    out.sort(key=lambda x: (x[1] or "").casefold())
    # adiciona "Geral" no topo (id=0)
    return [(0, "Geral")] + out

def _paginate(items: List[Tuple[int, str]], per_page: int, page: int):
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    i0 = (page - 1) * per_page
    i1 = i0 + per_page
    return items[i0:i1], total_pages, page

async def _choose_topic_in_forum(ent, titulo="TÓPICO"):
    """
    UI de seleção de tópico:
      - duas colunas, mais espaço
      - paginação (30 por página)
      - busca (p / /texto)
      - ENTER ou 'b' → volta sem escolher (retorna (None, None))
    Retorna (topic_id, topic_title) ou (None, None).
    """
    if not getattr(ent, "forum", False):
        return None, None

    all_topics = await _fetch_all_topics(ent)  # [(id,título)...] com "Geral" no topo
    filtered = all_topics[:]
    per_page = 30
    page = 1

    while True:
        os.system('cls' if os.name == 'nt' else 'clear')
        print(f"=== {titulo} / {getattr(ent, 'title', '')} ===\n")
        show, total_pages, page = _paginate(filtered, per_page, page)
        if not show:
            print("Nenhum tópico encontrado.\n")
        linhas = []
        base_idx = (page - 1) * per_page
        for i, (tid, tname) in enumerate(show, 1):
            idx = base_idx + i
            label = (tname or "").strip()
            if len(label) > 60:
                label = label[:57] + "..."
            linhas.append(f"[{idx:>3}]  {label}  (id={tid})")
        _print_columns_safe(linhas)
        print(f"Página {page}/{total_pages}")
        print("Digite número, 'n'/'p' p/ navegar, 'p' ou '/termo' p/ procurar, 'b' ou ENTER p/ voltar.\n")
        s = input("Escolha: ").strip()

        if s == "" or s.lower() == "b":
            return None, None
        if s.lower() in ("n", ">"):
            if page < total_pages: page += 1
            continue
        if s.lower() in ("p", "<"):
            if page > 1: page -= 1
            continue
        if s.lower() == "p" or s.startswith("/"):
            term = s[1:] if s.startswith("/") else input("🔎 Título contém: ").strip()
            filtered = _filter_casefold(all_topics, term) if term else all_topics[:]
            page = 1
            continue
        if s.isdigit():
            sel = int(s)
            i0 = (page - 1) * per_page
            # índice relativo à página
            if i0 < sel <= i0 + len(show):
                return show[sel - i0 - 1]
            # fallback: índice absoluto na lista filtrada
            if 1 <= sel <= len(filtered):
                return filtered[sel - 1]
            print("❌ Índice fora do intervalo.")
            input("ENTER para continuar...")
            continue

        print("❌ Entrada inválida.")
        input("ENTER para continuar...")

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
                if not src:
                    continue
                dst, th_dst = await _choose_dialog(client, "DESTINO")
                if not dst:
                    continue
                strip = input("❓ Remover legendas das mídias? (s/N): ").lower().startswith('s')

                # checkpoint: exibe ponto atual e permite reset
                last_id = get_checkpoint(getattr(src, "id", 0), (th_src or 0))
                if last_id:
                    print(f"🔄 Você já encaminhou até a mensagem ID {last_id}.")
                    if input("   Limpar esse ponto e recomeçar do início? (s/N): ").lower().startswith("s"):
                        data = load_cli_checkpoint()
                        data.get(str(getattr(src, "id", 0)), {}).pop(str(th_src or 0), None)
                        save_cli_checkpoint(data)
                        last_id = None  # recomeça do zero

                await fw.forward_history(
                    client, src, dst,
                    topic_id=th_src,
                    dst_topic_id=th_dst,        # tópico do DESTINO corrigido
                    strip_caption=strip,
                    resume_id=last_id,          # retoma de onde parou
                    on_forward=lambda mid: update_checkpoint(getattr(src, "id", 0), (th_src or 0), mid)
                )

            elif op == "2":  # ── ESPELHAR EM TEMPO REAL ──
                src, th_src = await _choose_dialog(client, "ORIGEM")
                if not src:
                    continue
                dst, th_dst = await _choose_dialog(client, "DESTINO")
                if not dst:
                    continue
                strip = input("❓ Remover legendas ao espelhar? (s/N): ").lower().startswith('s')

                fw.live_mirror(
                    client, src, dst,
                    topic_id=th_src,
                    dst_topic_id=th_dst,
                    strip_caption=strip
                )
                print("🔄 Espelhando… CTRL+C para parar.")
                await client.run_until_disconnected()

            elif op == "3":  # ── MIGRAR USUÁRIOS ──
                src, _ = await _choose_dialog(client, "ORIGEM")
                if not src:
                    continue
                dst, _ = await _choose_dialog(client, "DESTINO")
                if not dst:
                    continue
                await us.copy_users(client, src, dst)

            elif op == "4":  # ── APP ORIGINAL ──
                await core.main(client)

            elif op == "0":
                break

            else:
                print("❌ Opção inválida.")

    finally:
        await client.disconnect()

# ───────────────────── Run ─────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, EOFError):
        print("\n⚠️  Interrompido pelo usuário.")
    except Exception as e:
        print("\n❌ Erro inesperado:")
        import traceback; traceback.print_exception(e, file=sys.stdout)
