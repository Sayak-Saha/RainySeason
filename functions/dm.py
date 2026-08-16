import hikari
from config import BOT_TOKEN

#hikari_client = hikari.GatewayBot(token=BOT_TOKEN, intents=hikari.Intents.ALL)

added_tag = [
    hikari.impl.ContainerComponentBuilder(
    components=[
        hikari.impl.MediaGalleryComponentBuilder(
            items=[
                hikari.impl.MediaGalleryItemBuilder(
                    media="https://res.cloudinary.com/yatoez/image/upload/w_607,h_341,c_fill/v1753483304/rainyseason.gif",
                ),
            ]
        ),
        hikari.impl.SeparatorComponentBuilder(divider=True, spacing=hikari.SpacingType.LARGE),
        hikari.impl.TextDisplayComponentBuilder(
            content="## 🌧️ Welcome to Rainy Season\n⠀⠀⠀Thanks for choosing the <:raintag_0:1377671025105571912><:raintag_1:1377671022966476942> tag — it means a lot. You're what makes this community glow."
        ),
        hikari.impl.SeparatorComponentBuilder(divider=True, spacing=hikari.SpacingType.SMALL),
        hikari.impl.TextDisplayComponentBuilder(content="## ☁️ Get Set Up"),
        hikari.impl.MessageActionRowBuilder(
            components=[
                hikari.impl.LinkButtonBuilder(
                    url="https://discord.com/channels/1369397376175050863/1369397376745345056",
                    label="Join chats →",
                ),
            ]
        ),
        hikari.impl.MessageActionRowBuilder(
            components=[
                hikari.impl.LinkButtonBuilder(
                    url="https://discord.com/channels/1369397376175050863/1397670643167658145",
                    label="Want to chat with me?😉 →",
                ),
            ]
        ),
        hikari.impl.MessageActionRowBuilder(
            components=[
                hikari.impl.LinkButtonBuilder(
                    url="https://discord.com/channels/1369397376175050863/1369693594000167003",
                    label="Pick your roles →",
                ),
            ]
        ),
        hikari.impl.MessageActionRowBuilder(
            components=[
                hikari.impl.LinkButtonBuilder(
                    url="https://discord.com/channels/1369397376175050863/1370368688762523688",
                    label="Select your colors →",
                ),
            ]
        ),
        hikari.impl.SeparatorComponentBuilder(divider=True, spacing=hikari.SpacingType.SMALL),
        hikari.impl.TextDisplayComponentBuilder(content="💫 You're Part of It Now"),
    ]
    ),
    hikari.impl.MessageActionRowBuilder(
        components=[
            hikari.impl.LinkButtonBuilder(
                url="https://discord.gg/CvU77YA65K",
                label="Rainy Season Server",
                emoji="💙",
            ),
        ]
    ),
]


removed_tag = [
    hikari.impl.ContainerComponentBuilder(
        components=[
            hikari.impl.MediaGalleryComponentBuilder(
                items=[
                    hikari.impl.MediaGalleryItemBuilder(
                        media="https://res.cloudinary.com/yatoez/image/upload/w_607,h_341,c_fill/v1753483304/rainyseason.gif",
                    ),
                ]
            ),
            hikari.impl.SeparatorComponentBuilder(divider=True, spacing=hikari.SpacingType.LARGE),
            hikari.impl.TextDisplayComponentBuilder(
                content=(
                    "## ☁️ All Seasons Change\n"
                    "⠀We noticed you’re not using the rain tag anymore — and that’s perfectly okay.\n\n"
                    "⠀Thanks for repping it while you did — you made the vibe better just by being part of it.\n"
                    "⠀Whenever you feel like bringing back the rain, know it's always welcome. ☔"
                )
            ),
            hikari.impl.SeparatorComponentBuilder(divider=True, spacing=hikari.SpacingType.SMALL),
            hikari.impl.TextDisplayComponentBuilder(
                content="💙 Either way, we're glad you're still here with us. <3"
            ),
        ]
    ),
    hikari.impl.MessageActionRowBuilder(
        components=[
            hikari.impl.LinkButtonBuilder(
                url="https://discord.gg/CvU77YA65K",
                label="Jump back in",
                emoji="🌧️",
            ),
        ]
    ),
]

DMReply = [
    hikari.impl.ContainerComponentBuilder(
        components=[
            hikari.impl.TextDisplayComponentBuilder(content="<a:CatPeek:1371836907725656195> | **Want to chat with me?**"),
            hikari.impl.SeparatorComponentBuilder(divider=True, spacing=hikari.SpacingType.SMALL,),
            hikari.impl.MessageActionRowBuilder(
                components=[
                    hikari.impl.LinkButtonBuilder(
                        url="https://discord.com/channels/1369397376175050863/1397670643167658145",
                        label="Yes",
                    ),
                    hikari.impl.LinkButtonBuilder(
                        url="https://discord.com/channels/1369397376175050863/1397670643167658145",
                        label="No",
                    ),
                ]
            ),
        ]
    ),
]

async def send_thanks(user_id: int):
    rest_app = hikari.RESTApp()
    await rest_app.start()
    async with rest_app.acquire(BOT_TOKEN, "Bot") as rest:
        try:
            user = await rest.fetch_user(user_id)
            dm_channel = await user.fetch_dm_channel()
            await rest.create_message(dm_channel.id,components=added_tag)
            print(f"Sent DM to {user.username}#{user.discriminator}")
        except Exception as e:
            print(f"Error sending DM: {e}")
            
    await rest_app.close()
    
async def send_bye(user_id: int):
    rest_app = hikari.RESTApp()
    await rest_app.start()
    async with rest_app.acquire(BOT_TOKEN, "Bot") as rest:
        try:
            user = await rest.fetch_user(user_id)
            dm_channel = await user.fetch_dm_channel()
            await rest.create_message(dm_channel.id,components=removed_tag)
            print(f"Sent DM to {user.username}#{user.discriminator}")
        except Exception as e:
            print(f"Error sending DM: {e}")
            
    await rest_app.close()
    
async def sendDMReply(user_id: int):
    rest_app = hikari.RESTApp()
    await rest_app.start()
    async with rest_app.acquire(BOT_TOKEN, "Bot") as rest:
        try:
            user = await rest.fetch_user(user_id)
            dm_channel = await user.fetch_dm_channel()
            await rest.create_message(dm_channel.id,components=DMReply)
            print(f"Sent DM to {user.username}#{user.discriminator}")
        except Exception as e:
            print(f"Error sending DM: {e}")
            
    await rest_app.close()
    
# hikari_client = hikari.GatewayBot(token=BOT_TOKEN, intents=hikari.Intents.ALL)

# @hikari_client.listen(hikari.StartedEvent)
# async def on_started(event: hikari.StartedEvent) -> None:
#     user = await hikari_client.rest.fetch_user(510796282139508756)
#     dm_channel = await user.fetch_dm_channel()
#     await hikari_client.rest.create_message(dm_channel.id, components=removed_tag)
#     print(f"Bot has started and sent a DM to {user.username}#{user.discriminator}")

# hikari_client.run()

