"""
Debug script: test race condition nel relay server.

Simula:
1. HOST si connette al relay, registra device + sessione
2. CLIENT si connette al relay, cerca l'host per device_id
3. Handshake auth challenge-response
4. Verifica che entrambi i peer rimangano connessi

Se il bug si verifica, l'host riceve "Peer disconnected" (410)
subito dopo l'autenticazione.
"""

import asyncio
import logging
import sys

sys.path.insert(0, "/home/giampaolo/Codium/opendesk-relay/src")
sys.path.insert(0, "/home/giampaolo/Codium/opendesk")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)
logger = logging.getLogger("debug_relay")

from opendesk.crypto.challenge import (  # noqa: E402
    compute_response,
    generate_nonce,
    verify_response,
)
from opendesk.network.protocol import Message, MessageType  # noqa: E402

RELAY_HOST = "127.0.0.1"
RELAY_PORT = 8475

HOST_DEVICE_ID = "host-device-uuid-0001"
HOST_DEVICE_NAME = "DebugHost"
SESSION_ID = "123456789"
PASSWORD = "testpassword123"


async def write_msg(writer, msg):
    data = msg.encode()
    writer.write(data)
    await writer.drain()


async def read_msg(reader, label=""):
    msg = await Message.from_reader(reader)
    logger.debug("  [%s] ← %s", label, msg.type)
    return msg


async def run_host():
    """Connetti come HOST, registra device + sessione, attendi client."""
    logger.info("[HOST] Connecting to relay...")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RELAY_HOST, RELAY_PORT), timeout=10
    )
    logger.info("[HOST] Connected")

    # Registra device + sessione
    await write_msg(writer, Message.relay_register(
        session_id=SESSION_ID,
        device_id=HOST_DEVICE_ID,
        device_name=HOST_DEVICE_NAME,
    ))
    logger.info("[HOST] Sent RELAY_REGISTER (session=%s, device=%s)", SESSION_ID, HOST_DEVICE_ID)

    # Leggi risposta (potrebbe arrivare DEVICE_LIST dopo REGISTER)
    resp = await read_msg(reader, "HOST")
    assert resp.type == MessageType.RELAY_REGISTER, f"Expected REGISTER, got {resp.type}"
    logger.info("[HOST] Registered as host: %s", resp.payload)

    # Dopo la registrazione, il relay potrebbe inviare DEVICE_LIST
    # Leggiamo tutti i messaggi finché non arriva PEER_LIST
    logger.info("[HOST] Waiting for peer...")
    while True:
        msg = await read_msg(reader, "HOST")
        if msg.type == MessageType.RELAY_DEVICE_LIST:
            logger.info("[HOST] Device list: %s", msg.payload)
        elif msg.type == MessageType.RELAY_PEER_LIST:
            logger.info("[HOST] Peer joined: %s", msg.payload)
            break
        else:
            logger.info("[HOST] Unexpected message: %s", msg.type)
            break

    # Invia AUTH_REQUEST
    nonce = generate_nonce()
    await write_msg(writer, Message.auth_request(SESSION_ID, nonce=nonce))
    logger.info("[HOST] Sent AUTH_REQUEST (nonce=%s...)", nonce[:16])

    # Leggi AUTH_RESPONSE
    auth_resp = await read_msg(reader, "HOST")
    assert auth_resp.type == MessageType.AUTH_RESPONSE, (
        f"Expected AUTH_RESPONSE, got {auth_resp.type}"
    )
    client_hash = auth_resp.payload.get("nonce_hash", "")
    logger.info(
        "[HOST] Got AUTH_RESPONSE, hash=%s...",
        client_hash[:16] if client_hash else "EMPTY",
    )

    # Verifica e rispondi
    success = verify_response(nonce, PASSWORD, client_hash) if client_hash else False
    if success:
        await write_msg(writer, Message.auth_ok())
        logger.info("[HOST] Auth OK → sent AUTH_OK")
    else:
        await write_msg(writer, Message.auth_fail("Invalid credentials"))
        logger.info("[HOST] Auth FAIL → sent AUTH_FAIL")

    # Dopo auth, resta in ascolto per 5 secondi totali per vedere se arriva errore
    logger.info("[HOST] Listening for 5s after auth...")
    try:
        async with asyncio.timeout(5.0):
            while True:
                msg = await read_msg(reader, "HOST")
                if msg.type == MessageType.ERROR:
                    logger.error("[HOST] ❌ Received ERROR: %s", msg.payload)
                    logger.error(
                        "[HOST] ❌ BUG! Host received '%s' after auth!",
                        msg.payload.get("message", ""),
                    )
                    writer.close()
                    return False
                elif msg.type == MessageType.RELAY_DEVICE_LIST:
                    logger.info("[HOST] Device list (ignored): %s", msg.payload)
                else:
                    logger.info("[HOST] Received %s (unexpected but not error)", msg.type)
    except TimeoutError:
        logger.info("[HOST] ✅ No error for 5s — host session survived!")

    writer.close()
    return True


async def run_client():
    """Connetti come CLIENT, cerca host per device_id, fai auth."""
    # Aspetta che l'host sia pronto
    await asyncio.sleep(0.5)

    logger.info("[CLIENT] Connecting to relay...")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RELAY_HOST, RELAY_PORT), timeout=10
    )
    logger.info("[CLIENT] Connected")

    # Cerca host per device_id
    await write_msg(writer, Message(
        MessageType.RELAY_REGISTER,
        {"lookup_device": HOST_DEVICE_ID},
    ))
    logger.info("[CLIENT] Sent RELAY_REGISTER (lookup_device=%s)", HOST_DEVICE_ID)

    # Leggi risposta (potrebbe arrivare DEVICE_LIST prima)
    resp = await read_msg(reader, "CLIENT")
    if resp.type == MessageType.ERROR:
        logger.error("[CLIENT] ❌ Lookup failed: %s", resp.payload)
        writer.close()
        return False

    if resp.type == MessageType.RELAY_DEVICE_LIST:
        logger.info("[CLIENT] Device list: %s", resp.payload)
        resp = await read_msg(reader, "CLIENT")

    assert resp.type == MessageType.RELAY_REGISTER, f"Expected REGISTER, got {resp.type}"
    assert resp.payload.get("paired"), f"Expected paired=True, got {resp.payload}"
    logger.info("[CLIENT] Paired with host! session=%s", resp.payload.get("session_id"))

    # Leggi AUTH_REQUEST
    auth_req = await read_msg(reader, "CLIENT")
    assert auth_req.type == MessageType.AUTH_REQUEST, f"Expected AUTH_REQUEST, got {auth_req.type}"
    nonce = auth_req.payload.get("nonce", "")
    logger.info("[CLIENT] Got AUTH_REQUEST, nonce=%s...", nonce[:16] if nonce else "EMPTY")

    # Rispondi
    nonce_hash = compute_response(nonce, PASSWORD) if nonce else ""
    await write_msg(writer, Message.auth_response(nonce_hash, device_id="client-device-uuid-0002"))
    logger.info("[CLIENT] Sent AUTH_RESPONSE")

    # Leggi AUTH_OK o AUTH_FAIL
    auth_result = await read_msg(reader, "CLIENT")
    if auth_result.type == MessageType.AUTH_OK:
        logger.info("[CLIENT] ✅ Authentication successful!")
    elif auth_result.type == MessageType.AUTH_FAIL:
        logger.error("[CLIENT] ❌ Authentication failed: %s", auth_result.payload)
        writer.close()
        return False
    else:
        logger.warning("[CLIENT] Unexpected response: %s", auth_result.type)

    # Dopo auth, resta in ascolto per vedere se arriva "Peer disconnected"
    logger.info("[CLIENT] Listening for 5s after auth...")
    try:
        async with asyncio.timeout(5.0):
            while True:
                msg = await read_msg(reader, "CLIENT")
                if msg.type == MessageType.ERROR:
                    logger.error("[CLIENT] ❌ Received ERROR: %s", msg.payload)
                    writer.close()
                    return False
                elif msg.type == MessageType.RELAY_DEVICE_LIST:
                    logger.info("[CLIENT] Device list (ignored): %s", msg.payload)
                else:
                    logger.info("[CLIENT] Received %s", msg.type)
    except TimeoutError:
        logger.info("[CLIENT] ✅ No error for 5s — client session survived!")

    writer.close()
    return True


async def main():
    logger.info("=" * 60)
    logger.info("Debug relay race condition test")
    logger.info("Relay: %s:%s", RELAY_HOST, RELAY_PORT)
    logger.info("=" * 60)

    # Esegui 10 iterazioni per vedere se il race si manifesta
    for i in range(10):
        logger.info("\n" + "=" * 60)
        logger.info("Iteration %d/10", i + 1)
        logger.info("=" * 60)

        host_task = asyncio.create_task(run_host())
        client_task = asyncio.create_task(run_client())

        host_ok = await host_task
        client_ok = await client_task

        if not host_ok or not client_ok:
            logger.error("❌ BUG CONFIRMED at iteration %d!", i + 1)

        await asyncio.sleep(0.5)

    logger.info("\n" + "=" * 60)
    logger.info("Test complete")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
