#!/usr/bin/env python3
import argparse
import base64
import json
import os
import socket
import struct
import threading
from typing import Optional

HEADER_SIZE = 4
BUFFER_SIZE = 4096
DOWNLOAD_DIR = "downloads"
MAX_JSON_BYTES = 50 * 1024 * 1024
stop_event = threading.Event()
print_lock = threading.Lock()


def ensure_download_dir() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def safe_filename(filename: str) -> str:
    return os.path.basename(filename.strip())


def recv_exact(sock: socket.socket, length: int) -> Optional[bytes]:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(min(BUFFER_SIZE, length - len(data)))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def recv_json(sock: socket.socket) -> Optional[dict]:
    header = recv_exact(sock, HEADER_SIZE)
    if not header:
        return None
    msg_len = struct.unpack("!I", header)[0]
    if msg_len <= 0 or msg_len > MAX_JSON_BYTES:
        raise ValueError("Ukuran pesan tidak valid atau terlalu besar.")
    payload = recv_exact(sock, msg_len)
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def send_json(sock: socket.socket, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack("!I", len(data)) + data)


def console_print(message: str) -> None:
    with print_lock:
        print(f"\n{message}")
        if not stop_event.is_set():
            print("> ", end="", flush=True)


def receiver_loop(sock: socket.socket) -> None:
    ensure_download_dir()
    try:
        while not stop_event.is_set():
            msg = recv_json(sock)
            if msg is None:
                console_print("[!] Server menutup koneksi.")
                stop_event.set()
                break

            msg_type = msg.get("type")
            if msg_type == "system":
                console_print(f"[SYSTEM] {msg.get('text', '')}")
            elif msg_type == "chat":
                console_print(f"[{msg.get('from', 'Unknown')}] {msg.get('text', '')}")
            elif msg_type == "file_list":
                files = msg.get("files", [])
                if files:
                    console_print("[FILES]\n- " + "\n- ".join(files))
                else:
                    console_print("[FILES] Belum ada file di server.")
            elif msg_type == "download":
                filename = safe_filename(msg.get("filename", "downloaded_file"))
                path = os.path.join(DOWNLOAD_DIR, filename)
                raw = base64.b64decode(msg.get("content_b64", "").encode("utf-8"))
                with open(path, "wb") as f:
                    f.write(raw)
                console_print(f"[DOWNLOAD] File tersimpan di: {path}")
            elif msg_type == "ack":
                console_print(f"[OK] {msg.get('text', '')}")
            elif msg_type == "error":
                console_print(f"[ERROR] {msg.get('text', '')}")
            else:
                console_print(f"[UNKNOWN] {msg}")
    except Exception as e:
        if not stop_event.is_set():
            console_print(f"[!] Error receiver: {e}")
            stop_event.set()


def upload_file(sock: socket.socket, path: str) -> None:
    if not os.path.isfile(path):
        console_print(f"[!] File lokal tidak ditemukan: {path}")
        return

    filename = safe_filename(os.path.basename(path))
    with open(path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("utf-8")
    send_json(sock, {"type": "upload", "filename": filename, "content_b64": content_b64})


def print_help() -> None:
    help_text = """
Perintah:
  /list                  -> lihat file yang tersedia di server
  /upload <path>         -> upload file lokal ke server
  /download <filename>   -> download file dari server
  /help                  -> tampilkan bantuan
  /quit                  -> keluar
  teks biasa             -> kirim pesan broadcast ke semua client
""".strip()
    console_print(help_text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal chat client with file transfer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    name = args.name.strip()
    if not name:
        name = input("Masukkan nama: ").strip() or "Anonymous"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect((args.host, args.port))
        send_json(sock, {"type": "hello", "name": name})

        receiver = threading.Thread(target=receiver_loop, args=(sock,), daemon=True)
        receiver.start()

        print_help()

        try:
            while not stop_event.is_set():
                line = input("> ").strip()
                if not line:
                    continue

                if line == "/help":
                    print_help()
                elif line == "/list":
                    send_json(sock, {"type": "list"})
                elif line.startswith("/upload "):
                    path = line[len("/upload ") :].strip().strip('"')
                    upload_file(sock, path)
                elif line.startswith("/download "):
                    filename = safe_filename(line[len("/download ") :].strip())
                    send_json(sock, {"type": "download", "filename": filename})
                elif line == "/quit":
                    send_json(sock, {"type": "quit"})
                    stop_event.set()
                    break
                else:
                    send_json(sock, {"type": "chat", "text": line})
        except KeyboardInterrupt:
            stop_event.set()
            try:
                send_json(sock, {"type": "quit"})
            except Exception:
                pass
        finally:
            stop_event.set()


if __name__ == "__main__":
    main()
