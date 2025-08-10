import os
import sys
import re
import requests
import tempfile
import concurrent.futures
import time
from urllib.parse import urljoin

def download_text(session, url):
    r = session.get(url)
    r.raise_for_status()
    return r.text

def download_binary_with_retry(session, url, max_retries=3, backoff=1):
    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(url, stream=True, timeout=10)
            r.raise_for_status()
            return r.content
        except (requests.exceptions.RequestException, requests.exceptions.ChunkedEncodingError) as e:
            print(f"\n[!] Fehler beim Download {url} (Versuch {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                raise
            time.sleep(backoff * attempt)  # exponentielles Backoff

def is_master_playlist(m3u8_content):
    return "#EXT-X-STREAM-INF" in m3u8_content

def pick_720p_playlist(m3u8_content, base_url):
    lines = m3u8_content.strip().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF") and "RESOLUTION=1280x720" in line:
            return urljoin(base_url, lines[i+1].strip())
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            return urljoin(base_url, lines[i+1].strip())
    raise RuntimeError("Keine passende Playlist gefunden")

def parse_segments(m3u8_content, base_url):
    return [urljoin(base_url, line.strip()) for line in m3u8_content.splitlines() if not line.startswith("#") and line.strip()]

def find_subtitles(m3u8_content, base_url):
    subs = []
    for line in m3u8_content.splitlines():
        if line.startswith("#EXT-X-MEDIA") and "TYPE=SUBTITLES" in line:
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                subs.append(urljoin(base_url, m.group(1)))
    return subs

def vtt_to_srt(vtt_text):
    srt = []
    counter = 1
    for block in vtt_text.replace("\r", "").split("\n\n"):
        lines = block.strip().splitlines()
        if not lines:
            continue
        if "-->" in lines[0]:  # Zeitstempel ohne Zähler
            lines.insert(0, str(counter))
            counter += 1
        srt.append("\n".join(lines))
    return "\n\n".join(srt).replace("WEBVTT\n", "").strip()

def main(m3u8_url, output_file="output.mp4"):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://player.videasy.net",
        "Referer": "https://player.videasy.net/"
    }
    session = requests.Session()
    session.headers.update(headers)

    print(f"[+] Lade Playlist: {m3u8_url}")
    master_content = download_text(session, m3u8_url)

    # Untertitel-Links extrahieren
    subtitle_urls = find_subtitles(master_content, m3u8_url)

    # Falls Master-Playlist → auf 720p wechseln
    if is_master_playlist(master_content):
        print("[+] Master-Playlist erkannt, suche 720p...")
        m3u8_url = pick_720p_playlist(master_content, m3u8_url)
        print(f"[+] 720p-Playlist: {m3u8_url}")
        playlist_content = download_text(session, m3u8_url)
    else:
        playlist_content = master_content

    # Segmente laden (parallel, mit Retry)
    segments = parse_segments(playlist_content, m3u8_url)
    if not segments:
        raise RuntimeError("Keine Segmente gefunden.")
    print(f"[+] {len(segments)} Segmente gefunden. Lade parallel...")

    temp_ts = tempfile.NamedTemporaryFile(delete=False, suffix=".ts").name
    with open(temp_ts, "wb") as f:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            download_func = lambda url: download_binary_with_retry(session, url)
            for i, data in enumerate(executor.map(download_func, segments), start=1):
                f.write(data)
                print(f"\r   Segment {i}/{len(segments)}", end="", flush=True)

    print("\n[+] Segmente geladen.")

    # Untertitel laden (falls vorhanden)
    sub_file = None
    if subtitle_urls:
        print(f"[+] Lade Untertitel von: {subtitle_urls[0]}")
        vtt_text = download_text(session, subtitle_urls[0])
        srt_text = vtt_to_srt(vtt_text)
        sub_file = tempfile.NamedTemporaryFile(delete=False, suffix=".srt").name
        with open(sub_file, "w", encoding="utf-8") as sf:
            sf.write(srt_text)

    # Video + evtl. Untertitel muxen
    if sub_file:
        os.system(f'ffmpeg -y -i "{temp_ts}" -i "{sub_file}" -c copy -c:s mov_text "{output_file}"')
    else:
        os.system(f'ffmpeg -y -i "{temp_ts}" -c copy "{output_file}"')

    os.remove(temp_ts)
    if sub_file:
        os.remove(sub_file)

    print(f"[+] Fertig: {output_file}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Nutzung: python videasy_dl.py <m3u8_url> [output.mp4]")
        sys.exit(1)
    m3u8_link = sys.argv[1]
    outfile = sys.argv[2] if len(sys.argv) > 2 else "video.mp4"
    main(m3u8_link, outfile)
