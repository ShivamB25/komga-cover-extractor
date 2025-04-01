import os
import re
from datetime import datetime

# Define LOGS_DIR relative to the project root
# TODO: Consider making this configurable
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # Go up two levels from messaging/
LOGS_DIR = os.path.join(ROOT_DIR, "logs")

# Checks if the passed string is present line by line in the text file
def check_text_file_for_message(text_file, message):
    try:
        # Open the file in read mode using a context manager
        with open(text_file, "r") as f:
            # Check if any line in the file matches the message
            return any(line.strip() == message.strip() for line in f)
    except FileNotFoundError:
        # If the file doesn't exist, the message isn't present
        return False
    except Exception as e:
        # Log other potential errors, but assume message not found
        print(f"Error checking text file {text_file}: {e}")
        return False


# Writes a log file
def write_to_file(
    file,
    message,
    without_timestamp=False,
    overwrite=False,
    check_for_dup=False,
    write_to=None,
    can_write_log=True, # Defaulting to True, should be controlled by config
):
    write_status = False
    logs_dir_loc = write_to or LOGS_DIR

    # check if the logs directory exists, if not create it
    if not os.path.exists(logs_dir_loc):
        try:
            os.makedirs(logs_dir_loc)
        except OSError as e:
            print(f"Error creating logs directory {logs_dir_loc}: {e}")
            return False

    if can_write_log and logs_dir_loc:
        # get rid of formatting
        message = re.sub("\t|\n", "", str(message), flags=re.IGNORECASE).strip()
        contains = False

        # check if it already contains the message
        log_file_path = os.path.join(logs_dir_loc, file)

        if check_for_dup and os.path.isfile(log_file_path):
            contains = check_text_file_for_message(log_file_path, message)

        if not contains or overwrite:
            try:
                append_write = (
                    "a" if os.path.exists(log_file_path) and not overwrite else "w"
                )
                try:
                    now = datetime.now()
                    dt_string = now.strftime("%d/%m/%Y %H:%M:%S")

                    with open(log_file_path, append_write) as f:
                        if without_timestamp:
                            f.write(f"\n{message}") # Added newline for consistency
                        else:
                            f.write(f"\n{dt_string} {message}")
                    write_status = True

                except Exception as e:
                    print(f"Error writing to log file {log_file_path}: {e}")
            except Exception as e:
                 print(f"Error opening log file {log_file_path}: {e}")
    return write_status