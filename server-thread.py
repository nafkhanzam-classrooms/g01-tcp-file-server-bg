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
STORAGE_DIR = "server_storage"
MAX_JSON_BYTES = 50 * 1024 * 1024

clients: set[socket.socket] = set()
client_names: dict[socket.socket, str] = {}
lock = threading.Lock()


def ensure_storage() -> None:
    os.makedirs(STORAGE_DIR, exist_ok=True)


def safe_filename(filename: str) -> str:
    return os.path.basename(filename.strip())


def list_files() -> list[str]:
    ensure_storage()
    return sorted(
        f for f in os.listdir(STORAGE_DIR) if os.path.isfile(os.path.join(STORAGE_DIR, f))
    )


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


def save_file(filename: str, content_b64: str) -> None:
    ensure_storage()
    filename = safe_filename(filename)
    if not filename:
        raise ValueError("Nama file tidak valid.")
    raw = base64.b64decode(content_b64.encode("utf-8"), validate=True)
    with open(os.path.join(STORAGE_DIR, filename), "wb") as f:
        f.write(raw)


def read_file_b64(filename: str) -> str:
    filename = safe_filename(filename)
    path = os.path.join(STORAGE_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(filename)
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def remove_client(sock: socket.socket) -> str:
    with lock:
        name = client_names.pop(sock, "Unknown")
        clients.discard(sock)
    return name


def broadcast(payload: dict) -> None:
    dead = []
    with lock:
        targets = list(clients)
    for client in targets:
        try:
            send_json(client, payload)
        except Exception:
            dead.append(client)
    for client in dead:
        try:
            client.close()
        except Exception:
            pass
        remove_client(client)


def client_worker(conn: socket.socket, addr: tuple[str, int]) -> None:
    name = f"Guest-{addr[1]}"
    with lock:
        clients.add(conn)
        client_names[conn] = name

    try:
        send_json(conn, {"type": "system", "text": "Terhubung ke thread server."})
        while True:
            msg = recv_json(conn)
            if msg is None:
                break

            msg_type = msg.get("type")

            if msg_type == "hello":
                name = msg.get("name", name).strip() or name
                with lock:
                    client_names[conn] = name
                broadcast({"type": "system", "text": f"{name} bergabung."})

            elif msg_type == "chat":
                broadcast({"type": "chat", "from": client_names.get(conn, name), "text": msg.get("text", "")})

            elif msg_type == "list":
                send_json(conn, {"type": "file_list", "files": list_files()})

            elif msg_type == "upload":
                try:
                    filename = safe_filename(msg.get("filename", ""))
                    save_file(filename, msg.get("content_b64", ""))
                    send_json(conn, {"type": "ack", "text": f"File '{filename}' berhasil di-upload."})
                    broadcast({"type": "system", "text": f"{client_names.get(conn, name)} meng-upload '{filename}'."})
                except Exception as e:
                    send_json(conn, {"type": "error", "text": f"Gagal upload: {e}"})

            elif msg_type == "download":
                filename = safe_filename(msg.get("filename", ""))
                try:
                    send_json(conn, {"type": "download", "filename": filename, "content_b64": read_file_b64(filename)})
                except FileNotFoundError:
                    send_json(conn, {"type": "error", "text": f"File '{filename}' tidak ditemukan."})
                except Exception as e:
                    send_json(conn, {"type": "error", "text": f"Gagal download: {e}"})

            elif msg_type == "quit":
                send_json(conn, {"type": "ack", "text": "Sampai jumpa!"})
                break

            else:
                send_json(conn, {"type": "error", "text": f"Perintah tidak dikenal: {msg_type}"})

    except Exception as e:
        print(f"[!] Error client {addr}: {e}")
    finally:
        left_name = remove_client(conn)
        try:
            conn.close()
        except Exception:
            pass
        broadcast({"type": "system", "text": f"{left_name} keluar."})
        print(f"[-] {left_name} ({addr}) terputus")


def run_server(host: str, port: int) -> None:
    ensure_storage()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(100)
        print(f"[THREAD] Server jalan di {host}:{port}")

        while True:
            conn, addr = server.accept()
            print(f"[+] Koneksi baru dari {addr}")
            thread = threading.Thread(target=client_worker, args=(conn, addr), daemon=True)
            thread.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Thread-based multi-client terminal server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    run_server(args.host, args.port)
