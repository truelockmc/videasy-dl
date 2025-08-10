import os
import sys
import re
import requests
import tempfile
import concurrent.futures
import time
import threading
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
            print(f"\n[!] Error while downloading {url} (Attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                raise
            time.sleep(backoff * attempt)  # exponential backoff

def is_master_playlist(m3u8_content):
    return "#EXT-X-STREAM-INF" in m3u8_content

def list_playlists(m3u8_content, base_url):
    """
    Return a list of tuples: (resolution_str, playlist_url)
    """
    lines = m3u8_content.strip().splitlines()
    playlists = []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            # Try to find RESOLUTION
            m = re.search(r'RESOLUTION=(\d+x\d+)', line)
            res = m.group(1) if m else "Unknown resolution"
            playlist_url = urljoin(base_url, lines[i+1].strip())
            playlists.append((res, playlist_url))
    return playlists

def pick_playlist_by_index(playlists, index):
    if index < 1 or index > len(playlists):
        raise ValueError("Invalid playlist selection")
    return playlists[index-1][1]

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
        if "-->" in lines[0]:  # timestamp without counter
            lines.insert(0, str(counter))
            counter += 1
        srt.append("\n".join(lines))
    return "\n\n".join(srt).replace("WEBVTT\n", "").strip()

def print_progress_bar(current, total, bar_length=40):
    fraction = current / total
    filled_length = int(bar_length * fraction)
    bar = "█" * filled_length + '-' * (bar_length - filled_length)
    print(f"\rProgress: |{bar}| {current}/{total} segments", end='', flush=True)

def main(m3u8_url, output_file="output.mp4"):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://player.videasy.net",
        "Referer": "https://player.videasy.net/"
    }
    session = requests.Session()
    session.headers.update(headers)

    print(f"[+] Downloading master playlist: {m3u8_url}")
    master_content = download_text(session, m3u8_url)

    # Find subtitles URLs from master playlist
    subtitle_urls = find_subtitles(master_content, m3u8_url)

    # Check if master playlist
    if is_master_playlist(master_content):
        playlists = list_playlists(master_content, m3u8_url)
        if not playlists:
            print("[!] No playlists found in master playlist.")
            return
        print("\nAvailable video resolutions:")
        for i, (res, _) in enumerate(playlists, 1):
            print(f"  {i}. {res}")
        while True:
            choice = input(f"Select resolution [1-{len(playlists)}]: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(playlists):
                chosen_index = int(choice)
                break
            print("Invalid input, please enter a valid number.")
        selected_playlist_url = playlists[chosen_index - 1][1]
        print(f"\n[+] Selected playlist: {playlists[chosen_index - 1][0]} - {selected_playlist_url}\n")
        playlist_content = download_text(session, selected_playlist_url)
    else:
        # No master playlist, just use given url content
        playlist_content = master_content

    segments = parse_segments(playlist_content, m3u8_url if not is_master_playlist(master_content) else selected_playlist_url)
    if not segments:
        raise RuntimeError("No segments found in playlist.")
    print(f"[+] Found {len(segments)} segments. Downloading in parallel...")

    temp_ts = tempfile.NamedTemporaryFile(delete=False, suffix=".ts").name

    with open(temp_ts, "wb") as f:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            # To track progress safely across threads, use a thread-safe counter
            progress = {'count': 0}
            lock = threading.Lock()

            def download_and_track(url):
                data = download_binary_with_retry(session, url)
                with lock:
                    progress['count'] += 1
                    print_progress_bar(progress['count'], len(segments))
                return data

            for data in executor.map(download_and_track, segments):
                f.write(data)

    print("\n[+] All segments downloaded.")

    sub_file = None
    if subtitle_urls:
        print(f"[+] Downloading subtitles from: {subtitle_urls[0]}")
        vtt_text = download_text(session, subtitle_urls[0])
        srt_text = vtt_to_srt(vtt_text)
        sub_file = tempfile.NamedTemporaryFile(delete=False, suffix=".srt").name
        with open(sub_file, "w", encoding="utf-8") as sf:
            sf.write(srt_text)

    print("[+] Muxing video (and subtitles if present) with ffmpeg...")

    if sub_file:
        os.system(f'ffmpeg -y -i "{temp_ts}" -i "{sub_file}" -c copy -c:s mov_text "{output_file}"')
    else:
        os.system(f'ffmpeg -y -i "{temp_ts}" -c copy "{output_file}"')

    os.remove(temp_ts)
    if sub_file:
        os.remove(sub_file)

    print(f"[+] Done! Output saved as: {output_file}")

def get_user_input():
    print("Welcome to the m3u8 Video Downloader")
    print("Please enter the URL of the m3u8 playlist (e.g. https://example.com/playlist.m3u8):")
    m3u8_url = input("M3U8 URL: ").strip()
    while not m3u8_url:
        print("URL cannot be empty. Please enter a valid URL:")
        m3u8_url = input("M3U8 URL: ").strip()

    # Erstelle eine temporäre Session und lade die Playlist um Auflösungen zu ermitteln
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://player.videasy.net",
        "Referer": "https://player.videasy.net/"
    })

    print("\nFetching available video qualities...")
    master_content = download_text(session, m3u8_url)

    selected_playlist_url = m3u8_url  # default fallback

    if is_master_playlist(master_content):
        playlists = list_playlists(master_content, m3u8_url)
        if not playlists:
            print("[!] No playlists found in master playlist, using original URL.")
        else:
            print("\nAvailable video resolutions:")
            for i, (res, _) in enumerate(playlists, 1):
                print(f"  {i}. {res}")
            while True:
                choice = input(f"Select resolution [1-{len(playlists)}]: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(playlists):
                    chosen_index = int(choice)
                    selected_playlist_url = playlists[chosen_index - 1][1]
                    break
                print("Invalid input, please enter a valid number.")
    else:
        print("[+] No master playlist detected, using the provided URL.")

    print("\nEnter desired output filename (e.g. myvideo.mp4). If no extension is given, '.mp4' will be added:")
    output_file = input("Output filename: ").strip()
    if not output_file:
        output_file = "video.mp4"
    elif '.' not in output_file:
        output_file += ".mp4"

    return selected_playlist_url, output_file

if __name__ == "__main__":
    if len(sys.argv) < 2:
        # No args, interactive mode
        url, outfile = get_user_input()
        main(url, outfile)
    else:
        # Args given, use args but still ask for resolution interactively inside main
        m3u8_link = sys.argv[1]
        outfile = sys.argv[2] if len(sys.argv) > 2 else "video.mp4"
        if '.' not in outfile:
            outfile += ".mp4"
        main(m3u8_link, outfile)
