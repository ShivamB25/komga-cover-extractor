import time
from functools import lru_cache
from discord_webhook import DiscordEmbed, DiscordWebhook

# TODO: Move these constants to a config file or pass them as arguments
# Discord Colors
purple_color = 7615723  # Starting Execution Notification
red_color = 16711680  # Removing File Notification
grey_color = 8421504  # Renaming, Reorganizing, Moving, Series Matching, Bookwalker Release
yellow_color = 16776960  # Not Upgradeable Notification
green_color = 65280  # Upgradeable and New Release Notification
preorder_blue_color = 5919485  # Bookwalker Preorder Notification

# Discord Embed Limit
discord_embed_limit = 10

# Global state for webhook rotation - Consider refactoring to avoid global state
last_hook_index = None
webhook_obj = DiscordWebhook(url=None) # Initialize webhook object

# Handles our embed object along with any associated file
class Embed:
    def __init__(self, embed, file=None):
        self.embed = embed
        self.file = file

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
                inline=field.get("inline", False), # Use .get for safety
            )
    return embed

# Handles picking a webhook url, to evenly distribute the load
@lru_cache(maxsize=10)
def pick_webhook(available_webhooks, passed_webhook=None, url=None):
    global last_hook_index

    hook = None
    if passed_webhook:
        hook = passed_webhook
    elif url:
        hook = url
    elif available_webhooks: # Use the passed list
        if last_hook_index is None or last_hook_index >= len(available_webhooks) - 1:
             last_hook_index = 0
        else:
             last_hook_index += 1
        hook = available_webhooks[last_hook_index]

    return hook

# Sends a discord message using the users webhook url
def send_discord_message(
    message,
    embeds=[],
    available_webhooks=[], # Pass available webhooks
    url=None,
    rate_limit=True,
    timestamp=True,
    passed_webhook=None,
    image=None,
    image_local=None,
    script_version_text=None, # Pass script version if needed for footer
):
    global webhook_obj # Use the global webhook object

    sent_status = False
    hook = pick_webhook(available_webhooks, passed_webhook, url)

    if not hook:
        print("Error: No webhook URL available to send Discord message.", file=sys.stderr)
        return False

    try:
        webhook_obj.url = hook # Set the URL on the existing object

        # Clear previous content/embeds
        webhook_obj.content = None
        webhook_obj.embeds = []
        webhook_obj.files = {}

        if rate_limit:
            webhook_obj.rate_limit_retry = rate_limit

        if embeds:
            # Limit the number of embeds
            for index, embed_item in enumerate(embeds[:discord_embed_limit], start=1):
                if not isinstance(embed_item, Embed):
                     print(f"Warning: Invalid embed item type: {type(embed_item)}. Skipping.")
                     continue

                current_embed = embed_item.embed # Access the DiscordEmbed object

                if script_version_text:
                    current_embed.set_footer(text=script_version_text)

                if timestamp and not current_embed.timestamp:
                    current_embed.set_timestamp()

                if image and not image_local:
                    current_embed.set_image(url=image)
                elif embed_item.file:
                    file_name = (
                        "cover.jpg" if len(embeds) == 1 else f"cover_{index}.jpg"
                    )
                    # Ensure file is bytes-like object
                    file_content = embed_item.file
                    if isinstance(file_content, str):
                        try:
                            with open(file_content, 'rb') as f:
                                file_content = f.read()
                        except Exception as e:
                             print(f"Error reading file for Discord attachment {file_content}: {e}")
                             continue # Skip this embed if file can't be read

                    if isinstance(file_content, bytes):
                         webhook_obj.add_file(file=file_content, filename=file_name)
                         current_embed.set_image(url=f"attachment://{file_name}")
                    else:
                         print(f"Warning: Invalid file content type for Discord attachment: {type(file_content)}")


                webhook_obj.add_embed(current_embed)
        elif message:
            webhook_obj.content = message

        if webhook_obj.embeds or webhook_obj.content: # Only execute if there's something to send
             response = webhook_obj.execute()
             # Optional: Check response status if needed
             # if response.status_code >= 400:
             #     print(f"Error sending Discord message (Status {response.status_code}): {response.text}")
             # else:
             #     sent_status = True
             sent_status = True # Assume success if execute doesn't raise exception
        else:
             print("Warning: Attempted to send empty Discord message.")


    except Exception as e:
        # TODO: Replace send_message with proper logging
        print(f"Error sending Discord message: {e}")
        # Reset the webhook object state after error might be needed depending on library behavior
        webhook_obj = DiscordWebhook(url=None)
        return False

    # Reset webhook object state for next use (important!)
    webhook_obj.content = None
    webhook_obj.embeds = []
    webhook_obj.files = {}
    # Keep the URL set by pick_webhook for potential reuse if needed, or reset:
    # webhook_obj.url = None

    return sent_status


# Handles adding our embed to the list of grouped notifications
# If the list is at the limit, it will send the list and clear it
# Also handles setting the timestamp on the embed of when it was added
def group_notification(notifications, embed, available_webhooks=[], passed_webhook=None):
    failed_attempts = 0

    if len(notifications) >= discord_embed_limit:
        while notifications:
            message_status = send_discord_message(
                None, notifications, available_webhooks=available_webhooks, passed_webhook=passed_webhook
            )
            if (
                message_status
                or (failed_attempts >= len(available_webhooks) and not passed_webhook) # Use available_webhooks count
                or (passed_webhook and failed_attempts >= 1)
            ):
                notifications.clear() # Use clear() for lists
            else:
                failed_attempts += 1
                # Rotate webhook if multiple attempts fail?
                if passed_webhook is None and available_webhooks:
                     passed_webhook = pick_webhook(available_webhooks) # Try next webhook

    # Set timestamp on embed if not already set
    if not embed.embed.timestamp:
         embed.embed.set_timestamp()

    # Add embed to list
    if embed not in notifications: # Check for duplicates if necessary
        notifications.append(embed)

    return notifications