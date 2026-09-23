import argparse
import os.path
import json
import sys
import numpy as np
import random

def addPhoneme(p, t):
    global prevPhoneme
    global f
    if p != prevPhoneme:
        strings[4] += (str.format('{0:.3f}', t)+",phoneme,"+p+"\n")
    prevPhoneme = p

def pickNewPose(t):
    global pose
    global prevPose
    global POSE_COUNT
    global prevPhoneme
    global f
    newPose = -1
    while newPose == -1 or newPose == pose or newPose == prevPose:
        newPose = int(random.random()*POSE_COUNT)
    prevPose = pose
    pose = newPose
    strings[3] += (str.format('{0:.3f}', t)+",pose,"+str(pose)+"\n")
    prevPhoneme = "na"

strings = [""]*5

POSE_COUNT = 5

emotions = {}
emotions["explain"] = 0
emotions["happy"] = 1
emotions["sad"] = 2
emotions["angry"] = 3
emotions["confused"] = 4
emotions["rq"] = 5

mouthList = [["aa","a"],["ae","a"],["ah","a"],["ao","a"],["aw","au"],
["ay","ay"],["b","m"],["ch","t"],["d","t"],["dh","t"],
["eh","a"],["er","u"],["ey","ay"],["f","f"],["g","t"],
["hh","y"],["ih","a"],["iy","ay"],["jh","t"],["k","t"],
["l","y"],["m","m"],["n","t"],["ng","t"],["ow","au"],
["oy","ua"],["p","m"],["r","u"],["s","t"],["sh","t"],
["t","t"],["th","t"],["uh","u"],["uw","u"],["v","f"],
["w","u"],["y","y"],["z","t"],["zh","t"],
["oov","m"]] # For unknown phonemes, the stick figure will just have a closed mouth ("mmm")

mouths = {}
for x in mouthList:
    mouths[x[0]] = x[1]

ENDING_PHONEME = "m"
STOPPERS = [",",";",".",":","!","?"]

def _scheduler_word_candidates(word_string):
    """Gentle word forms that may appear inside tagged script.txt (incl. [emphasis])."""
    raw = word_string if isinstance(word_string, str) else str(word_string or "")
    out = []
    seen = set()

    def add(value):
        if value and value not in seen:
            seen.add(value)
            out.append(value)

    stripped = raw.strip()
    core = stripped.strip("\"'`“”‘’()[]{}")
    while core and core[-1] in ",.;:!?…":
        core = core[:-1]
    while core and core[0] in ",.;:!?…":
        core = core[1:]
    # Prefer bare spelling first so "skin." does not skip a nearer "[skin]".
    add(core)
    if "-" in core:
        add(core.replace("-", ""))
    add(stripped)
    add(raw)
    return out


def _index_word(script, word_string, start_at):
    """Return end offset of the earliest candidate match at/after start_at."""
    best = None  # (match_start, match_end)
    for candidate in _scheduler_word_candidates(word_string):
        try:
            pos = script.index(candidate, start_at)
        except ValueError:
            continue
        end = pos + len(candidate)
        if best is None or pos < best[0] or (pos == best[0] and end < best[1]):
            best = (pos, end)
    if best is None:
        raise ValueError(f"substring not found: {word_string!r}")
    return best[1]


parser = argparse.ArgumentParser(description='blah')
parser.add_argument('--input_file', type=str,  help='the script')
args = parser.parse_args()
INPUT_FILE = args.input_file

f = open(INPUT_FILE+".txt","r+")
originalScript = f.read()
f.close()

f = open(INPUT_FILE+".json","r+")
fileData = f.read()
f.close()

data = json.loads(fileData)
WORD_COUNT = len(data['words'])

pose = -1
prevPose = -1
prevPhoneme = "na"
emotion = "0"
pararaph = 0
image = 0

OS_IndexAt = 0
pickNewPose(0)
strings[1] += "0,emotion,0\n"
strings[0] += "0,paragraph,0\n"
strings[2] += "0,image,0\n"
strings[4] += "0,phoneme,m\n"
for i in range(WORD_COUNT):
    word = data['words'][i]
    if "start" not in word:
        continue
    wordString = word["word"]
    timeStart = word["start"]

    try:
        OS_nextIndex = _index_word(originalScript, wordString, OS_IndexAt)
    except ValueError:
        print(
            "scheduler.py: Gentle script.json does not match script.txt. "
            f"Word {wordString!r} (index {i}) was not found in the tagged script "
            f"(search from offset {OS_IndexAt}). Re-align phonemes after the current "
            "script and wav - never schedule from a stale json.",
            file=sys.stderr,
        )
        raise
    if "<" in originalScript[OS_IndexAt:]:
        tagStart = originalScript.index("<",OS_IndexAt)
        tagEnd = originalScript.index(">",OS_IndexAt)
        if OS_nextIndex > tagStart and tagEnd >= OS_nextIndex:
            OS_nextIndex = _index_word(originalScript, wordString, tagEnd)
    nextDigest = originalScript[OS_IndexAt:OS_nextIndex]

    if "\n" in nextDigest and data['words'][i-1]['case'] != 'not-found-in-audio' and (prevPhoneme == "a" or prevPhoneme == "f" or prevPhoneme == "u" or prevPhoneme == "y"):
        addPhoneme("m", data['words'][i-1]["end"])

    """print(wordString)
    print(str(OS_IndexAt)+", "+str(OS_nextIndex))
    print(nextDigest)
    print("")"""
    pickedPose = False
    for stopper in STOPPERS:
        if stopper in nextDigest:
            pickNewPose(timeStart)
            pickedPose = True

    if "<" in nextDigest:
        leftIndex = nextDigest.index("<")+1
        rightIndex = nextDigest.index(">")
        emotion = emotions[nextDigest[leftIndex:rightIndex]]
        strings[1] += (str.format('{0:.3f}', timeStart)+",emotion,"+str(emotion)+"\n")
        prevPhoneme = "na"

    if "\n\n" in nextDigest:
        pararaph += 1
        image += 1 # The line of the script advances 2 lines whenever we hit a /n/n.
        strings[0] += (str.format('{0:.3f}', timeStart)+",paragraph,"+str(pararaph)+"\n")
        prevPhoneme = "na"

    if "\n" in nextDigest:
        image += 1
        strings[2] += (str.format('{0:.3f}', timeStart)+",image,"+str(image)+"\n")
        prevPhoneme = "na"
        if not pickedPose:
            pickNewPose(timeStart) # A new image means we also need to have a new pose



    phones = word["phones"]
    timeAt = timeStart
    for phone in phones:
        timeAt += phone["duration"]
        phoneString = phone["phone"]
        if phoneString == "sil":
            truePhone = "m"
        else:
            truePhone = mouths[phoneString[:phoneString.index("_")]]
        if len(truePhone) == 2:
            addPhoneme(truePhone[0], timeAt-phone["duration"])
            addPhoneme(truePhone[1], timeAt-phone["duration"]*0.5)
        else:
            addPhoneme(truePhone, timeAt-phone["duration"])
    OS_IndexAt = OS_nextIndex

f = open(INPUT_FILE+"_schedule.csv","w+")
for i in range(len(strings)):
    f.write(strings[i])
    if i < len(strings)-1:
        f.write("SECTION\n")
f.flush()
f.close()
print(f"Done creating schedule for {INPUT_FILE}.")
