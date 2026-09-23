# There is probably a smarter, more efficient way to do this with regex. This runs nearly instantly though, so no big deal.
import re

def removeTags(script):
  TO_REMOVE = ["[","]","/"]

  newScript = script.replace("-"," ")
  for charToRemove in TO_REMOVE:
      newScript = newScript.replace(charToRemove,"")

  while "<" in newScript:
      start = newScript.index("<")
      end = newScript.index(">")+1
      newScript = newScript[:start]+newScript[end:]
  while "  " in newScript:
      newScript = newScript.replace("  "," ")
  while "\n " in newScript:
      newScript = newScript.replace("\n ","\n")
  while " \n" in newScript:
      newScript = newScript.replace(" \n","\n")
  while newScript[0] == " ":
      newScript = newScript[1:]

  return newScript

def lineBillboardStem(index):
    """Short stable billboard stem for disk (Windows MAX_PATH safe): b001, b002, ...

    index is 1-based among non-empty script lines (same order as Studio parse_tagged_script).
    """
    try:
        n = int(index)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        n = 1
    return f"b{n:03d}"


def getFilenameOfLine(line):
    """Legacy topic-based stem (can be very long). Prefer lineBillboardStem for new writes."""
    topic = getTopic(line)
    return re.sub(r'[^A-Za-z0-9 -]+', '',  topic.lower())

def getTopic(stri):
    if "[" in stri:
        start = stri.index("[")+1
        end = stri.index("]")
        return stri[start:end]
    else:
        return removeTags(stri)

def capitalize(stri):
    words = stri.split(" ")
    result = ""
    for i in range(len(words)):
        if i >= 1:
            result = result+" "
        w = words[i]
        result = result+w[0].upper()+w[1:]
    return result
