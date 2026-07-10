from config import *


async def delete_messages_except_grow_tree(trigger_message):
    if trigger_message.guild.id != GUILD_ID:
        return
    if trigger_message.channel.id == 1430896438728065207:
        def keep_tags(message_to_check):
            return message_to_check.id not in [1430913127691845633]
        await trigger_message.channel.purge(limit=100, check=keep_tags)
    if trigger_message.channel.id != GROW_TREE_CHANNEL_ID:
        return
    
    ids_to_keep = {
        GROW_TREE_MESSAGE_ID,  # The permanent Tree Message
        trigger_message.id,     # The new webhook message that triggered this
        GROW_TREE_REACTIONROLE_MESSAGE_ID # The reaction role message
    }

    def should_delete(message_to_check):
        return message_to_check.id not in ids_to_keep

    await trigger_message.channel.purge(limit=100, check=should_delete)