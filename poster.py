#!/usr/bin/env python3
"""NIX SPEECH 01 — GitHub Actions FB poster
Pulls the newest vault video (short or long) from the HF Space and posts it to
the Facebook Page as a native Reel/Video — from GitHub's runners, which FB
does NOT block (unlike HF cloud IPs).

Only posts videos that have NOT been crossposted yet (state kept on the Space).
Env vars:
  SPACE_URL        https://mdh-zone-yt-tiktok-nix-speech-o1.hf.space
  XP_KEY           CROSSPOST_KEY secret (HF side)
  FB_PAGE_ID       Nix Speech o1 page id
  FB_ACCESS_TOKEN  long-lived page token
"""
import os, sys, json, io, zipfile, time, uuid, urllib.request, urllib.parse, glob

SPACE = os.getenv("SPACE_URL", "").rstrip("/")
KEY = os.getenv("XP_KEY", "")
PAGE = os.getenv("FB_PAGE_ID", "")
TOK = os.getenv("FB_ACCESS_TOKEN", "")
KINDS = ["short", "long"]          # try shorts first
CHUNK = 4 * 1024 * 1024


def http(url, data=None, method=None, headers=None, timeout=180, binary=False):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if binary else r.read().decode()


def gradio_call(fn, payload, want="json"):
    """POST to a gradio endpoint, stream the event, return outputs."""
    url = f"{SPACE}/gradio_api/call/{fn}"
    body = json.dumps({"data": payload}).encode()
    try:
        resp = http(url, data=body, method="POST",
                    headers={"Content-Type": "application/json"}, timeout=120)
    except Exception as e:
        raise RuntimeError(f"gradio POST {fn} failed: {e}")
    ev = json.loads(resp)
    eid = ev.get("event_id")
    if not eid:
        raise RuntimeError(f"no event_id from {fn}: {resp[:200]}")
    # stream until 'complete'
    sse = http(f"{url}/{eid}", timeout=300)
    out = None
    for ln in sse.splitlines():
        if ln.startswith("data:"):
            d = ln[5:].strip()
            if d and d != "null":
                try:
                    out = json.loads(d)
                except Exception:
                    out = d
    return out


def last_posted(kind):
    try:
        r = gradio_call("xp_last_posted", [kind])
        if isinstance(r, list) and r:
            return str(r[0]).strip()
        return str(r).strip() if r else ""
    except Exception as e:
        print(f"[warn] last_posted {kind}: {e}")
        return ""


def fetch_video(kind):
    """Return (video_id, mp4_bytes, json_meta_dict) of newest unposted video or None."""
    out = gradio_call("xp_fetch_video", [kind, KEY])
    if not out:
        return None
    val = out
    if isinstance(out, list):
        val = out[0] if out else None
    if not val:
        return None
    # value may be dict {path,url} or JSON string of dict, or string path
    d = None
    if isinstance(val, dict):
        d = val
    elif isinstance(val, str):
        try:
            d = json.loads(val)
        except Exception:
            d = {"path": val, "url": None}
    path = (d or {}).get("path") or ""
    url = (d or {}).get("url") or ""
    if not path and not url:
        return None
    dl = url if url.startswith("http") else (
        url if url.startswith("/") and SPACE else path)
    if url.startswith("/"):
        dl = SPACE + url
    elif not dl.startswith("http"):
        dl = SPACE + "/gradio_api/file=" + urllib.parse.quote(path)
    data = http(dl, timeout=600, binary=True)
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    mp4 = next((n for n in names if n.endswith(".mp4")), None)
    if not mp4:
        return None
    vid = os.path.basename(mp4)[:-4]
    meta = {}
    js = next((n for n in names if n.endswith(".json")), None)
    if js:
        try:
            meta = json.loads(z.read(js))
        except Exception:
            pass
    return vid, z.read(mp4), meta


def mark(kind, vid):
    r = gradio_call("xp_mark_posted", [kind, vid, KEY])
    print(f"[mark] {kind} {vid} -> {r}")


def fb_call(path, params=None, data=None, files=None, method="POST"):
    url = f"https://graph.facebook.com/v23.0/{path}"
    params = dict(params or {})
    params.setdefault("access_token", TOK)
    if files:
        b = uuid.uuid4().hex
        body = b""
        for k, v in params.items():
            body += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (fn, content, ct) in files.items():
            body += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{fn}\"\r\nContent-Type: {ct}\r\n\r\n").encode() + content + b"\r\n"
        body += f"--{b}--\r\n".encode()
        return json.loads(http(url, data=body, method="POST",
                               headers={"Content-Type": f"multipart/form-data; boundary={b}"},
                               timeout=900))
    sep = "&" if "?" in url else "?"
    full = url + sep + urllib.parse.urlencode(params)
    # IMPORTANT: default to POST for upload-phase calls (start/finish pass
    # params only, no body). A GET here returns a video LIST {"data":[]} and
    # the upload never starts. Page reads must pass method="GET" explicitly.
    method = method if method else "POST"
    return json.loads(http(full, data=urllib.parse.urlencode(data or {}).encode(),
                           method=method, timeout=300))


def _rupload(upload_url, data, token):
    """Direct-upload a full file to a Facebook rupload URL (Reels).
    NOTE: the offset header here is 'offset' (not 'file_offset') — FB returns
    'HeaderValuePredicate: Header Offset not convertable to unsigned long'
    if the wrong header name is used."""
    req = urllib.request.Request(upload_url, data=data, method="POST")
    req.add_header("Content-Type", "application/octet-stream")
    req.add_header("Authorization", f"OAuth {token}")
    req.add_header("offset", "0")
    req.add_header("X-Entity-Length", str(len(data)))
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode())


def _resumable_upload(ep, mp4_bytes, title):
    """Resumable session upload for {page}/{ep} (used for long 'videos')."""
    js = fb_call(f"{PAGE}/{ep}",
                 params={"upload_phase": "start", "file_size": len(mp4_bytes),
                         "title": title})
    if "upload_session_id" not in js:
        raise RuntimeError(f"FB start-phase had no upload_session_id: {json.dumps(js)[:400]}")
    sid = js["upload_session_id"]
    new_id = js.get("video_id", "")
    off = int(js.get("start_offset", 0))
    size = len(mp4_bytes)
    while off < size:
        chunk = mp4_bytes[off:off + CHUNK]
        js = fb_call(f"{PAGE}/{ep}",
                     data={"upload_phase": "transfer", "upload_session_id": sid,
                           "start_offset": off},
                     files={"video_file_chunk": ("c.mp4", chunk,
                                                 "application/octet-stream")})
        off = int(js.get("start_offset", off + len(chunk)))
        print(f"   {off}/{size}")
    fin = fb_call(f"{PAGE}/{ep}",
                  params={"upload_phase": "finish", "upload_session_id": sid,
                          "video_id": new_id})
    return fin.get("video_id") or new_id


def post_fb(kind, vid, mp4_bytes, meta):
    """Post a video to the page. Reels (shorts) use the rupload DIRECT upload
    protocol; long videos use the resumable-session protocol."""
    ep = "video_reels" if kind == "short" else "videos"
    title = (meta or {}).get("title", "Nix Speech o1")[:100]
    desc = (meta or {}).get("description", "") or ""
    credit = (meta or {}).get("music_credit", "")
    if credit and credit not in desc:
        desc = f"{desc}\n\n{credit}"
    print(f"[fb] posting {kind} {vid} -> {ep}: {title}")

    # start phase (POST)
    js = fb_call(f"{PAGE}/{ep}",
                 params={"upload_phase": "start", "file_size": len(mp4_bytes),
                         "title": title})

    if kind == "short" and js.get("upload_url"):
        # ---- Reels: direct upload to rupload ----
        _rupload(js["upload_url"], mp4_bytes, TOK)
        print("   direct upload OK")
        fin = fb_call(f"{PAGE}/{ep}",
                      params={"upload_phase": "finish",
                              "video_id": js.get("video_id", ""),
                              "title": title})
        if desc:
            fb_call(f"{PAGE}/{ep}",
                    params={"upload_phase": "finish",
                            "video_id": js.get("video_id", ""),
                            "description": desc[:5000]})
        new_id = fin.get("post_id") or fin.get("video_id") or js.get("video_id", "")
        print(f"[fb] DONE {kind} id={new_id}")
        return new_id
    else:
        # ---- Videos (or reels that gave a session): resumable ----
        new_id = _resumable_upload(ep, mp4_bytes, title)
        meta_payload = {"title": title}
        if desc:
            meta_payload["description"] = desc[:5000]
        try:
            fb_call(str(new_id), data=meta_payload)
        except Exception as e:
            print(f"[warn] meta set failed: {e}")
        print(f"[fb] DONE {kind} id={new_id}")
        return new_id


def main():
    if not (SPACE and KEY and PAGE and TOK):
        print("Missing env (SPACE_URL/XP_KEY/FB_PAGE_ID/FB_ACCESS_TOKEN)")
        return 2
    # sanity: page check
    try:
        js = fb_call(str(PAGE), params={"fields": "name,id"}, method="GET")
        print(f"[fb] page ok: {js.get('name')}")
    except Exception as e:
        print(f"[fb] page check FAILED: {e}")
        return 1
    posted_any = False
    for kind in KINDS:
        try:
            last = last_posted(kind)
            got = fetch_video(kind)
            if not got:
                print(f"[{kind}] nothing new")
                continue
            vid, data, meta = got
            if vid == last:
                print(f"[{kind}] {vid} already crossposted — skip")
                continue
            post_fb(kind, vid, data, meta)
            mark(kind, vid)
            posted_any = True
        except Exception as e:
            print(f"[{kind}] ERROR: {e}")
    print("ALL DONE" if posted_any else "NOTHING NEW")
    return 0


if __name__ == "__main__":
    sys.exit(main())
