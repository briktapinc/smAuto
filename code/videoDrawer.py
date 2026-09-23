import argparse
import os.path
import numpy as np
from PIL import Image
import math
from utils import getFilenameOfLine, lineBillboardStem
import shutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _poses_root() -> str:
    """Same folder Studio recolors: BUBBLEPOD_POSES_DIR or user_data/poses, else repo poses/."""
    for key in ("BUBBLEPOD_POSES_DIR", "LAZYKH_POSES_DIR"):
        raw = (os.environ.get(key) or "").strip()
        if raw and os.path.isdir(raw):
            return raw
    for key in ("BUBBLEPOD_USER_DATA", "LAZYKH_USER_DATA"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            candidate = os.path.join(raw, "poses")
            if os.path.isdir(candidate):
                return candidate
    # Dev fallback next to this file's repo
    fallback = os.path.join(REPO_ROOT, "user_data", "poses")
    if os.path.isdir(fallback) and any(
        name.startswith("pose") and name.endswith(".png") for name in os.listdir(fallback)
    ):
        return fallback
    return os.path.join(REPO_ROOT, "poses")


POSES_ROOT = None  # resolved lazily so env from parent process is visible


def poses_root() -> str:
    global POSES_ROOT
    if POSES_ROOT is None:
        POSES_ROOT = _poses_root()
    return POSES_ROOT


FRAME_START_RENDER_AT  = 0
PRINT_EVERY = 10
FRAME_RATE = 30
PARTS_COUNT = 5
W_W = 1920
W_H = 1080
W_M = 20
EMOTION_POSITIVITY = [1,1,0,0,0,1]
POSE_COUNT = 30

MAX_JIGGLE_TIME = 7
BACKGROUND_COUNT = 5
PORTRAIT = False
LAYOUT_BILLBOARD = False
CHARACTER_SCALE = 1.0
CHARACTER_SIZE_SCALES = {
    "large": 1.0,
    "medium": 0.5,
    "small": 1.0 / 3.0,
}
# Landscape: occupy ~left 58% (right edge ~60%), slight outer pad, shifted toward the speaker.
TV_LANDSCAPE_WIDTH_FRAC = 0.58
TV_LANDSCAPE_OUTER_PAD_FRAC = 0.022
# Portrait: nearly full width in the top region (16:9 is width-limited).
TV_PORTRAIT_WIDTH_FRAC = 0.96
TV_PORTRAIT_TOP_FRAC = 0.03
TV_PORTRAIT_SLOT_H_FRAC = 0.50
TV_ASSET_PATHS = (
    os.path.join(REPO_ROOT, "code", "assets", "tv_billboard.png"),
    os.path.join(REPO_ROOT, "backgrounds", "tv_frame.png"),
)
TV_BLACK_MAX = 18
TV_WHITE_MIN = 240
TV_ROW_WHITE_FRAC = 0.12
origScript = []
BG_CACHE = [None, None]
_TV_GRAPHIC_CACHE = None

def _opaque_rgb(img):
    """Flatten to 100% opaque RGB. Composite over the doodle's own fill; never pale."""
    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    alpha = arr[:, :, 3].astype(np.float32) / 255.0
    rgb = arr[:, :, :3].astype(np.float32)
    solid = arr[:, :, 3] >= 12
    if solid.any():
        fill = np.median(rgb[solid], axis=0)
    else:
        fill = np.array([255.0, 255.0, 255.0], dtype=np.float32)
    out = rgb * alpha[..., None] + fill * (1.0 - alpha[..., None])
    return Image.fromarray(np.clip(np.rint(out), 0, 255).astype(np.uint8), "RGB")

def _cover(img, w, h):
    img = _opaque_rgb(img)
    iw, ih = img.size
    if iw == 0 or ih == 0:
        return Image.new("RGB", (w, h), (232, 196, 140))
    scale = max(w / iw, h / ih)
    nw = max(w, int(round(iw * scale)))
    nh = max(h, int(round(ih * scale)))
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = max(0, (nw - w) // 2)
    top = max(0, (nh - h) // 2)
    return img.crop((left, top, left + w, top + h))

def getJiggle(x, fader, multiplier):
    if x >= MAX_JIGGLE_TIME:
        return 1
    return math.exp(-fader*pow(x/multiplier,2))*math.sin(x/multiplier)

def _pale(img):
    return Image.eval(img, lambda x: int(256 - (256 - x) / 2))


def _line_image_path(imageNum):
    if not USE_BILLBOARDS:
        return None
    if imageNum < 0 or imageNum >= len(origScript):
        return None
    line = origScript[imageNum]
    if not line.strip():
        return None
    # 1-based index among non-empty lines — matches Studio parse_tagged_script / b001.png.
    slot = sum(1 for i in range(imageNum + 1) if (origScript[i] or "").strip())
    short = f"{INPUT_FILE}_billboards/{lineBillboardStem(slot)}.png"
    if os.path.isfile(short):
        return short
    legacy = f"{INPUT_FILE}_billboards/{getFilenameOfLine(line)}.png"
    return legacy if os.path.isfile(legacy) else None


def _tv_slot(flipped):
    """Box the 16:9 TV sits in: ~left 58% on landscape, top band on portrait."""
    if PORTRAIT:
        pad_x = max(8, int(round(W_W * (1.0 - TV_PORTRAIT_WIDTH_FRAC) / 2)))
        top = max(8, int(round(W_H * TV_PORTRAIT_TOP_FRAC)))
        bottom = max(top + 2, int(round(W_H * TV_PORTRAIT_SLOT_H_FRAC)))
        return (pad_x, top, W_W - pad_x, bottom)
    outer = max(8, int(round(W_W * TV_LANDSCAPE_OUTER_PAD_FRAC)))
    tv_span = max(2, int(round(W_W * TV_LANDSCAPE_WIDTH_FRAC)))
    if flipped:
        rx1 = W_W - outer
        rx0 = rx1 - tv_span
    else:
        rx0 = outer
        rx1 = rx0 + tv_span
    return (rx0, outer, rx1, W_H - outer)


def _tv_asset_path():
    for path in TV_ASSET_PATHS:
        if os.path.isfile(path):
            return path
    return None


def _longest_true_span(flags):
    best = (0, 0)
    start = None
    n = int(flags.shape[0])
    for i in range(n):
        if flags[i]:
            if start is None:
                start = i
        elif start is not None:
            if i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    if start is not None and n - start > best[1] - best[0]:
        best = (start, n)
    return best


def _knockout_near_black(arr):
    """Treat near-black as transparent unless the PNG already has a real alpha hole."""
    alpha = arr[:, :, 3]
    if (alpha < 250).mean() > 0.01:
        return arr
    rgb = arr[:, :, :3].astype(np.int16)
    near_black = rgb.max(axis=2) <= TV_BLACK_MAX
    arr = arr.copy()
    arr[:, :, 3] = np.where(near_black, 0, 255).astype(np.uint8)
    return arr


def _largest_white_screen(arr):
    """Largest near-white rounded-rect: longest high-white row/col run, then white pixels inside."""
    rgb = arr[:, :, :3].astype(np.int16)
    alpha = arr[:, :, 3]
    white = (rgb.min(axis=2) >= TV_WHITE_MIN) & (alpha > 128)
    if not white.any():
        return None, None
    h, w = white.shape
    row_on = white.mean(axis=1) >= TV_ROW_WHITE_FRAC
    col_on = white.mean(axis=0) >= TV_ROW_WHITE_FRAC
    y0, y1 = _longest_true_span(row_on)
    x0, x1 = _longest_true_span(col_on)
    if y1 - y0 < 8 or x1 - x0 < 8:
        ys, xs = np.where(white)
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        y0, y1 = int(ys.min()), int(ys.max()) + 1
    hole = np.zeros((h, w), dtype=bool)
    hole[y0:y1, x0:x1] = white[y0:y1, x0:x1]
    if not hole.any():
        return None, None
    ys, xs = np.where(hole)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return box, hole


def _crop_tv_graphic(arr, hole):
    visible = arr[:, :, 3] > 8
    if hole is not None:
        visible = visible | hole
    if not visible.any():
        return arr, hole
    ys, xs = np.where(visible)
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    cropped = arr[y0:y1, x0:x1]
    hole_c = None if hole is None else hole[y0:y1, x0:x1]
    return cropped, hole_c


def _load_tv_graphic():
    """RGBA TV overlay (screen punched out) plus screen box in overlay pixels."""
    global _TV_GRAPHIC_CACHE
    path = _tv_asset_path()
    if not path:
        return None
    stamp = (path, os.path.getmtime(path), os.path.getsize(path))
    if _TV_GRAPHIC_CACHE and _TV_GRAPHIC_CACHE[0] == stamp:
        return _TV_GRAPHIC_CACHE[1]
    arr = np.array(Image.open(path).convert("RGBA"))
    arr = _knockout_near_black(arr)
    screen_box, hole = _largest_white_screen(arr)
    arr = arr.copy()
    rgb = arr[:, :, :3].astype(np.int16)
    stray = (rgb.min(axis=2) >= 160) & (arr[:, :, 3] > 8)
    if hole is not None:
        arr[hole, 3] = 0
        stray = stray & ~hole
    if stray.any():
        arr[stray, 3] = 0
    arr, hole = _crop_tv_graphic(arr, hole)
    if hole is not None and hole.any():
        ys, xs = np.where(hole)
        screen_box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    else:
        screen_box = None
    overlay = Image.fromarray(arr, "RGBA")
    packed = (overlay, screen_box)
    _TV_GRAPHIC_CACHE = (stamp, packed)
    return packed


def _fit_in_slot(src_w, src_h, slot):
    rx0, ry0, rx1, ry1 = slot
    avail_w = max(1, int(rx1 - rx0))
    avail_h = max(1, int(ry1 - ry0))
    scale = min(avail_w / max(src_w, 1), avail_h / max(src_h, 1))
    tw = max(1, int(round(src_w * scale)))
    th = max(1, int(round(src_h * scale)))
    x = int(round(rx0 + (avail_w - tw) / 2))
    y = int(round(ry0 + (avail_h - th) / 2))
    return x, y, tw, th


def _paste_tv_billboard(frame, scribble, flipped):
    """Place the TV graphic; cover-crop the doodle into the white screen only. No drawn bezel, no pale."""
    scribble = _opaque_rgb(scribble)
    s_W, s_H = scribble.size
    if s_W < 1 or s_H < 1:
        return
    slot = _tv_slot(flipped)
    graphic = _load_tv_graphic()
    if not graphic:
        rx0, ry0, rx1, ry1 = slot
        avail_w = max(1, int(rx1 - rx0))
        avail_h = max(1, int(ry1 - ry0))
        if avail_w / avail_h > 16 / 9:
            screen_h = avail_h
            screen_w = int(round(screen_h * 16 / 9))
        else:
            screen_w = avail_w
            screen_h = int(round(screen_w * 9 / 16))
        sx = int(round(rx0 + (avail_w - screen_w) / 2))
        sy = int(round(ry0 + (avail_h - screen_h) / 2))
        screen = _cover(scribble, max(1, screen_w), max(1, screen_h))
        frame.paste(screen, (sx, sy))
        return
    overlay, screen_box = graphic
    ow, oh = overlay.size
    tv_x, tv_y, tv_w, tv_h = _fit_in_slot(ow, oh, slot)
    overlay_s = overlay.resize((tv_w, tv_h), Image.Resampling.LANCZOS)
    if screen_box:
        sx0, sy0, sx1, sy1 = screen_box
        scale_x = tv_w / ow
        scale_y = tv_h / oh
        gx0 = tv_x + int(round(sx0 * scale_x))
        gy0 = tv_y + int(round(sy0 * scale_y))
        gx1 = tv_x + int(round(sx1 * scale_x))
        gy1 = tv_y + int(round(sy1 * scale_y))
        screen_w = max(1, gx1 - gx0)
        screen_h = max(1, gy1 - gy0)
    else:
        screen_w = max(1, int(round(tv_w * 0.86)))
        screen_h = max(1, int(round(screen_w * 9 / 16)))
        gx0 = tv_x + (tv_w - screen_w) // 2
        gy0 = tv_y + max(0, int(round(tv_h * 0.04)))
    screen = _cover(scribble, screen_w, screen_h)
    frame.paste(screen, (gx0, gy0))
    frame.paste(overlay_s, (tv_x, tv_y), overlay_s)


def _compose_background(paragraph, imageNum, flipped):
    line_path = _line_image_path(imageNum)
    if not LAYOUT_BILLBOARD:
        if line_path:
            return _cover(Image.open(line_path), W_W, W_H)
        return _pale(_cover(Image.open(_background_path(paragraph)), W_W, W_H))
    # Billboard: room at full color (do not wash/pale the studio wall).
    frame = _cover(Image.open(_background_path(paragraph)), W_W, W_H)
    if line_path:
        _paste_tv_billboard(frame, Image.open(line_path), flipped)
    return frame


def drawFrame(frameNum,paragraph,emotion,imageNum,pose,phoneNum,poseTimeSinceLast,poseTimeTillNext):
    global MOUTH_COOR
    global BG_CACHE
    FLIPPED = (paragraph%2 == 1)
    cache_key = (LAYOUT_BILLBOARD, paragraph, imageNum if USE_BILLBOARDS else -1)

    if cache_key == BG_CACHE[0]:
        frame = BG_CACHE[1].copy()
    else:
        frame = _compose_background(paragraph, imageNum, FLIPPED)
        BG_CACHE = [cache_key, frame.copy()]

    if not INCLUDE_BUBBLEHEAD:
        if not os.path.isdir(FRAMES_DIR):
            os.makedirs(FRAMES_DIR)
        frame.save(os.path.join(FRAMES_DIR, "f"+"{:06d}".format(frameNum)+".png"))
        return

    s_X = 0
    if not PORTRAIT and FLIPPED:
        s_X += int(W_W/2)

    jiggleFactor = 1
    if ENABLE_JIGGLING:
        preJF = getJiggle(poseTimeSinceLast,0.06,0.6)-getJiggle(poseTimeTillNext,0.06,0.6)
        jiggleFactor = pow(1.07,preJF)

    blinker = 0
    blinkFactor = poseTimeSinceLast%60
    if blinkFactor == 57 or blinkFactor == 58:
        blinker = 2
    elif blinkFactor >= 56:
        blinker = 1

    poseIndex = emotion*5+pose
    poseIndexBlinker = poseIndex*3+blinker
    body = Image.open(os.path.join(poses_root(), "pose"+"{:04d}".format(poseIndexBlinker+1)+".png"))

    mouthImageNum = phoneNum+1
    if EMOTION_POSITIVITY[emotion] == 0:
        mouthImageNum += 11
    mouth = Image.open(os.path.join(REPO_ROOT, "mouths", "mouth"+"{:04d}".format(mouthImageNum)+".png"))

    if MOUTH_COOR[poseIndex,2] < 0:
        mouth = mouth.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if MOUTH_COOR[poseIndex,3] != 1:
        m_W, m_H = mouth.size
        mouth = mouth.resize((int(abs(m_W*MOUTH_COOR[poseIndex,2])), int(m_H*MOUTH_COOR[poseIndex,3])), Image.Resampling.LANCZOS)
    if MOUTH_COOR[poseIndex,4] != 0:
        mouth = mouth.rotate(-MOUTH_COOR[poseIndex,4],resample=Image.Resampling.BICUBIC)

    m_W, m_H = mouth.size
    body.paste(mouth,(int(MOUTH_COOR[poseIndex,0]-m_W/2),int(MOUTH_COOR[poseIndex,1]-m_H/2)),mouth)

    ow, oh = body.size
    if PORTRAIT:
        target_h = W_H * 0.50
        target_w = W_W * 0.88
        base = min(target_h / max(oh, 1), target_w / max(ow, 1))
        nh = oh * base * jiggleFactor
        nw = ow * base / max(jiggleFactor, 0.01)
        inx = (W_W - nw) / 2
        iny = W_H - nh - 16
    else:
        nh = oh*jiggleFactor
        nw = ow/jiggleFactor
        inx = W_W*0.75-nw/2
        if inx < 300:
            inx -= 50
        else:
            inx += 50
        iny = W_H-nh
    # Scale around bottom-center so feet stay on the same screen anchor.
    scale = CHARACTER_SCALE if CHARACTER_SCALE > 0 else 1.0
    if scale != 1.0:
        new_nh = nh * scale
        new_nw = nw * scale
        inx = inx + (nw - new_nw) / 2
        iny = iny + (nh - new_nh)
        nh, nw = new_nh, new_nw
    inh = max(1, int(round(nh)))
    inw = max(1, int(round(nw)))
    inx = int(round(inx))
    iny = int(round(iny))
    body = body.resize((inw,inh), Image.Resampling.LANCZOS)

    if FLIPPED:
        body = body.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    paste_x = inx if PORTRAIT else inx-s_X
    frame.paste(body,(paste_x,iny),body)
    if not os.path.isdir(FRAMES_DIR):
        os.makedirs(FRAMES_DIR)
    frame.save(os.path.join(FRAMES_DIR, "f"+"{:06d}".format(frameNum)+".png"))

def duplicateFrame(prevFrame, thisFrame):
    prevFrameFile = os.path.join(FRAMES_DIR, "f"+"{:06d}".format(prevFrame)+".png")
    thisFrameFile = os.path.join(FRAMES_DIR, "f"+"{:06d}".format(thisFrame)+".png")
    shutil.copyfile(prevFrameFile, thisFrameFile)

def infoToString(arr):
    return ','. join(map(str,arr))

def setPhoneme(i):
    global phonemeTimeline
    global phonemesPerFrame
    phoneme = phonemeTimeline[i][0]
    thisFrame = phonemeTimeline[i][1]
    nextFrame = phonemeTimeline[i+1][1]
    frameLen = nextFrame-thisFrame
    simple = [['y',0],['t',6],['f',7],['m',8]]
    prevPhoneme = 'na'
    if i >= 1:
        prevPhoneme = phonemeTimeline[i-1][0]
    nextPhoneme = phonemeTimeline[i+1][0]
    for s in simple:
        if phoneme == s[0]:
            phonemesPerFrame[thisFrame:nextFrame] = s[1]
    if phoneme == 'u':
        phonemesPerFrame[thisFrame:nextFrame] = 9
        if frameLen == 2:
            phonemesPerFrame[thisFrame+1] = 10
        elif frameLen >= 3:
            phonemesPerFrame[thisFrame+1:nextFrame-1] = 10
    elif phoneme == 'a':
        START_FORCE_OPEN = (prevPhoneme == 't' or prevPhoneme == 'y')
        END_FORCE_OPEN = (nextPhoneme == 't' or nextPhoneme == 'y')
        OPEN_TRACKS = [
        [[1],[2],[2],[2]],
        [[2,1],[1,2],[2,1],[3,2]],
        [[1,2,1],[1,3,2],[2,3,1],[2,3,2]],
        [[1,3,2,1],[1,2,3,2],[2,3,2,1],[2,3,3,2]]]
        if frameLen >= 5:
            startSize = 1
            endSize = 1
            if START_FORCE_OPEN:
                startSize = 2
            if END_FORCE_OPEN:
                endSize = 2
            for fra in range(thisFrame,nextFrame):
                starter = min(fra-thisFrame+startSize,nextFrame-1-fra+endSize)
                if starter >= 3:
                    if starter%2 == 1:
                        phonemesPerFrame[fra] = 4
                    else:
                        phonemesPerFrame[fra] = 5
                else:
                    phonemesPerFrame[fra] = (starter-1)*2
        else:
            index = 0
            if START_FORCE_OPEN:
                index += 2
            if END_FORCE_OPEN:
                index += 1
            choiceArray = OPEN_TRACKS[frameLen-1][index]
            for fra in range(thisFrame,nextFrame):
                 phonemesPerFrame[fra] = (choiceArray[fra-thisFrame]-1)*2
    if phoneme == 'a' or phoneme == 'y':
        if prevPhoneme == 'u':
            phonemesPerFrame[thisFrame] += 1
        if nextPhoneme == 'u':
            phonemesPerFrame[nextFrame-1] += 1

def timestepToFrames(timestep):
    return max(0,int(timestep*FRAME_RATE-2))

def stateOf(p):
    global indicesOn
    if indicesOn[p] == -1:
        return 0
    parts = schedules[p][indicesOn[p]].split(",")
    return int(parts[2])

def frameOf(p, offset):
    global indicesOn
    if indicesOn[p]+offset <= -1 or indicesOn[p]+offset >= len(schedules[p]):
        return -999999
    parts = schedules[p][indicesOn[p]+offset].split(",")
    timestep = float(parts[0])
    frames = timestepToFrames(timestep)
    return frames

parser = argparse.ArgumentParser(description='blah')
parser.add_argument('--input_file', type=str,  help='the script')
parser.add_argument('--use_billboards', type=str,  help='do you want to use billboards or not')
parser.add_argument('--layout', type=str, default='cover', help='cover = full-bleed illustration; billboard = 16:9 TV screen (opaque, no wood frame)')
parser.add_argument('--jiggly_transitions', type=str,  help='Do you want the stick figure to jiggle when transitioning between poses?')
parser.add_argument('--frame_caching', type=str,  help='Do you want the program to duplicate frame files if they look exactly the same? This will speed up rendering by 5x. By default, this is already enabled!')
parser.add_argument('--aspect', type=str, default='16:9', help='16:9 landscape (default) or 9:16 portrait')
parser.add_argument('--background', type=str, default='', help='studio room image path or filename (clock/wall/floor behind the stick figure)')
parser.add_argument('--frames_dir', type=str, default='', help='where to write f######.png (aspect-specific, e.g. script_frames_16x9)')
parser.add_argument('--character_size', type=str, default='large', help='bubble-head size: large (1.0), medium (0.5), small (1/3). Scaled around bottom-center so feet stay put.')
parser.add_argument('--include_bubblehead', type=str, default='T', help='T = composite yellow stick-figure narrator; F = pictures/room only (audio still muxed later)')
parser.add_argument('--start_frame', type=int, default=0, help='resume drawing from this frame index (skip already-written f######.png)')
args = parser.parse_args()
INPUT_FILE = args.input_file
USE_BILLBOARDS = (args.use_billboards == "T")
ENABLE_JIGGLING = (args.jiggly_transitions == "T")
ENABLE_FRAME_CACHING = (args.frame_caching != "F")
INCLUDE_BUBBLEHEAD = (args.include_bubblehead or "T").strip().upper() not in ("F", "FALSE", "0", "N", "NO", "OFF")
FRAME_START_RENDER_AT = max(0, int(args.start_frame or 0))
_aspect = (args.aspect or "16:9").strip().lower().replace("x", ":")
PORTRAIT = _aspect in ("9:16", "portrait", "vertical")
_layout = (args.layout or "cover").strip().lower().replace("-", "").replace("_", "")
LAYOUT_BILLBOARD = _layout in ("billboard", "billboards", "framed", "inset", "panel", "original")
_size_raw = (args.character_size or "large").strip().lower().replace("-", " ").replace("_", " ")
_size_raw = " ".join(_size_raw.split())
if _size_raw in ("medium", "med", "mid", "half", "m"):
    CHARACTER_SCALE = CHARACTER_SIZE_SCALES["medium"]
elif _size_raw in ("small", "sm", "tiny", "third", "s"):
    CHARACTER_SCALE = CHARACTER_SIZE_SCALES["small"]
else:
    CHARACTER_SCALE = CHARACTER_SIZE_SCALES["large"]
SELECTED_BG = (args.background or "").strip()
FRAMES_DIR = (args.frames_dir or "").strip() or (INPUT_FILE + "_frames")
if PORTRAIT:
    W_W, W_H = 1080, 1920
else:
    W_W, W_H = 1920, 1080

f = open(INPUT_FILE+"_schedule.csv","r+")
scheduleLines = f.read().split("\nSECTION\n")
f.close()

schedules = [None]*PARTS_COUNT
for i in range(PARTS_COUNT):
    schedules[i] = scheduleLines[i].split("\n")
    if i == 4:
        schedules[i] = schedules[i][0:-1]
lastParts = schedules[-1][-2].split(",")
lastTimestamp = float(lastParts[0])
FRAME_COUNT = timestepToFrames(lastTimestamp+1)
phonemeTimeline = []
for i in range(len(schedules[4])):
    parts = schedules[4][i].split(",")
    timestamp = float(parts[0])
    framestamp = timestepToFrames(timestamp)
    if i >= 1 and framestamp <= phonemeTimeline[-1][1]: # we have a 0-frame phoneme! Try to fix it.
        if i >= 2 and phonemeTimeline[-2][1] <= framestamp-2:
            phonemeTimeline[-1][1] = framestamp-1 # shift previous one back
        else:
            framestamp += 1 # shift current one forward
    phoneme = parts[2]
    phonemeTimeline.append([phoneme,framestamp])
phonemeTimeline.append(["end",FRAME_COUNT])
phonemesPerFrame = np.zeros(FRAME_COUNT,dtype='int32')
for i in range(len(phonemeTimeline)-1):
    setPhoneme(i)

f = open(INPUT_FILE+".txt","r+")
origScript = f.read().split("\n")
f.close()
# Keep blank lines: scheduler increments image by 2 on a section break, matching this index.


def _default_shared_background():
    folders = [
        os.path.join(REPO_ROOT, "backgrounds"),
        os.path.join(REPO_ROOT, "user_data", "backgrounds"),
    ]
    preferred = os.path.join(folders[0], "bga0.png")
    if os.path.isfile(preferred):
        return preferred
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        names = sorted(
            n for n in os.listdir(folder)
            if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
        )
        if names:
            return os.path.join(folder, names[0])
    return None


def _selected_background_path():
    if not SELECTED_BG:
        return None
    if os.path.isfile(SELECTED_BG):
        return SELECTED_BG
    name = os.path.basename(SELECTED_BG)
    for folder in (
        os.path.join(REPO_ROOT, "backgrounds"),
        os.path.join(REPO_ROOT, "user_data", "backgrounds"),
    ):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    print(f"Background: selected file missing ({SELECTED_BG}); falling back to default.")
    return None


def _background_path(paragraph):
    selected = _selected_background_path()
    if selected:
        return selected
    custom = f"{INPUT_FILE}_backgrounds/bg{paragraph}.png"
    if os.path.isfile(custom):
        return custom
    shared = os.path.join(REPO_ROOT, "backgrounds", "bga"+str(paragraph % BACKGROUND_COUNT)+".png")
    if os.path.isfile(shared):
        return shared
    default = _default_shared_background()
    if default and os.path.isfile(default):
        return default
    varied_name = "fallback_bg"+str(paragraph % 8)+("_9x16.png" if PORTRAIT else ".png")
    varied = os.path.join(REPO_ROOT, "studio", "assets", varied_name)
    if os.path.isfile(varied):
        return varied
    return os.path.join(REPO_ROOT, "studio", "assets", "fallback_bg.png")

f = open(os.path.join(REPO_ROOT, "code", "mouthCoordinates.csv"), "r+")
mouthCoordinatesStr = f.read().split("\n")
f.close()
MOUTH_COOR = np.zeros((POSE_COUNT,5))
for i in range(len(mouthCoordinatesStr)):
    parts = mouthCoordinatesStr[i].split(",")
    for j in range(5):
        MOUTH_COOR[i,j] = float(parts[j])
MOUTH_COOR[:,0:2] *= 3 #upscale for 1080p, not 360p

lastFrameInfo = None
FRAME_CACHES = {}
indicesOn = [-1]*(PARTS_COUNT-1)
for frame in range(0,FRAME_COUNT):
    for p in range(PARTS_COUNT-1):
        frameOfNext = frameOf(p,1)
        if frameOfNext >= 0 and frame >= frameOfNext:
            indicesOn[p] += 1
    paragraph = stateOf(0)
    emotion = stateOf(1)
    imageNum = stateOf(2)
    pose = stateOf(3)
    timeSincePrevPoseChange = frame-frameOf(3,0)
    timeUntilNextPoseChange = frameOf(3,1)-frame

    TSPPC_cache = min(timeSincePrevPoseChange, MAX_JIGGLE_TIME) if ENABLE_JIGGLING else 0
    TUNPC_cache = min(timeUntilNextPoseChange, MAX_JIGGLE_TIME) if ENABLE_JIGGLING else 0
    IMAGE_cache = imageNum if USE_BILLBOARDS else 0

    thisFrameInfo = infoToString([paragraph, emotion, IMAGE_cache, pose, phonemesPerFrame[frame], TSPPC_cache, TUNPC_cache])
    if ENABLE_FRAME_CACHING and thisFrameInfo not in FRAME_CACHES:
        FRAME_CACHES[thisFrameInfo] = frame

    if frame >= FRAME_START_RENDER_AT:
        if ENABLE_FRAME_CACHING and FRAME_CACHES[thisFrameInfo] < frame:
            duplicateFrame(FRAME_CACHES[thisFrameInfo], frame)
        else:
            drawFrame(frame,paragraph,emotion,imageNum,pose,phonemesPerFrame[frame],timeSincePrevPoseChange,timeUntilNextPoseChange)
        if frame%PRINT_EVERY == 0 or frame == FRAME_COUNT-1:
            print(f"Just drew frame {frame+1} / {FRAME_COUNT}")
