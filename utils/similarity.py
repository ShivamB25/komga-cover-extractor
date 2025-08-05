from difflib import SequenceMatcher
from functools import lru_cache


# Checks similarity between two strings.
@lru_cache(maxsize=3500)
def similar(a, b):
    # convert to lowercase and strip
    a = a.lower().strip()
    b = b.lower().strip()

    # evaluate
    if a == "" or b == "":
        return 0.0
    elif a == b:
        return 1.0
    else:
        return SequenceMatcher(None, a, b).ratio()