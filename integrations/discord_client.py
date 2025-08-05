# -*- coding: utf-8 -*-
"""
Discord integration functions and classes.
"""
from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from discord_webhook import DiscordWebhook

if TYPE_CHECKING:
    from settings import script_version_text

from config.constants import discord_embed_limit, discord_webhook_url
from utils.helpers import send_message


# Handles our embed object along with any associated file
class Embed:
    def __init__(self, embed, file=None):
        self.embed = embed
        self.file = file


class NewReleaseNotification:
    def __init__(self, number, title, color, fields, webhook, series_name, volume_obj):
        self.number = number
        self.title = title
        self.color = color
        self.fields = fields
        self.webhook = webhook
        self.series_name = series_name
        self.volume_obj = volume_obj


# Adjusts discord embeds fields to fit the discord embed field limits
def handle_fields(embed, fields):
    if fields:
        # An embed can contain a maximum of 25 fields
        fields = fields[:25]

        for field in fields:
            # A field name/title is limited to 256 characters
            if len(field["name"]) > 256:
                field["name"] = (
                    field["name"][:253] + "..."
                    if not field["name"].endswith("```")
                    else field["name"][:-3][:250] + "...```"
                )

            # The value of the field is limited to 1024 characters
            if len(field["value"]) > 1024:
                field["value"] = (
                    field["value"][:1021] + "..."
                    if not field["value"].endswith("```")
                    else field["value"][:-3][:1018] + "...```"
                )

            embed.add_embed_field(
                name=field["name"],
                value=field["value"],
                inline=field["inline"],
            )
    return embed


last_hook_index = None


# Handles picking a webhook url, to evenly distribute the load
@lru_cache(maxsize=10)
def pick_webhook(hook, passed_webhook=None, url=None):
    global last_hook_index

    if passed_webhook:
        hook = passed_webhook
    elif url:
        hook = url
    elif discord_webhook_url:
        if last_hook_index is None or last_hook_index == len(discord_webhook_url) - 1:
            hook = discord_webhook_url
        else:
            hook = discord_webhook_url[last_hook_index + 1]
        last_hook_index = discord_webhook_url.index(hook)

    return hook


webhook_obj = DiscordWebhook(url=None)


# Sends a discord message using the users webhook url
def send_discord_message(
    message,
    embeds=[],
    url=None,
    rate_limit=True,
    timestamp=True,
    passed_webhook=None,
    image=None,
    image_local=None,
):
    global grouped_notifications, webhook_obj

    sent_status = False
    hook = None
    hook = pick_webhook(hook, passed_webhook, url)

    try:
        if hook:
            webhook_obj.url = hook

            if rate_limit:
                webhook_obj.rate_limit_retry = rate_limit

            if embeds:
                # Limit the number of embeds to 10
                for index, embed in enumerate(embeds[:10], start=1):
                    if script_version_text:
                        embed.embed.set_footer(text=script_version_text)

                    if timestamp and (
                        not hasattr(embed.embed, "timestamp")
                        or not embed.embed.timestamp
                    ):
                        embed.embed.set_timestamp()

                    if image and not image_local:
                        embed.embed.set_image(url=image)
                    elif embed.file:
                        file_name = (
                            "cover.jpg" if len(embeds) == 1 else f"cover_{index}.jpg"
                        )
                        webhook_obj.add_file(file=embed.file, filename=file_name)
                        embed.embed.set_image(url=f"attachment://{file_name}")

                    webhook_obj.add_embed(embed.embed)
            elif message:
                webhook_obj.content = message

            webhook_obj.execute()
            sent_status = True
    except Exception as e:
        send_message(f"{e}", error=True, discord=False)
        # Reset the webhook object
        webhook_obj = DiscordWebhook(url=None)
        return sent_status

    # Reset the webhook object
    webhook_obj = DiscordWebhook(url=None)

    return sent_status


# Handles adding our embed to the list of grouped notifications
# If the list is at the limit, it will send the list and clear it
# Also handles setting the timestamp on the embed of when it was added
def group_notification(notifications, embed, passed_webhook=None):
    failed_attempts = 0

    if len(notifications) >= discord_embed_limit:
        while notifications:
            message_status = send_discord_message(
                None, notifications, passed_webhook=passed_webhook
            )
            if (
                message_status
                or (failed_attempts >= len(discord_webhook_url) and not passed_webhook)
                or (passed_webhook and failed_attempts >= 1)
            ):
                notifications = []
            else:
                failed_attempts += 1

    # Set timestamp on embed
    embed.embed.set_timestamp()

    # Add embed to list
    if embed not in notifications:
        notifications.append(embed)

    return notifications