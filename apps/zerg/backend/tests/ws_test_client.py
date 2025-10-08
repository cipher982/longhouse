import asyncio
import json
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional

from websockets.client import WebSocketClientProtocol
from websockets.client import connect


class WebSocketTestClient:
    """
    Test client for WebSocket connections.

    This client provides a simple interface for connecting to a WebSocket server,
    sending messages, and receiving responses for testing purposes.
    """

    def __init__(self, base_url: str, path: str = "/api/ws"):
        self.url = f"{base_url.rstrip('/')}{path}"
        self.ws: Optional[WebSocketClientProtocol] = None
        self.subscribed_topics: List[str] = []

    async def connect(self) -> None:
        """Establish WebSocket connection"""
        self.ws = await connect(self.url)

    async def disconnect(self) -> None:
        """Close WebSocket connection"""
        if self.ws:
            await self.ws.close()
            self.ws = None

    async def send_json(self, data: Dict) -> None:
        """Send JSON data over WebSocket"""
        if not self.ws:
            raise RuntimeError("WebSocket connection not established")
        await self.ws.send(json.dumps(data))

    async def receive_json(self, timeout: float = 5.0) -> Dict:
        """
        Receive JSON data from WebSocket with timeout

        Args:
            timeout: Maximum time to wait for a message in seconds

        Returns:
            Dict containing the received JSON data

        Raises:
            TimeoutError: If no message is received within the timeout period
            RuntimeError: If WebSocket connection is not established
        """
        if not self.ws:
            raise RuntimeError("WebSocket connection not established")
        try:
            data = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            return json.loads(data)
        except asyncio.TimeoutError:
            raise TimeoutError(f"No message received within {timeout} seconds")

    async def subscribe(self, topics: List[str], message_id: str = "test-sub") -> Dict:
        """Subscribe to topics and return the response"""
        await self.send_json({"type": "subscribe", "topics": topics, "message_id": message_id})
        self.subscribed_topics.extend(topics)
        return await self.receive_json()

    async def unsubscribe(self, topics: List[str], message_id: str = "test-unsub") -> Dict:
        """Unsubscribe from topics and return the response"""
        await self.send_json({"type": "unsubscribe", "topics": topics, "message_id": message_id})
        for topic in topics:
            if topic in self.subscribed_topics:
                self.subscribed_topics.remove(topic)
        return await self.receive_json()


async def connect_clients(base_url: str, path: str, num_clients: int) -> List[WebSocketTestClient]:
    """
    Connect multiple WebSocket clients to the server.

    Args:
        base_url: Base URL of the WebSocket server
        path: Path to connect to
        num_clients: Number of clients to connect

    Returns:
        List of connected WebSocketTestClient instances
    """
    clients = []
    for _ in range(num_clients):
        client = WebSocketTestClient(base_url, path)
        await client.connect()
        clients.append(client)
    return clients


async def disconnect_clients(clients: List[WebSocketTestClient]):
    """
    Disconnect multiple WebSocket clients.

    Args:
        clients: List of WebSocketTestClient instances to disconnect
    """
    for client in clients:
        await client.disconnect()


async def with_client(base_url: str, path: str, callback: Callable):
    """
    Context manager-like function for working with a WebSocket client.

    Args:
        base_url: Base URL of the WebSocket server
        path: Path to connect to
        callback: Async function to call with the connected client
    """
    client = WebSocketTestClient(base_url, path)
    try:
        await client.connect()
        await callback(client)
    finally:
        await client.disconnect()
