# komga_cover_extractor/log_utils.py
import os
import re
from datetime import datetime

# Assuming these will be moved to config.py
# TODO: Ensure these are correctly imported from config module later
from .config import errors, items_changed, LOGS_DIR, log_to_file


# Check the text file line by line for the passed message
# Note: Kept here for now, consider moving to file_utils later if appropriate.
def check_text_file_for_message(text_file, message):
    """Checks if a message exists in a text file."""
    # Open the file in read mode using a context manager
    try:
        with open(text_file, "r") as f:
            # Check if any line in the file matches the message
            return any(line.strip() == message.strip() for line in f)
    except FileNotFoundError:
        # If the file doesn't exist, it can't contain the message
        return False
    except Exception as e:
        # Use print to avoid recursion if send_message is used for logging here
        print(f"ERROR reading text file {text_file}: {e}")
        return False


# Sends a message, prints it, and writes it to a file.
def send_message(
    message,
    error=False,
    log=log_to_file,  # Use imported config value
    error_file_name="errors.txt",
    changes_file_name="changes.txt",
):
    """Prints a message, logs it, and appends to error/change lists."""
    print(message)
    if error:
        errors.append(message)  # Use imported config value
        if log:
            write_to_file(error_file_name, message)
    else:
        items_changed.append(message)  # Use imported config value
        if log:
            write_to_file(changes_file_name, message)


# Writes a log file
def write_to_file(
    file,
    message,
    without_timestamp=False,
    overwrite=False,
    check_for_dup=False,
    write_to=None,
    can_write_log=log_to_file,  # Use imported config value
):
    """Writes a message to a specified log file."""
    write_status = False
    logs_dir_loc = write_to or LOGS_DIR  # Use imported config value

    # check if the logs directory exists, if not create it
    if not os.path.exists(logs_dir_loc):
        try:
            os.makedirs(logs_dir_loc)
        except OSError as e:
            # Use print to avoid recursion with send_message
            print(f"ERROR creating log directory {logs_dir_loc}: {e}")
            return False

    if can_write_log and logs_dir_loc:
        # get rid of formatting
        message = re.sub("\t|\n", "", str(message), flags=re.IGNORECASE).strip()
        contains = False

        # check if it already contains the message
        log_file_path = os.path.join(logs_dir_loc, file)

        if check_for_dup and os.path.isfile(log_file_path):
            # Use the local/imported check_text_file_for_message function
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
                            f.write(f"\n {message}")
                        else:
                            f.write(f"\n{dt_string} {message}")
                    write_status = True

                except Exception as e:
                    # Use print to avoid recursion with send_message
                    print(f"ERROR writing to log file {log_file_path}: {e}")
            except Exception as e:
                # Use print to avoid recursion with send_message
                print(f"ERROR opening log file {log_file_path}: {e}")
    return write_status
