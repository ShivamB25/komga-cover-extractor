# komga_cover_extractor/discord_utils.py
from discord_webhook import DiscordWebhook, DiscordEmbed
from functools import lru_cache

# Import necessary config variables
# Use try-except for robustness during refactoring
try:
    from .config import discord_webhook_url, script_version_text, discord_embed_limit
except ImportError:
    print("WARN: Could not import from .config in discord_utils.py, using placeholders.")
    discord_webhook_url = []
    script_version_text = ""
    discord_embed_limit = 10

# Import necessary functions from other utils
try:
    from .log_utils import send_message
except ImportError:
    print("WARN: Could not import send_message from log_utils.py")
    def send_message(msg, error=False, discord=False): print(f"{'ERROR: ' if error else ''}{msg}")

# Import models used in this module
try:
    from .models import Embed # Assuming Embed class is defined in models.py
except ImportError:
     print("WARN: Could not import Embed model in discord_utils.py")
     class Embed: pass # Placeholder

# Global variable to track the last used webhook index for load balancing
last_hook_index = None
# Global webhook object (re-initialized per message)
webhook_obj = DiscordWebhook(url=None)

# Adjusts discord embeds fields to fit the discord embed field limits
def handle_fields(embed, fields):
    """Adds fields to a DiscordEmbed object, truncating if necessary."""
    if not fields:
        return embed

    # An embed can contain a maximum of 25 fields
    fields = fields[:25]

    for field in fields:
        field_name = str(field.get("name", "Unnamed Field"))
        field_value = str(field.get("value", "No Value"))
        field_inline = field.get("inline", False) # Default to False

        # A field name/title is limited to 256 characters
        if len(field_name) > 256:
            field_name = field_name[:253] + "..."

        # The value of the field is limited to 1024 characters
        if len(field_value) > 1024:
            # Check for code blocks and truncate appropriately
            if field_value.startswith("```") and field_value.endswith("```"):
                 field_value = field_value[:1021] + "...```"
            else:
                 field_value = field_value[:1021] + "..."

        try:
            embed.add_embed_field(
                name=field_name,
                value=field_value,
                inline=field_inline,
            )
        except Exception as e:
             # Use send_message for logging errors from this module
             send_message(f"Error adding embed field: {e}", error=True)

    return embed

# Handles picking a webhook url, to evenly distribute the load
@lru_cache(maxsize=10) # Cache based on passed_webhook/url to avoid re-calculating index unnecessarily
def pick_webhook(passed_webhook=None, url=None):
    """Selects a webhook URL, rotating through the configured list."""
    global last_hook_index
    hook = None

    if passed_webhook:
        hook = passed_webhook
    elif url:
        hook = url
    elif discord_webhook_url: # Use imported config value
        if not isinstance(discord_webhook_url, list) or not discord_webhook_url:
             return None # No valid webhooks configured

        num_hooks = len(discord_webhook_url)
        if last_hook_index is None or last_hook_index >= num_hooks - 1:
            current_index = 0
        else:
            current_index = last_hook_index + 1

        hook = discord_webhook_url[current_index]
        last_hook_index = current_index
    return hook

# Sends a discord message using the users webhook url
def send_discord_message(
    message=None, # Allow message content directly
    embeds=[], # List of Embed objects (from models.py)
    url=None, # Specific URL override
    rate_limit=True,
    timestamp=True,
    passed_webhook=None, # Allow forcing a specific webhook from the list
    # image=None, # Deprecated? Embeds handle images now
    # image_local=None, # Deprecated? Embeds handle images now
):
    """Sends a message and/or embeds to Discord via webhook."""
    global webhook_obj # Use the global object

    sent_status = False
    hook = pick_webhook(passed_webhook=passed_webhook, url=url) # Use local function

    if not hook:
        # send_message("No valid Discord webhook URL found.", error=True, discord=False) # Avoid recursion
        print("ERROR: No valid Discord webhook URL found for send_discord_message.")
        return False

    try:
        # Re-initialize webhook object for each message to clear previous state
        webhook_obj = DiscordWebhook(url=hook, rate_limit_retry=rate_limit)

        if message:
            webhook_obj.content = str(message) # Ensure message is string

        if embeds:
            # Limit the number of embeds
            embeds_to_send = embeds[:discord_embed_limit] # Use imported config value

            for index, embed_wrapper in enumerate(embeds_to_send):
                 # Ensure we have a valid DiscordEmbed object
                 if not isinstance(embed_wrapper, Embed) or not isinstance(embed_wrapper.embed, DiscordEmbed):
                     send_message(f"Invalid embed object type in list: {type(embed_wrapper)}", error=True)
                     continue

                 discord_embed = embed_wrapper.embed

                 # Add footer and timestamp
                 if script_version_text: # Use imported config value
                     discord_embed.set_footer(text=script_version_text)
                 if timestamp and not discord_embed.timestamp: # Check if timestamp already set
                     discord_embed.set_timestamp()

                 # Handle file attachments (if Embed wrapper contains file data)
                 if embed_wrapper.file:
                     try:
                         # Determine filename, ensure uniqueness if multiple files
                         file_name = f"cover_{index}.jpg" if len(embeds_to_send) > 1 else "cover.jpg"
                         # Add file expects file content (bytes) and filename
                         webhook_obj.add_file(file=embed_wrapper.file, filename=file_name)
                         # Set image URL to attachment
                         discord_embed.set_image(url=f"attachment://{file_name}")
                     except Exception as file_err:
                          send_message(f"Error adding file to Discord message: {file_err}", error=True)

                 webhook_obj.add_embed(discord_embed)

        # Only execute if there's content or embeds
        if webhook_obj.content or webhook_obj.embeds:
            response = webhook_obj.execute()
            # Check response status if needed (response is requests.Response object or list of them)
            # Basic check: if no exception, assume success for now
            sent_status = True
        # else:
        #     print("DEBUG: No content or embeds to send to Discord.")


    except Exception as e:
        send_message(f"Error sending Discord message to {hook}: {e}", error=True, discord=False) # Avoid recursion
        sent_status = False
    finally:
         # Clear embeds and content for the next potential use of the global object (though re-init is safer)
         webhook_obj.embeds = []
         webhook_obj.content = None
         # Clear files too
         # webhook_obj.files = {} # Property might not exist directly, handled by add_file

    return sent_status


# Handles adding our embed to the list of grouped notifications
# If the list is at the limit, it will send the list and clear it
# Also handles setting the timestamp on the embed of when it was added
def group_notification(notifications_list, embed_wrapper, passed_webhook=None):
    """Adds an Embed object to a list, sending if the list reaches the limit."""
    if not isinstance(embed_wrapper, Embed):
         send_message(f"Invalid object passed to group_notification: {type(embed_wrapper)}", error=True)
         return notifications_list # Return original list

    # Set timestamp just before adding (more accurate than setting it earlier)
    if isinstance(embed_wrapper.embed, DiscordEmbed) and not embed_wrapper.embed.timestamp:
        embed_wrapper.embed.set_timestamp()

    # Add embed to list
    if embed_wrapper not in notifications_list: # Basic check to avoid exact duplicates
        notifications_list.append(embed_wrapper)

    # Check if limit reached
    if len(notifications_list) >= discord_embed_limit: # Use imported config value
        send_discord_message(None, embeds=notifications_list, passed_webhook=passed_webhook) # Use local function
        return [] # Return a new empty list

    return notifications_list # Return the modified list