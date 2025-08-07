# teleclone_mod/users.py
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.errors import (
    UserPrivacyRestrictedError,
    PeerFloodError,
    ChatAdminRequiredError,
)

async def copy_users(client, src, dst, limit=None):
    """
    Tenta adicionar ao dst todos os participantes de src cujo
    phone ou username esteja disponível.
    """
    added = 0
    async for p in client.iter_participants(src, limit=limit):
        username = getattr(p, "username", None)
        phone = getattr(p, "phone", None)
        if not username and not phone:
            # Ignora usuários sem meio de contato identificável
            continue
        try:
            await client(InviteToChannelRequest(dst, [p]))
            added += 1
        except (
                UserPrivacyRestrictedError,
                PeerFloodError,
                ChatAdminRequiredError,
                ValueError,
                TypeError,
        ):
            # Erros conhecidos ou usuários inválidos são simplesmente ignorados
            continue
    print(f"✅ {added} usuários convidados.")
