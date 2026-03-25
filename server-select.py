#!/usr/bin/env python3
import argparse
import base64
import json
import os
import select
import socket
import struct

HEADER_SIZE = 4
BUFFER_SIZE = 4096
STORAGE_DIR = "server_storage"
MAX_JSON_BYTES = 50 * 1024 * 1024


def ensure_storage() -> None:
    os.makedirs(STORAGE_DIR, exist_ok=True)


def safe_filename(filename: str) -> str:
    return os.path.basename(filename.strip())


def list_files() -> list[str]:
    ensure_storage()
    return sorted(
        f for f in os.listdir(STORAGE_DIR) if os.path.isfile(os.path.join(STORAGE_DIR, f))
    )


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


def close_client(sock: socket.socket, states: dict, clients: set[socket.socket]) -> None:
    name = states.get(sock, {}).get("name", "Unknown")
    print(f"[-] {name} terputus")
    clients.discard(sock)
    states.pop(sock, None)
    try:
        sock.close()
    except Exception:
        pass
    broadcast(clients, {"type": "system", "text": f"{name} keluar."})


def broadcast(clients: set[socket.socket], payload: dict) -> None:
    dead = []
    for client in list(clients):
        try:
            send_json(client, payload)
        except Exception:
            dead.append(client)
    for client in dead:
        try:
            client.close()
        except Exception:
            pass
        clients.discard(client)


def extract_messages(state: dict) -> list[dict]:
    messages = []
    while True:
        if state["expected"] is None:
            if len(state["buffer"]) < HEADER_SIZE:
                break
            state["expected"] = struct.unpack("!I", state["buffer"][:HEADER_SIZE])[0]
            del state["buffer"][:HEADER_SIZE]
            if state["expected"] <= 0 or state["expected"] > MAX_JSON_BYTES:
                raise ValueError("Ukuran pesan tidak valid atau terlalu besar.")

        if len(state["buffer"]) < state["expected"]:
            break

        payload = bytes(state["buffer"][: state["expected"]])
        del state["buffer"][: state["expected"]]
        state["expected"] = None
        messages.append(json.loads(payload.decode("utf-8")))
    return messages


def handle_message(sock: socket.socket, msg: dict, states: dict, clients: set[socket.socket]) -> bool:
    name = states[sock]["name"]
    msg_type = msg.get("type")

    if msg_type == "hello":
        name = msg.get("name", name).strip() or name
        states[sock]["name"] = name
        broadcast(clients, {"type": "system", "text": f"{name} bergabung."})

    elif msg_type == "chat":
        broadcast(clients, {"type": "chat", "from": states[sock]["name"], "text": msg.get("text", "")})

    elif msg_type == "list":
        send_json(sock, {"type": "file_list", "files": list_files()})

    elif msg_type == "upload":
        try:
            filename = safe_filename(msg.get("filename", ""))
            save_file(filename, msg.get("content_b64", ""))
            send_json(sock, {"type": "ack", "text": f"File '{filename}' berhasil di-upload."})
            broadcast(clients, {"type": "system", "text": f"{states[sock]['name']} meng-upload '{filename}'."})
        except Exception as e:
            send_json(sock, {"type": "error", "text": f"Gagal upload: {e}"})

    elif msg_type == "download":
        filename = safe_filename(msg.get("filename", ""))
        try:
            send_json(sock, {"type": "download", "filename": filename, "content_b64": read_file_b64(filename)})
        except FileNotFoundError:
            send_json(sock, {"type": "error", "text": f"File '{filename}' tidak ditemukan."})
        except Exception as e:
            send_json(sock, {"type": "error", "text": f"Gagal download: {e}"})

    elif msg_type == "quit":
        send_json(sock, {"type": "ack", "text": "Sampai jumpa!"})
        return False

    else:
        send_json(sock, {"type": "error", "text": f"Perintah tidak dikenal: {msg_type}"})

    return True


def run_server(host: str, port: int) -> None:
    ensure_storage()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(100)
    server.setblocking(False)

    sockets = [server]
    clients: set[socket.socket] = set()
    states: dict[socket.socket, dict] = {}

    print(f"[SELECT] Server jalan di {host}:{port}")

    try:
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets)

            for sock in readable:
                if sock is server:
                    conn, addr = server.accept()
                    conn.setblocking(False)
                    sockets.append(conn)
                    clients.add(conn)
                    states[conn] = {"buffer": bytearray(), "expected": None, "name": f"Guest-{addr[1]}"}
                    send_json(conn, {"type": "system", "text": "Terhubung ke select server."})
                    print(f"[+] Koneksi baru dari {addr}")
                    continue

                try:
                    data = sock.recv(BUFFER_SIZE)
                    if not data:
                        if sock in sockets:
                            sockets.remove(sock)
                        close_client(sock, states, clients)
                        continue

                    states[sock]["buffer"].extend(data)
                    for msg in extract_messages(states[sock]):
                        keep = handle_message(sock, msg, states, clients)
                        if not keep:
                            if sock in sockets:
                                sockets.remove(sock)
                            close_client(sock, states, clients)
                            break
                except Exception as e:
                    print(f"[!] Error client: {e}")
                    if sock in sockets:
                        sockets.remove(sock)
                    close_client(sock, states, clients)

            for sock in exceptional:
                if sock in sockets:
                    sockets.remove(sock)
                close_client(sock, states, clients)

    finally:
        for sock in sockets:
            try:
                sock.close()
            except Exception:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Select-based multi-client terminal server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    run_server(args.host, args.port)
