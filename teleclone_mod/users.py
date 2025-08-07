# teleclone_mod/users.py
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.errors import (UserPrivacyRestrictedError,
                             PeerFloodError, ChatAdminRequiredError)

async def copy_users(client, src, dst, limit=None):
    """
    Tenta adicionar ao dst todos os participantes de src cujo
    phone ou username esteja disponível.
    """
    added = 0
    async for p in client.iter_participants(src, limit=limit):
        if not (p.username or p.phone):
            continue  # não adicionáveis
        try:
            await client(InviteToChannelRequest(dst, [p.id]))
            added += 1
        except (UserPrivacyRestrictedError, PeerFloodError,
                ChatAdminRequiredError):
            continue   # ignora restrições
    print(f"✅ {added} usuários convidados.")
