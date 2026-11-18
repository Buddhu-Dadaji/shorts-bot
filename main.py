#!/usr/bin/env python3
import asyncio, json, os, re, subprocess, tempfile, shutil
from pathlib import Path
import requests
from groq import Groq
import edge_tts
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
TOPICS = ROOT / "topics.txt"
WORK = Path(tempfile.mkdtemp())
W, H, FPS = 1080, 1920, 30
VOICE = os.getenv("VOICE", "en-US-ChristopherNeural")
MODELS = [m for m in (os.getenv("GROQ_MODEL"), "openai/gpt-oss-120b",
          "llama-3.3-70b-versatile", "qwen/qwen3-32b",
          "meta-llama/llama-4-scout-17b-16e-instruct") if m]
DRY = os.getenv("DRY_RUN", "false").lower() == "true"
CREDITS = []


def sh(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stderr[-3000:])
        raise SystemExit("ffmpeg failed")


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"index": 0, "done": []}


def next_topic(state):
    topics = [t.strip() for t in TOPICS.read_text().splitlines() if t.strip()]
    if not topics:
        raise SystemExit("topics.txt is empty")
    if len(state["done"]) >= len(topics):
        state["done"] = []
    for _ in range(len(topics) + 1):
        t = topics[state["index"] % len(topics)]
        state["index"] += 1
        if t not in state["done"]:
            return t
    raise SystemExit("no unused topics")


REQUIRED = ("hook", "script", "title", "description", "tags", "queries")


def write_script(topic):
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    prompt = (
        f"You write YouTube Shorts.\nTopic: {topic}\n"
        "Return ONLY minified JSON with keys: "
        "hook (max 8 words, ALL CAPS, no period), "
        "script (55-75 words, spoken narration, second person, punchy, no emojis, no markdown), "
        "title (max 60 chars, no hashtags), description (1-2 sentences), "
        "tags (array of 8 short strings), "
        "queries (array of 3 stock-footage search terms, 1-3 words each)."
    )
    errors = []
    for model in MODELS:
        for use_json in (True, False):
            try:
                kw = {"response_format": {"type": "json_object"}} if use_json else {}
                r = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.9,
                    max_tokens=2000,
                    **kw,
                )
                txt = (r.choices[0].message.content or "").strip()
                m = re.search(r"\{.*\}", txt, re.S)
                if not m:
                    errors.append(f"{model}: no JSON ({txt[:100]!r})")
                    continue
                data = json.loads(m.group(0))
                if all(k in data for k in REQUIRED):
                    print("model:", model)
                    return data
                errors.append(f"{model}: missing keys")
            except Exception as e:
                errors.append(f"{model}: {type(e).__name__} {str(e)[:100]}")
    raise SystemExit("all models failed:\n" + "\n".join(errors))


async def make_voice(text):
    mp3 = WORK / "vo.mp3"
    words = []
    comm = edge_tts.Communicate(text, VOICE)
    with open(mp3, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"], ch["duration"], ch["text"]))
    return mp3, words


def ass_time(t):
    cs = int(t // 100000)
    s, cs = divmod(cs, 100)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass(words, path):
    out = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {W}", f"PlayResY: {H}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Cap,DejaVu Sans,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,3,5,60,60,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i in range(0, len(words), 3):
        grp = words[i:i + 3]
        start = grp[0][0]
        end = grp[-1][0] + grp[-1][1]
        txt = " ".join(w[2] for w in grp).replace("\n", " ")
        out.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Cap,,0,0,0,,{txt}")
    path.write_text("\n".join(out), encoding="utf-8")


def _download(url, path):
    try:
        with requests.get(url, stream=True, timeout=180) as r:
            if r.status_code != 200:
                return False
            path.write_bytes(r.content)
        return True
    except Exception:
        return False


def _kenburns(img, out, secs):
    sh(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-t", f"{secs:.2f}",
        "-vf", f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,"
               f"crop={W * 2}:{H * 2},"
               f"zoompan=z='min(zoom+0.0012,1.18)':d={int(secs * FPS)}:s={W}x{H}:fps={FPS}",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", str(out)])


def _grab_video(url, dur, credit, got, total):
    p = WORK / f"clip{len(got)}.mp4"
    if not _download(url, p):
        return total
    take = min(dur, 6)
    got.append((p, take))
    CREDITS.append(credit)
    return total + take


def _grab_image(url, credit, got, total):
    img = WORK / f"img{len(got)}.jpg"
    if not _download(url, img):
        return total
    seg = WORK / f"kb{len(got)}.mp4"
    try:
        _kenburns(img, seg, 4.0)
    except SystemExit:
        return total
    got.append((seg, 4.0))
    CREDITS.append(credit)
    return total + 4.0


def _pixabay(q):
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://pixabay.com/api/videos/",
                         params={"key": key, "q": q, "video_type": "film",
                                 "per_page": 20, "safesearch": "true",
                                 "min_height": 1080},
                         timeout=60)
        if r.status_code != 200:
            print("pixabay status", r.status_code)
            return []
        hits = r.json().get("hits", [])
    except Exception:
        return []

    def vert(h):
        v = h.get("videos", {}).get("medium", {})
        return (v.get("height") or 0) > (v.get("width") or 0)

    hits.sort(key=vert, reverse=True)
    out = []
    for h in hits:
        v = h.get("videos", {}).get("medium") or h.get("videos", {}).get("small")
        if not v or not v.get("url") or (h.get("duration") or 0) < 2:
            continue
        out.append((v["url"], h["duration"], f"{h.get('user', 'pixabay')}/Pixabay"))
    return out


def _openverse(q):
    try:
        r = requests.get("https://api.openverse.org/v1/images/",
                         params={"q": q, "license_type": "commercial", "page_size": 5},
                         timeout=60)
        if r.status_code != 200:
            return []
        return [(i["url"], 4.0, f"{i.get('creator', 'openverse')}/Openverse")
                for i in r.json().get("results", []) if i.get("url")]
    except Exception:
        return []


def _wikimedia(q):
    try:
        r = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            headers={"User-Agent": "shorts-bot/1.0 (personal channel automation)"},
            params={"action": "query", "format": "json", "generator": "search",
                    "gsrsearch": f"filetype:bitmap {q}", "gsrlimit": 5,
                    "gsrnamespace": 6, "prop": "imageinfo",
                    "iiprop": "url", "iiurlwidth": 1280},
            timeout=60,
        )
        if r.status_code != 200:
            return []
        pages = r.json().get("query", {}).get("pages", {})
    except Exception:
        return []
    out = []
    for p in pages.values():
        ii = (p.get("imageinfo") or [{}])[0]
        u = ii.get("thumburl") or ii.get("url")
        if u:
            out.append((u, 4.0, "Wikimedia Commons"))
    return out


def get_clips(queries, need):
    got, total = [], 0.0

    for q in queries:
        if total >= need:
            break
        for url, dur, credit in _pixabay(q):
            if total >= need:
                break
            total = _grab_video(url, dur, credit, got, total)

    if total < need:
        for source in (_openverse, _wikimedia):
            for q in queries:
                if total >= need:
                    break
                for url, _d, credit in source(q):
                    if total >= need:
                        break
                    total = _grab_image(url, credit, got, total)

    if not got:
        seg = WORK / "fallback.mp4"
        sh(["ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=0x101820:s={W}x{H}:d={need:.2f}:r={FPS}",
            "-vf", "noise=alls=10:allf=t", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(seg)])
        got.append((seg, need))
        CREDITS.append("gradient")

    print("footage sources:", ", ".join(CREDITS[:6]) or "gradient fallback")
    return got


def dur_of(path):
    o = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    return float(o)


def build_video(clips, seconds):
    segs, left = [], seconds
    for p, d in clips:
        if left <= 0:
            break
        take = min(d, left)
        out = WORK / f"seg{len(segs)}.mp4"
        sh(["ffmpeg", "-y", "-i", str(p), "-t", f"{take:.2f}",
            "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS}",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", str(out)])
        segs.append(out)
        left -= take
    lst = WORK / "list.txt"
    lst.write_text("\n".join(f"file '{s}'" for s in segs))
    silent = WORK / "silent.mp4"
    sh(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(silent)])
    return silent


def finalize(silent, vo, ass, out):
    sh(["ffmpeg", "-y", "-i", str(silent), "-i", str(vo),
        "-vf", f"ass={ass}",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)])


def upload(path, meta):
    creds = Credentials(
        None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    desc = meta["description"]
    if CREDITS:
        desc += "\n\nFootage: " + ", ".join(dict.fromkeys(CREDITS[:6]))
    body = {
        "snippet": {
            "title": (meta["title"][:92] + " #Shorts"),
            "description": desc + "\n\n#Shorts",
            "tags": meta["tags"][:15],
            "categoryId": "24",
        },
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(path), chunksize=4 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"upload {int(status.progress() * 100)}%")
    return resp["id"]


def main():
    state = load_state()
    topic = next_topic(state)
    print("topic:", topic)

    meta = write_script(topic)
    print("hook:", meta["hook"])

    mp3, words = asyncio.run(make_voice(meta["script"]))
    vo = WORK / "vo.wav"
    sh(["ffmpeg", "-y", "-i", str(mp3), "-ar", "44100", "-ac", "1", str(vo)])

    total = dur_of(vo) + 0.6
    ass = WORK / "cap.ass"
    build_ass(words, ass)

    clips = get_clips(meta["queries"], total)
    silent = build_video(clips, total)
    out = WORK / "final.mp4"
    finalize(silent, vo, ass, out)
    print(f"built {out.stat().st_size / 1e6:.1f} MB")

    if DRY:
        shutil.copy(out, ROOT / "preview.mp4")
        print("dry run complete")
    else:
        vid = upload(out, meta)
        print("LIVE: https://youtube.com/shorts/" + vid)
        state["done"].append(topic)

    STATE.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
