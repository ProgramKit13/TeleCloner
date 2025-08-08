#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# teleclone_mod/users.py

import asyncio
import random

from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.errors import FloodWaitError
from telethon.errors.rpcerrorlist import (
    PeerFloodError,
    ChatAdminRequiredError,
    UserPrivacyRestrictedError,
    UserNotMutualContactError,
    UserChannelsTooMuchError,
    ChannelPrivateError,
)


async def copy_users(client, src, dst, limit=None, base_pause=2.0):
    """
    Convida participantes de `src` para `dst`, com tratamento de erros e pacing.
    - limit: máximo de usuários a iterar (None = todos)
    - base_pause: pausa base entre convites (segundos), com jitter aleatório
    """
    added = skipped_privacy = skipped_not_mutual = skipped_full = 0
    failures = 0
    invited = 0

    async for p in client.iter_participants(src, limit=limit):
        # pacing: pausa curta + jitter
        await asyncio.sleep(base_pause + random.random())

        try:
            await client(InviteToChannelRequest(dst, [p]))
            added += 1
            invited += 1

            # pausa maior a cada bloco para reduzir risco de flood
            if invited % 20 == 0:
                await asyncio.sleep(20 + random.random() * 10)

        except FloodWaitError as e:
            secs = getattr(e, "seconds", None) or 60
            print(f"⏳ FloodWait {secs}s — pausando.")
            await asyncio.sleep(secs)

        except PeerFloodError:
            print("⛔ PeerFloodError — limite atingido. Pare por algumas horas e tente depois.")
            break

        except ChatAdminRequiredError:
            print("⛔ Permissão insuficiente no destino: você precisa ser admin para convidar.")
            break

        except UserNotMutualContactError:
            # a pessoa não é seu contato mútuo; Telegram bloqueia o convite
            skipped_not_mutual += 1
            continue

        except UserPrivacyRestrictedError:
            # privacidade do usuário impede convite
            skipped_privacy += 1
            continue

        except UserChannelsTooMuchError:
            # usuário já está no limite de canais/grupos
            skipped_full += 1
            continue

        except ChannelPrivateError:
            print("⛔ O destino é privado/inacessível para este cliente.")
            break

        except Exception as e:
            failures += 1
            uname = getattr(p, "username", None) or f"id={getattr(p, 'id', '?')}"
            print(f"⚠️ Falha ao convidar {uname}: {e}")
            continue

    print(
        "✅ Migração concluída. "
        f"Adicionados: {added} | Privacidade: {skipped_privacy} | Não-mútuos: {skipped_not_mutual} "
        f"| Cheio: {skipped_full} | Outras falhas: {failures}"
    )
