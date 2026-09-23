"""English grapheme-to-ARPAbet without Kaldi.

Used by the non-Docker Gentle fallback so scheduler.py still sees
Gentle-style phone names (aa, ae, iy, … no stress digits).
"""

from __future__ import annotations

import re

# Vowels scheduler.py maps onto mouth shapes.
_VOWELS = {
    "aa", "ae", "ah", "ao", "aw", "ay", "eh", "er", "ey",
    "ih", "iy", "ow", "oy", "uh", "uw",
}

# Frequent words (CMUdict, unstressed). Keeps function words from going weird.
_LEXICON = {
    "a": ["ah"], "an": ["ae", "n"], "and": ["ae", "n", "d"], "the": ["dh", "ah"],
    "to": ["t", "uw"], "of": ["ah", "v"], "in": ["ih", "n"], "is": ["ih", "z"],
    "it": ["ih", "t"], "you": ["y", "uw"], "that": ["dh", "ae", "t"],
    "for": ["f", "ao", "r"], "on": ["aa", "n"], "with": ["w", "ih", "th"],
    "as": ["ae", "z"], "this": ["dh", "ih", "s"], "they": ["dh", "ey"],
    "be": ["b", "iy"], "at": ["ae", "t"], "or": ["ao", "r"], "from": ["f", "r", "ah", "m"],
    "have": ["hh", "ae", "v"], "was": ["w", "aa", "z"], "by": ["b", "ay"],
    "not": ["n", "aa", "t"], "but": ["b", "ah", "t"], "what": ["w", "ah", "t"],
    "all": ["ao", "l"], "were": ["w", "er"], "we": ["w", "iy"], "when": ["w", "eh", "n"],
    "your": ["y", "ao", "r"], "can": ["k", "ae", "n"], "there": ["dh", "eh", "r"],
    "said": ["s", "eh", "d"], "each": ["iy", "ch"], "which": ["w", "ih", "ch"],
    "do": ["d", "uw"], "their": ["dh", "eh", "r"], "time": ["t", "ay", "m"],
    "if": ["ih", "f"], "will": ["w", "ih", "l"], "how": ["hh", "aw"],
    "about": ["ah", "b", "aw", "t"], "up": ["ah", "p"], "out": ["aw", "t"],
    "them": ["dh", "eh", "m"], "then": ["dh", "eh", "n"], "she": ["sh", "iy"],
    "many": ["m", "eh", "n", "iy"], "some": ["s", "ah", "m"], "so": ["s", "ow"],
    "these": ["dh", "iy", "z"], "would": ["w", "uh", "d"], "other": ["ah", "dh", "er"],
    "into": ["ih", "n", "t", "uw"], "has": ["hh", "ae", "z"], "more": ["m", "ao", "r"],
    "her": ["hh", "er"], "two": ["t", "uw"], "like": ["l", "ay", "k"],
    "him": ["hh", "ih", "m"], "see": ["s", "iy"], "could": ["k", "uh", "d"],
    "no": ["n", "ow"], "make": ["m", "ey", "k"], "than": ["dh", "ae", "n"],
    "first": ["f", "er", "s", "t"], "been": ["b", "ih", "n"], "its": ["ih", "t", "s"],
    "who": ["hh", "uw"], "now": ["n", "aw"], "people": ["p", "iy", "p", "ah", "l"],
    "my": ["m", "ay"], "made": ["m", "ey", "d"], "over": ["ow", "v", "er"],
    "did": ["d", "ih", "d"], "down": ["d", "aw", "n"], "only": ["ow", "n", "l", "iy"],
    "way": ["w", "ey"], "find": ["f", "ay", "n", "d"], "use": ["y", "uw", "z"],
    "may": ["m", "ey"], "water": ["w", "ao", "t", "er"], "long": ["l", "ao", "ng"],
    "little": ["l", "ih", "t", "ah", "l"], "very": ["v", "eh", "r", "iy"],
    "after": ["ae", "f", "t", "er"], "words": ["w", "er", "d", "z"],
    "called": ["k", "ao", "l", "d"], "just": ["jh", "ah", "s", "t"],
    "where": ["w", "eh", "r"], "most": ["m", "ow", "s", "t"], "know": ["n", "ow"],
    "get": ["g", "eh", "t"], "through": ["th", "r", "uw"], "back": ["b", "ae", "k"],
    "much": ["m", "ah", "ch"], "before": ["b", "ih", "f", "ao", "r"],
    "go": ["g", "ow"], "good": ["g", "uh", "d"], "new": ["n", "uw"],
    "write": ["r", "ay", "t"], "our": ["aw", "er"], "used": ["y", "uw", "z", "d"],
    "me": ["m", "iy"], "man": ["m", "ae", "n"], "too": ["t", "uw"],
    "any": ["eh", "n", "iy"], "day": ["d", "ey"], "same": ["s", "ey", "m"],
    "right": ["r", "ay", "t"], "look": ["l", "uh", "k"], "think": ["th", "ih", "ng", "k"],
    "also": ["ao", "l", "s", "ow"], "around": ["er", "aw", "n", "d"],
    "another": ["ah", "n", "ah", "dh", "er"], "came": ["k", "ey", "m"],
    "come": ["k", "ah", "m"], "work": ["w", "er", "k"], "three": ["th", "r", "iy"],
    "must": ["m", "ah", "s", "t"], "because": ["b", "ih", "k", "ah", "z"],
    "does": ["d", "ah", "z"], "part": ["p", "aa", "r", "t"], "even": ["iy", "v", "ah", "n"],
    "place": ["p", "l", "ey", "s"], "well": ["w", "eh", "l"], "such": ["s", "ah", "ch"],
    "here": ["hh", "iy", "r"], "take": ["t", "ey", "k"], "why": ["w", "ay"],
    "help": ["hh", "eh", "l", "p"], "put": ["p", "uh", "t"], "different": ["d", "ih", "f", "er", "ah", "n", "t"],
    "away": ["ah", "w", "ey"], "again": ["ah", "g", "eh", "n"], "off": ["ao", "f"],
    "went": ["w", "eh", "n", "t"], "old": ["ow", "l", "d"], "number": ["n", "ah", "m", "b", "er"],
    "great": ["g", "r", "ey", "t"], "tell": ["t", "eh", "l"], "men": ["m", "eh", "n"],
    "say": ["s", "ey"], "small": ["s", "m", "ao", "l"], "every": ["eh", "v", "r", "iy"],
    "found": ["f", "aw", "n", "d"], "still": ["s", "t", "ih", "l"],
    "between": ["b", "ih", "t", "w", "iy", "n"], "name": ["n", "ey", "m"],
    "should": ["sh", "uh", "d"], "home": ["hh", "ow", "m"], "big": ["b", "ih", "g"],
    "give": ["g", "ih", "v"], "air": ["eh", "r"], "line": ["l", "ay", "n"],
    "end": ["eh", "n", "d"], "follow": ["f", "aa", "l", "ow"], "ask": ["ae", "s", "k"],
    "need": ["n", "iy", "d"], "too": ["t", "uw"], "try": ["t", "r", "ay"],
    "us": ["ah", "s"], "something": ["s", "ah", "m", "th", "ih", "ng"],
    "important": ["ih", "m", "p", "ao", "r", "t", "ah", "n", "t"],
    "world": ["w", "er", "l", "d"], "high": ["hh", "ay"], "light": ["l", "ay", "t"],
    "science": ["s", "ay", "ah", "n", "s"], "video": ["v", "ih", "d", "iy", "ow"],
    "subscribe": ["s", "ah", "b", "s", "k", "r", "ay", "b"],
    "really": ["r", "ih", "l", "iy"], "actually": ["ae", "k", "ch", "uw", "ah", "l", "iy"],
    "okay": ["ow", "k", "ey"], "ok": ["ow", "k", "ey"], "hello": ["hh", "eh", "l", "ow"],
    "hey": ["hh", "ey"], "yeah": ["y", "ae"], "yes": ["y", "eh", "s"],
    "don't": ["d", "ow", "n", "t"], "doesn't": ["d", "ah", "z", "ah", "n", "t"],
    "didn't": ["d", "ih", "d", "ah", "n", "t"], "can't": ["k", "ae", "n", "t"],
    "won't": ["w", "ow", "n", "t"], "isn't": ["ih", "z", "ah", "n", "t"],
    "it's": ["ih", "t", "s"], "that's": ["dh", "ae", "t", "s"],
    "there's": ["dh", "eh", "r", "z"], "they're": ["dh", "eh", "r"],
    "you're": ["y", "uh", "r"], "we're": ["w", "iy", "r"], "i'm": ["ay", "m"],
    "i've": ["ay", "v"], "i'll": ["ay", "l"], "let's": ["l", "eh", "t", "s"],
    "ice": ["ay", "s"], "water": ["w", "ao", "t", "er"], "cube": ["k", "y", "uw", "b"],
    "cubes": ["k", "y", "uw", "b", "z"], "melt": ["m", "eh", "l", "t"],
    "black": ["b", "l", "ae", "k"], "hole": ["hh", "ow", "l"],
    "holes": ["hh", "ow", "l", "z"], "space": ["s", "p", "ey", "s"],
    "earth": ["er", "th"], "sun": ["s", "ah", "n"], "moon": ["m", "uw", "n"],
    "one": ["w", "ah", "n"], "once": ["w", "ah", "n", "s"], "two": ["t", "uw"],
    "four": ["f", "ao", "r"], "five": ["f", "ay", "v"], "six": ["s", "ih", "k", "s"],
    "seven": ["s", "eh", "v", "ah", "n"], "eight": ["ey", "t"], "nine": ["n", "ay", "n"],
    "ten": ["t", "eh", "n"], "zero": ["z", "iy", "r", "ow"],
    "i": ["ay"], "oh": ["ow"], "ah": ["aa"], "um": ["ah", "m"], "uh": ["ah"],
}

_DIGIT = {
    "0": ["z", "iy", "r", "ow"], "1": ["w", "ah", "n"], "2": ["t", "uw"],
    "3": ["th", "r", "iy"], "4": ["f", "ao", "r"], "5": ["f", "ay", "v"],
    "6": ["s", "ih", "k", "s"], "7": ["s", "eh", "v", "ah", "n"],
    "8": ["ey", "t"], "9": ["n", "ay", "n"],
}

_ALLOWED = {
    "aa", "ae", "ah", "ao", "aw", "ay", "b", "ch", "d", "dh", "eh", "er", "ey",
    "f", "g", "hh", "ih", "iy", "jh", "k", "l", "m", "n", "ng", "ow", "oy",
    "p", "r", "s", "sh", "t", "th", "uh", "uw", "v", "w", "y", "z", "zh", "oov",
}


def _clean_key(word: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", word.lower())


def _from_pronouncing(word: str) -> list[str] | None:
    try:
        import pronouncing
    except Exception:
        return None
    phones = pronouncing.phones_for_word(word)
    if not phones:
        return None
    out = []
    for tok in phones[0].split():
        base = re.sub(r"\d+", "", tok).lower()
        if base in _ALLOWED:
            out.append(base)
        elif base:
            out.append("oov")
    return out or None


def _silent_e(stem: str) -> bool:
    return len(stem) >= 3 and stem.endswith("e") and stem[-2] not in "aeiou"


def _heuristic(word: str) -> list[str]:
    w = _clean_key(word)
    if not w:
        return ["oov"]
    if w.isdigit():
        phones: list[str] = []
        for ch in w:
            phones.extend(_DIGIT.get(ch, ["oov"]))
        return phones
    phones: list[str] = []
    i = 0
    n = len(w)
    drop_final_e = _silent_e(w)
    while i < n:
        if drop_final_e and i == n - 1 and w[i] == "e":
            break
        pair = w[i : i + 2]
        triple = w[i : i + 3]
        nxt = w[i + 1] if i + 1 < n else ""
        if triple in ("igh",):
            phones.append("ay")
            i += 3
            continue
        if pair == "ng":
            phones.append("ng")
            i += 2
            continue
        if pair == "th":
            voiced = w.startswith(
                ("the", "this", "that", "them", "they", "then", "there", "those", "these", "though")
            )
            phones.append("dh" if voiced else "th")
            i += 2
            continue
        if pair in ("ch",):
            phones.append("ch")
            i += 2
            continue
        if pair in ("sh",):
            phones.append("sh")
            i += 2
            continue
        if pair in ("ph",):
            phones.append("f")
            i += 2
            continue
        if pair in ("ck",):
            phones.append("k")
            i += 2
            continue
        if pair in ("wh",):
            phones.append("w")
            i += 2
            continue
        if pair in ("qu",):
            phones.extend(["k", "w"])
            i += 2
            continue
        if pair in ("kn",) and i == 0:
            phones.append("n")
            i += 2
            continue
        if pair in ("wr",) and i == 0:
            phones.append("r")
            i += 2
            continue
        if pair in ("ee", "ea"):
            phones.append("iy")
            i += 2
            continue
        if pair in ("ai", "ay"):
            phones.append("ey")
            i += 2
            continue
        if pair in ("oa",):
            phones.append("ow")
            i += 2
            continue
        if pair in ("oo",):
            phones.append("uw")
            i += 2
            continue
        if pair in ("ow", "ou"):
            phones.append("aw")
            i += 2
            continue
        if pair in ("oy", "oi"):
            phones.append("oy")
            i += 2
            continue
        if pair in ("aw", "au"):
            phones.append("ao")
            i += 2
            continue
        if pair in ("er", "ir", "ur") and i + 2 >= n - (1 if drop_final_e else 0):
            phones.append("er")
            i += 2
            continue
        if pair in ("ar",):
            phones.extend(["aa", "r"])
            i += 2
            continue
        if pair in ("or",):
            phones.extend(["ao", "r"])
            i += 2
            continue
        if pair in ("ew",):
            phones.append("uw")
            i += 2
            continue
        ch = w[i]
        if ch == "x":
            phones.extend(["k", "s"])
        elif ch == "c":
            phones.append("s" if nxt in "eiy" else "k")
        elif ch == "g":
            phones.append("jh" if nxt in "eiy" and pair != "gh" else "g")
        elif ch == "q":
            phones.append("k")
        elif ch == "y":
            if i == 0:
                phones.append("y")
            elif i == n - 1:
                phones.append("iy")
            else:
                phones.append("ih")
        elif ch == "a":
            phones.append("ey" if drop_final_e and i == n - 2 else "ae")
        elif ch == "e":
            phones.append("iy" if drop_final_e and i == n - 2 else "eh")
        elif ch == "i":
            phones.append("ay" if drop_final_e and i == n - 2 else "ih")
        elif ch == "o":
            phones.append("ow" if drop_final_e and i == n - 2 else "aa")
        elif ch == "u":
            phones.append("uw" if drop_final_e and i == n - 2 else "ah")
        elif ch == "h":
            phones.append("hh")
        elif ch == "j":
            phones.append("jh")
        elif ch in "bdklmnprstvwz":
            phones.append(ch)
        elif ch == "f":
            phones.append("f")
        else:
            phones.append("oov")
        i += 1
    return [p for p in phones if p in _ALLOWED] or ["oov"]


def word_to_phones(word: str) -> list[str]:
    raw = word.strip()
    if not raw or re.fullmatch(r"[^\w']+", raw):
        return []
    if "-" in raw:
        parts = [p for p in re.split(r"[-–—]", raw) if p]
        phones: list[str] = []
        for part in parts:
            phones.extend(word_to_phones(part))
        return phones or ["oov"]
    key = _clean_key(raw)
    if not key:
        return ["oov"]
    if key in _LEXICON:
        return list(_LEXICON[key])
    found = _from_pronouncing(key)
    if found:
        return found
    return _heuristic(key)


def syllable_weight(word: str) -> int:
    phones = word_to_phones(word)
    n = sum(1 for p in phones if p in _VOWELS)
    return max(1, n) if phones else 1


def phone_weight(phone: str) -> float:
    if phone in _VOWELS:
        return 1.6
    if phone in {"ch", "jh", "sh", "zh", "th", "dh", "ng"}:
        return 1.1
    return 0.75
