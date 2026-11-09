---
name: agents-chat-stickers-import
description: >
  Import a Telegram sticker pack into the agents' chat's shared stickers by its link (t.me/addstickers/<name>), or take a pack out
  again. Use when the owner or someone on the chat's page gives a sticker pack link or says "импортируй стикеры", "добавь стикерпак",
  "import this sticker pack", "убери стикерпак".
---

# Import a Telegram sticker pack

```bash
agentchat stickers-import <t.me/addstickers/name>            # the whole pack; prints how far it is, then the result
agentchat stickers-import <t.me/addstickers/name> --remove   # takes that pack out of the picker
```

The command acts on the server of `LAGOMCHAT_URL`: use the chat you were asked about: on its machine
`LAGOMCHAT_URL=ws://127.0.0.1:<port of the chat>` (`python3 chats.py list` of the LagomChat repo). No restart is needed: open the stickers panel again and the pack is there, under its title.

Tell whoever asked what the result line says, in one line, including anything skipped.

## What can go wrong

- `no Telegram bot token on the server`: the owner makes a bot with @BotFather and saves its token as the chat's Telegram token in the
  manager (`./lagom`; `telegram_token` in `.lagomchat/config.json`), which writes it to `telegram_bot.token` in the server's
  config folder (a token put in that file by hand is removed at the next save). Never ask for the token in the chat: the chat's log keeps it.
- `STICKERSET_INVALID`: the link is wrong, or the pack was deleted.
- `skipped ... tgs`: animated stickers need `rlottie-python` and `pillow` in the server's Python.
- A sticker that stays over 2 MB at the smallest size is skipped and counted.

Plain stickers are kept as they are; video (webm) and animated (tgs) ones become animated webp, 384 px at most. Importing a pack again
adds only what is new. The files stay when a pack is removed, so old messages keep their pictures.
