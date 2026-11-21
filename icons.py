"""
ICONS  -  bot ke saare icons yahin se aate hain (premium unicode theme).

THEME  = tumhara icon dict (isme koi bhi icon badal sakte ho)
ALIAS  = bot ke andar ke naam -> THEME ki key
EXTRA  = jo icons THEME me nahi the unke liye same style ke symbols
"""

THEME = {
    "ok":         "✓",
    "no":         "\u2718",
    "warn":       "\u26A0",
    "arrow":      "\u2192",
    "bullet":     "\u2022",
    "tri":        "\u25B8",
    "diamond":    "\u25C6",
    "star":       "\u2605",
    "spark":      "\u2726",
    "back":       "↲",
    "fwd":        "\u25B6",
    "plus":       "\u2295",
    "minus":      "\u2296",
    "rec":        "\u25C9",
    "rec_off":    "\u25CB",

    # dividers / borders
    "div":        "\u2501" * 16,
    "div_eq":     "\u2550" * 16,
    "div_dash":   "\u2508" * 16,
    "block_on":   "\u25A0",
    "block_off":  "\u25A1",
    "border_top": "\u2550" * 16,
    "border_mid": "\u2501" * 16,
    "border_bot": "\u2550" * 16,

    # process state
    "play":        "‣",
    "stop":        "\u25A0",
    "pause":       "\u2759\u2759",
    "refresh":     "\u21BB",
    "running":     "\u25B6",
    "stopped":     "■",
    "restarting":  "\u21BB",
    "stop_bot":    "■",

    # security / access
    "lock":     "\u25A3",
    "unlock":   "\u25A2",
    "secure":   "\u25C8",
    "key":      "\u2756",
    "shield":   "\u25C7",
    "ban":      "\u2694",
    "trash":    "\u2716",
    "eye":      "\u25C9",

    # people
    "user":   "\u25C8",
    "users":  "\u25CE",
    "crown":  "\u2654",

    # money / commerce
    "wallet":   "\u25C6",
    "premium":  "⌬",
    "lifetime": "\u2736",
    "gift":     "\u2726",
    "ticket":   "\u273F",
    "trophy":   "\u2605",

    # data / analytics
    "graph":    "\u25AA",
    "stats":    "\u25AA",
    "chart_up": "\u25B2",
    "plan":     "\u25A4",

    # comms
    "broadcast": "⚑",
    "chat":      "\u25AB",

    # storage / files
    "folder":   "\u25B8",
    "upload":   "\u25B4",
    "download": "\u25BE",
    "cloud":    "\u2601",

    # tools / time / energy
    "settings": "⚙",
    "cog":      "\u2699",
    "bolt":     "\u26A1",
    "clock":    "\u23F1",
}

# jo cheezein THEME me nahi thi (same style ke symbols)
EXTRA = {
    "gold": "\u2776", "silver": "\u2777", "bronze": "\u2778",   # ❶ ❷ ❸
    "yellow": "\u25D0", "home": "\u2302", "help": "?", "link": "\u2301",
}

# bot ka naam -> THEME key
ALIAS = {
    "check": "ok", "cross": "no", "heart": "spark", "party": "spark", "id": "key",
    "megaphone": "broadcast", "gear": "settings", "chart": "chart_up", "bell": "rec",
    "vote": "diamond", "fire": "bolt", "pin": "tri", "file": "folder", "send": "upload",
    "people": "users", "green": "rec", "red": "rec_off", "loading": "bolt", "smile": "user",
    "point": "fwd", "admin": "crown", "backup": "cloud", "mail": "chat", "connect": "key",
    "create": "spark", "manage": "cog", "contact": "chat", "link": "fwd",
}

# purane/aur user ke custom overrides yahan daal sakte ho (optional)
USER_ICONS = {}


def ic(key):
    if USER_ICONS.get(key):
        return USER_ICONS[key]
    if key in THEME:
        return THEME[key]
    if key in ALIAS and ALIAS[key] in THEME:
        return THEME[ALIAS[key]]
    return EXTRA.get(key, "")
