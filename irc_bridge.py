#!/usr/bin/env python3
"""WebSocket-to-IRC bridge server for the IRC client."""

import asyncio
import websockets
import json
import logging
import sys
import os
import struct
import socket
import re
import uuid
import base64

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

def dcc_decode_ip(n):
    """Convert DCC IP (decimal integer or dotted-quad) to dotted-quad string."""
    s = str(n).strip()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", s):
        return s   # already dotted-quad
    try:
        return socket.inet_ntoa(struct.pack(">I", int(s)))
    except Exception:
        return s

def parse_dcc_ctcp(text):
    """Parse a DCC CTCP payload. Returns dict or None."""
    m = re.match(r"DCC (\w+)\s+(.+)", text, re.DOTALL)
    if not m:
        return None
    kind = m.group(1).upper()
    rest = m.group(2).strip()

    if kind == "SEND":
        # Quoted filename: "name" ip port [size]
        qm = re.match(r'"([^"]+)"\s+(\d+)\s+(\d+)\s*(\d*)', rest)
        if qm:
            filename = qm.group(1)
            ip_dec, port, size = qm.group(2), qm.group(3), qm.group(4) or "0"
        else:
            # Unquoted: rsplit from right to preserve spaces in filename
            parts = rest.rsplit(None, 3)
            if len(parts) == 4:
                filename, ip_dec, port, size = parts
            elif len(parts) == 3:
                filename, ip_dec, port = parts
                size = "0"
            else:
                return None
        return {
            "kind": "SEND",
            "filename": os.path.basename(filename.strip()),
            "ip": dcc_decode_ip(ip_dec),
            "port": int(port),
            "size": int(size) if size else 0,
        }

    elif kind == "CHAT":
        parts = rest.split()
        if len(parts) < 3:
            return None
        return {
            "kind": "CHAT",
            "ip": dcc_decode_ip(parts[1]),
            "port": int(parts[2]),
        }

    return None

def extract_dcc(line):
    """Return parsed DCC info if line is a PRIVMSG CTCP DCC, else None."""
    if "PRIVMSG" not in line or "DCC" not in line.upper():
        return None

    m = re.search(r"\x01(DCC\s.+?)\x01?$", line, re.IGNORECASE)
    if not m:
        return None

    return parse_dcc_ctcp(m.group(1))

class DCCSession:
    def __init__(self, sid, kind, nick, filename=None, ip=None, port=None, size=None):
        self.id = sid
        self.kind = kind
        self.nick = nick
        self.filename = filename
        self.ip = ip
        self.port = port
        self.size = size or 0
        self.received = 0
        self.writer = None
        self.task = None

async def dcc_send_download(session: DCCSession, websocket):
    """Connect to a DCC SEND offer, read data, and stream it to the browser."""
    try:
        reader, writer = await asyncio.open_connection(session.ip, session.port)
        session.writer = writer
    except Exception as e:
        await _ws_send(websocket, {"type": "dcc_error", "session_id": session.id, "data": str(e)})
        return

    try:
        last_pct = -1
        while session.size == 0 or session.received < session.size:
            chunk = await reader.read(32768)
            if not chunk:
                break
            
            session.received += len(chunk)
            
            # Send chunk to browser via base64
            chunk_b64 = base64.b64encode(chunk).decode('ascii')
            await _ws_send(websocket, {
                "type": "dcc_data",
                "session_id": session.id,
                "data": chunk_b64
            })

            # DCC ACK: 4-byte big-endian received count
            writer.write(struct.pack(">I", session.received & 0xFFFFFFFF))
            await writer.drain()
            
            # Throttle progress events to 1% increments
            if session.size > 0:
                pct = int(session.received * 100 / session.size)
                if pct != last_pct:
                    last_pct = pct
                    await _ws_send(websocket, {
                        "type": "dcc_progress",
                        "session_id": session.id,
                        "received": session.received,
                        "total": session.size,
                    })
    except asyncio.CancelledError:
        return
    except Exception as e:
        await _ws_send(websocket, {"type": "dcc_error", "session_id": session.id, "data": str(e)})
        return
    finally:
        try:
            writer.close()
        except Exception:
            pass

    await _ws_send(websocket, {
        "type": "dcc_complete",
        "session_id": session.id,
        "filename": session.filename,
        "size": session.received,
    })
    log.info("CuteISO: pipe complete streamed to client: %s (%d bytes)", session.filename, session.received)

async def dcc_chat_run(session: DCCSession, websocket):
    """Connect to a DCC CHAT (FServ) and relay messages bidirectionally."""
    try:
        reader, writer = await asyncio.open_connection(session.ip, session.port)
        session.writer = writer
    except Exception as e:
        await _ws_send(websocket, {"type": "dcc_error", "session_id": session.id, "data": str(e)})
        return

    await _ws_send(websocket, {
        "type": "dcc_chat_open",
        "session_id": session.id,
        "nick": session.nick,
    })

    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            await _ws_send(websocket, {
                "type": "dcc_chat_msg",
                "session_id": session.id,
                "text": text,
            })
    except asyncio.CancelledError:
        pass
    except Exception as e:
        await _ws_send(websocket, {"type": "dcc_error", "session_id": session.id, "data": str(e)})
    finally:
        try:
            writer.close()
        except Exception:
            pass

    await _ws_send(websocket, {"type": "dcc_chat_closed", "session_id": session.id})

async def _ws_send(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        pass

class IRCConnection:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.nick = None
        self.connected = False
        self.dcc_sessions: dict[str, DCCSession] = {}

    async def connect(self, host, port, nick, username, realname, use_ssl=False):
        if use_ssl:
            import ssl
            ctx = ssl.create_default_context()
            self.reader, self.writer = await asyncio.open_connection(host, int(port), ssl=ctx)
        else:
            self.reader, self.writer = await asyncio.open_connection(host, int(port))
        self.nick = nick
        self.connected = True
        self._send_raw(f"NICK {nick}")
        self._send_raw(f"USER {username} 0 * :{realname}")
        await self.writer.drain()

    def _send_raw(self, line):
        if self.writer:
            self.writer.write(f"{line}\r\n".encode("utf-8"))

    async def send(self, line):
        self._send_raw(line)
        await self.writer.drain()

    async def disconnect(self):
        for s in list(self.dcc_sessions.values()):
            if s.task:
                s.task.cancel()
            if s.writer:
                try:
                    s.writer.close()
                except Exception:
                    pass
        self.dcc_sessions.clear()
        if self.writer:
            try:
                self._send_raw("QUIT :Goodbye")
                await self.writer.drain()
            except Exception:
                pass
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.connected = False

async def handle_websocket(websocket):
    irc = IRCConnection()
    irc_task = None

    async def irc_reader():
        try:
            while True:
                raw = await irc.reader.readline()
                if not raw:
                    await _ws_send(websocket, {"type": "disconnected", "data": "Server closed connection"})
                    break
                line = raw.decode("utf-8", errors="replace").strip()

                if line.startswith("PING"):
                    await irc.send("PONG" + line[4:])
                    continue

                dcc = extract_dcc(line)
                if dcc is not None:
                    nick_from = re.match(r":([^!]+)!", line)
                    nick_from = nick_from.group(1) if nick_from else "?"
                    sid = str(uuid.uuid4())[:8]
                    session = DCCSession(
                        sid, dcc["kind"], nick_from,
                        filename=dcc.get("filename"),
                        ip=dcc.get("ip"),
                        port=dcc.get("port"),
                        size=dcc.get("size", 0),
                    )
                    irc.dcc_sessions[sid] = session
                    await _ws_send(websocket, {
                        "type": "dcc_offer",
                        "session_id": sid,
                        "nick": nick_from,
                        "kind": dcc["kind"],
                        "filename": dcc.get("filename", ""),
                        "size": dcc.get("size", 0),
                        "ip": dcc.get("ip", ""),
                        "port": dcc.get("port", 0),
                    })
                    continue

                await _ws_send(websocket, {"type": "raw", "data": line})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await _ws_send(websocket, {"type": "error", "data": f"IRC read error: {e}"})

    try:
        async for message in websocket:
            try:
                cmd = json.loads(message)
            except json.JSONDecodeError:
                continue

            action = cmd.get("type")

            if action == "connect":
                try:
                    await irc.connect(
                        cmd["host"], cmd.get("port", 6667),
                        cmd["nick"], cmd.get("username", cmd["nick"]),
                        cmd.get("realname", "IRC Client"),
                        cmd.get("ssl", False),
                    )
                    irc_task = asyncio.create_task(irc_reader())
                    await _ws_send(websocket, {"type": "connected"})
                    log.info("CuteISO: piping to %s:%s as %s", cmd["host"], cmd.get("port", 6667), cmd["nick"])
                except Exception as e:
                    await _ws_send(websocket, {"type": "error", "data": f"Connection failed: {e}"})

            elif action == "ping":
                pass  # Just receiving this keeps the Render connection alive

            elif action == "send":
                if irc.connected:
                    await irc.send(cmd["data"])

            elif action == "dcc_accept":
                sid = cmd.get("session_id")
                session = irc.dcc_sessions.get(sid)
                if session:
                    if session.kind == "SEND":
                        session.task = asyncio.create_task(dcc_send_download(session, websocket))
                    elif session.kind == "CHAT":
                        session.task = asyncio.create_task(dcc_chat_run(session, websocket))

            elif action == "dcc_decline":
                sid = cmd.get("session_id")
                session = irc.dcc_sessions.pop(sid, None)
                if session and session.task:
                    session.task.cancel()

            elif action == "dcc_chat_send":
                sid = cmd.get("session_id")
                session = irc.dcc_sessions.get(sid)
                if session and session.writer:
                    text = cmd.get("text", "")
                    session.writer.write(f"{text}\r\n".encode("utf-8"))
                    await session.writer.drain()

            elif action == "disconnect":
                if irc_task:
                    irc_task.cancel()
                await irc.disconnect()
                await _ws_send(websocket, {"type": "disconnected", "data": "Disconnected"})

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.error("WebSocket handler error: %s", e)
    finally:
        if irc_task:
            irc_task.cancel()
        await irc.disconnect()

async def main():
    # Allow cloud platforms to inject the port via environment variables
    port = int(os.environ.get("PORT", 8765))
    host = "0.0.0.0" # Bind to all interfaces for cloud deployment
    log.info(f"CuteISO: piping bridge starting on ws://{host}:{port}")
    
    # Disable strict ping intervals so cloud proxies don't kill idle connections
    async with websockets.serve(handle_websocket, host, port, ping_interval=None, ping_timeout=None):
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
        sys.exit(0)