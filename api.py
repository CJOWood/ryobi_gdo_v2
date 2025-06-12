"""API interface for Ryobi GDO."""

from __future__ import annotations

import asyncio
from collections import abc
from datetime import UTC, datetime
import json
import logging
import time

import aiohttp
from aiohttp.client_exceptions import ServerConnectionError, ServerTimeoutError

from homeassistant.const import STATE_CLOSED, STATE_CLOSING, STATE_OPEN, STATE_OPENING

from .const import (
    DEVICE_GET_ENDPOINT,
    DEVICE_SET_ENDPOINT,
    GARAGE_UPDATE_MSG,
    HOST_URI,
    LOGIN_ENDPOINT,
    WS_AUTH_OK,
    WS_CMD_ACK,
    WS_OK,
)

LOGGER = logging.getLogger(__name__)

METHOD = "method"
PARAMS = "params"
RESULT = "result"

MAX_FAILED_ATTEMPTS = 5
INFO_LOOP_RUNNING = "Event loop already running, not creating new one."

# Websocket errors
ERROR_AUTH_FAILURE = "Authorization failure"
ERROR_TOO_MANY_RETRIES = "Too many retries"
ERROR_UNKNOWN = "Unknown"

# Websocket Signals
SIGNAL_CONNECTION_STATE = "websocket_state"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_STARTING = "starting"
STATE_STOPPED = "stopped"


class RyobiApiClient:
    """Class for interacting with the Ryobi Garage Door Opener API."""

    DOOR_STATE = {
        "0": STATE_CLOSED,
        "1": STATE_OPEN,
        "2": STATE_CLOSING,
        "3": STATE_OPENING,
        "4": "fault",
    }

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        device_id: str | None = None,
    ) -> None:
        """Initialize Ryobi API client."""
        self.username = username
        self.password = password
        self.session = session
        self.device_id = device_id
        self.door_state = None
        self.light_state = None
        self.battery_level = None
        self.api_key = None
        self._data = {}
        self.ws = None
        self.callback: abc.Callable | None = None
        self.socket_state = None
        self.ws_listening = False
        self._modules = {}
        self._ws_task = None

    async def _process_request(
        self, url: str, method: str, data: dict[str, str]
    ) -> dict | None:
        """Process HTTP requests."""
        async with self.session as session:
            http_hethod = getattr(session, method)
            LOGGER.debug("Connecting to %s using %s", url, method)
            reply = None
            try:
                async with http_hethod(url, data=data) as response:
                    rawReply = await response.text()
                    try:
                        reply = json.loads(rawReply)
                        if not isinstance(reply, dict):
                            reply = None
                    except ValueError:
                        LOGGER.warning("Reply was not in JSON format: %s", rawReply)

                    if response.status in [404, 405, 500]:
                        LOGGER.warning("HTTP Error: %s", rawReply)
            except (TimeoutError, ServerTimeoutError):
                LOGGER.error("Timeout connecting to %s", url)
            except ServerConnectionError:
                LOGGER.error("Problem connecting to server at %s", url)
            await session.close()
            return reply

    async def get_api_key(self) -> bool:
        """Get api_key from Ryobi."""
        auth_ok = False
        url = f"https://{HOST_URI}/{LOGIN_ENDPOINT}"
        data = {"username": self.username, "password": self.password}
        method = "post"
        request = await self._process_request(url, method, data)
        if request is None:
            return auth_ok
        try:
            resp_meta = request["result"]["metaData"]
            self.api_key = resp_meta["wskAuthAttempts"][0]["apiKey"]
            auth_ok = True
        except KeyError:
            LOGGER.error("Exception while parsing Ryobi answer to get API key")
        return auth_ok

    async def check_device_id(self) -> bool:
        """Check device_id from Ryobi."""
        device_found = False
        url = f"https://{HOST_URI}/{DEVICE_GET_ENDPOINT}"
        data = {"username": self.username, "password": self.password}
        method = "get"
        request = await self._process_request(url, method, data)
        if request is None:
            return device_found
        try:
            result = request["result"]
        except KeyError:
            return device_found
        if len(result) == 0:
            LOGGER.error("API error: empty result")
        else:
            for data in result:
                if data["varName"] == self.device_id:
                    device_found = True
        return device_found

    async def get_devices(self) -> dict:
        """Return dictionary of devices found."""
        devices = {}
        url = f"https://{HOST_URI}/{DEVICE_GET_ENDPOINT}"
        data = {"username": self.username, "password": self.password}
        method = "get"
        request = await self._process_request(url, method, data)
        if request is None:
            return devices
        try:
            result = request["result"]
        except KeyError:
            return devices
        if len(result) == 0:
            LOGGER.error("API error: empty result")
        else:
            for data in result:
                devices[data["varName"]] = data["metaData"]["name"]
        return devices

    async def update(self) -> bool:
        """Update door status from Ryobi."""
        if self.api_key is None:
            result = await self.get_api_key()
            if not result:
                LOGGER.error("Problem refreshing API key")
                return False

        # Reconnect logic
        if self.ws and not self.ws_listening:
            await self.ws_connect()

        update_ok = False
        url = f"https://{HOST_URI}/{DEVICE_GET_ENDPOINT}/{self.device_id}"
        data = {"username": self.username, "password": self.password}
        method = "get"
        request = await self._process_request(url, method, data)
        if request is None:
            return update_ok
        try:
            dtm = request["result"][0]["deviceTypeMap"]
            # Parse the modules
            result = await self._index_modules(dtm)

            LOGGER.debug("Modules indexed: %s", self._modules)

            # Parse initial values while we setup the websocket for push updates
            if result:
                if "garageDoor" in self._modules:
                    door_state = dtm[self._modules["garageDoor"]]["at"]["doorState"][
                        "value"
                    ]
                    self._data["door_state"] = self.DOOR_STATE[str(door_state)]
                    self._data["saftey"] = dtm[self._modules["garageDoor"]]["at"][
                        "sensorFlag"
                    ]["value"]
                    self._data["vacationMode"] = dtm[self._modules["garageDoor"]]["at"][
                        "vacationMode"
                    ]["value"]
                    if "motionSensor" in dtm[self._modules["garageDoor"]]["at"]:
                        self._data["motion"] = dtm[self._modules["garageDoor"]]["at"][
                            "motionSensor"
                        ]["value"]
                if "garageLight" in self._modules:
                    self._data["light_state"] = dtm[self._modules["garageLight"]]["at"][
                        "lightState"
                    ]["value"]
                if "backupCharger" in self._modules:
                    self._data["battery_level"] = dtm[self._modules["backupCharger"]][
                        "at"
                    ]["chargeLevel"]["value"]
                if "wifiModule" in self._modules:
                    self._data["wifi_rssi"] = dtm[self._modules["wifiModule"]]["at"][
                        "rssi"
                    ]["value"]
                if "parkAssistLaser" in self._modules:
                    self._data["park_assist"] = dtm[self._modules["parkAssistLaser"]][
                        "at"
                    ]["moduleState"]["value"]
                if "inflator" in self._modules:
                    self._data["inflator"] = dtm[self._modules["inflator"]]["at"][
                        "moduleState"
                    ]["value"]
                if "btSpeaker" in self._modules:
                    self._data["bt_speaker"] = dtm[self._modules["btSpeaker"]]["at"][
                        "moduleState"
                    ]["value"]
                    self._data["micStatus"] = dtm[self._modules["btSpeaker"]]["at"][
                        "micEnable"
                    ]["value"]
                if "fan" in self._modules:
                    self._data["fan"] = dtm[self._modules["fan"]]["at"]["speed"][
                        "value"
                    ]

            if "name" in request["result"][0]["metaData"]:
                self._data["device_name"] = request["result"][0]["metaData"]["name"]
            update_ok = True
            LOGGER.debug("Data: %s", self._data)
            if not self.ws:
                # Start websocket listening
                if self.api_key is None:
                    LOGGER.error("API key is None, cannot initialize websocket")
                    return False
                if self.device_id is None:
                    LOGGER.error("Device ID is None, cannot initialize websocket")
                    return False
                self.ws = RyobiWebSocket(
                    self._process_message,
                    self.username,
                    self.api_key,
                    self.session,
                    self.device_id,
                )
        except KeyError as error:
            LOGGER.error("Exception while parsing answer to update device: %s", error)
        return update_ok

    async def _index_modules(self, dtm: dict) -> bool:
        """Index and add modules to dictorary."""
        # Known modules
        module_list = [
            "garageDoor",
            "backupCharger",
            "garageLight",
            "wifiModule",
            "parkAssistLaser",
            "inflator",
            "btSpeaker",
            "fan",
        ]
        frame = {}
        try:
            for key in dtm:
                for module in module_list:
                    if module in key:
                        frame[module] = key
        except Exception as err:  # noqa: BLE001
            LOGGER.error("Problem parsing module list: %s", err)
            return False
        self._modules.update(frame)
        return True

    def get_module(self, module: str) -> int:
        """Return module number for device."""
        return self._modules[module].split("_")[1]

    def get_module_type(self, module: str) -> int:
        """Return module type for device."""
        module_type = {
            "garageDoor": 5,
            "backupCharger": 6,
            "garageLight": 5,
            "wifiModule": 7,
            "parkAssistLaser": 1,
            "inflator": 4,
            "btSpeaker": 2,
            "fan": 3,
        }
        return module_type[module]

    async def ws_connect(self) -> None:
        """Connect to websocket."""
        LOGGER.debug(
            "ws_connect called, ws: %s, ws_listening: %s", self.ws, self.ws_listening
        )
        if self.api_key is None:
            LOGGER.error("Problem refreshing API key")
            raise APIKeyError

        assert self.ws
        if self.ws_listening:
            LOGGER.debug("Websocket already connected")
            return

        LOGGER.debug("Websocket not connected, connecting now")
        await self.open_websocket()

    async def ws_disconnect(self) -> bool:
        """Disconnect from websocket."""
        assert self.ws
        if not self.ws_listening:
            LOGGER.debug("Websocket already disconnected")
        await self.ws.close()
        return True

    async def open_websocket(self) -> None:
        """Connect WebSocket to Ryobi Server."""
        try:
            LOGGER.debug("Attempting to find running loop")
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
            LOGGER.debug("Using new event loop")

        if not self.ws_listening and self.ws is not None:
            self._ws_task = loop.create_task(self.ws.listen())

    async def _process_message(
        self, msg_type: str, msg: dict, error: str | None = None
    ) -> None:
        """Process websocket data and handle websocket signaling."""
        LOGGER.debug(
            "Websocket callback msg_type: %s msg: %s err: %s", msg_type, msg, error
        )
        if msg_type == SIGNAL_CONNECTION_STATE:
            self.ws_listening = False
            if msg == STATE_CONNECTED:
                LOGGER.debug(
                    "Websocket to %s successful", self.ws.url if self.ws else "unknown"
                )
                self.ws_listening = True
            elif msg == STATE_STARTING:
                # Mark websocket as starting to prevent duplicate
                # connection attempts while negotiating
                LOGGER.debug(
                    "Websocket to %s starting", self.ws.url if self.ws else "unknown"
                )
                self.ws_listening = True
            elif msg == STATE_DISCONNECTED:
                LOGGER.debug(
                    "Websocket to %s disconnected",
                    self.ws.url if self.ws else "unknown",
                )
            # Stopped websockets without errors are expected during shutdown
            # and ignored
            elif msg == STATE_STOPPED and error:
                LOGGER.error(
                    "Websocket to %s failed, aborting [Error: %s]",
                    self.ws.url if self.ws else "unknown",
                    error,
                )
            # Flag websocket as not listening
            # STATE_STOPPED with no error
            else:
                LOGGER.debug("Websocket state: %s error: %s", msg, error)
            if self.callback is not None:
                await self.callback()

        elif msg_type == "data":
            message = msg
            LOGGER.debug("Websocket data: %s", message)

            if METHOD in message:
                if message[METHOD] == GARAGE_UPDATE_MSG:
                    LOGGER.debug("Websocket update message")
                    if PARAMS in message:
                        await self.parse_message(message[PARAMS])

                elif message[METHOD] == WS_AUTH_OK:
                    if message[PARAMS]["authorized"]:
                        LOGGER.debug("Websocket API key authorized")
                    else:
                        LOGGER.error("Websocket API key not authorized")

            elif RESULT in message:
                if RESULT in message[RESULT]:
                    if message[WS_CMD_ACK][RESULT] == WS_OK:
                        LOGGER.debug("Websocket result OK")
                if "authorized" in message[WS_CMD_ACK]:
                    if message[WS_CMD_ACK]["authorized"]:
                        LOGGER.debug("Websocket User authorization OK")

            else:
                LOGGER.error("Websocket unknown message received: %s", message)
        else:
            LOGGER.debug("Unknown message from websocket: %s type: %s", msg, msg_type)

    async def parse_message(self, data: dict) -> None:
        """Parse incoming updated data from the websocket."""
        if self.device_id != data.get("varName"):
            LOGGER.debug(
                "Websocket update for %s does not match %s",
                data.get("varName"),
                self.device_id,
            )
            return

        for key, value in data.items():
            if key in {"topic", "varName", "id"}:
                continue

            LOGGER.debug("Websocket parsing update for item %s: %s", key, value)
            parts = key.split(".")
            if len(parts) < 2:
                LOGGER.error("Websocket data update unknown module: %s", key)
                continue
            module_name = parts[1]

            match parts[0]:
                case "garageDoor":
                    match module_name:
                        case "doorState":
                            self._data["door_state"] = self.DOOR_STATE.get(str(value.get("value")))
                        case "motionSensor":
                            self._data["motion"] = value.get("value")
                        case "vacationMode":
                            self._data["vacationMode"] = value.get("value")
                        case "sensorFlag":
                            self._data["safety"] = value.get("value")
                    self._data["door_attributes"] = dict(value.items())
                case "garageLight":
                    if module_name == "lightState":
                        self._data["light_state"] = value.get("value")
                    self._data["light_attributes"] = dict(value.items())
                case "parkAssistLaser":
                    if module_name == "moduleState":
                        self._data["park_assist"] = value.get("value")
                case "btSpeaker":
                    match module_name:
                        case "moduleState":
                            self._data["bt_speaker"] = value.get("value")
                        case "micEnabled":
                            self._data["micStatus"] = value.get("value")
                case "inflator":
                    if module_name == "moduleState":
                        self._data["inflator"] = value.get("value")
                case "fan":
                    if module_name == "speed":
                        self._data["fan"] = value.get("value")
                case _:
                    LOGGER.error("Websocket data update unknown module: %s", key)

        if self.callback is not None:
            await self.callback()


class RyobiWebSocket:
    """Represent a websocket connection to Ryobi servers."""

    def __init__(
        self,
        ws_callback,
        username: str,
        apikey: str,
        session: aiohttp.ClientSession,
        device: str,
    ) -> None:
        """Initialize a RyobiWebSocket instance."""
        self.session = session
        self.url = f"wss://{HOST_URI}/{DEVICE_SET_ENDPOINT}"
        self._user = username
        self._apikey = apikey
        self._device_id = device
        self.callback: abc.Callable = ws_callback
        self._state = None
        self._error_reason = None
        self._ws_client = None
        self.failed_attempts = 0
        self._last_msg = time.time()

    @property
    def state(self) -> str | None:
        """Return the current state."""
        return self._state

    async def set_state(self, value) -> None:
        """Set the state asynchronously."""
        self._state = value
        if self._error_reason:
            LOGGER.debug("Websocket state: %s reason: %s", value, self._error_reason)
        else:
            LOGGER.debug("Websocket state: %s", value)
        await self.callback(SIGNAL_CONNECTION_STATE, value, self._error_reason)
        self._error_reason = None

    async def running(self) -> None:
        """Open a persistent websocket connection and act on events."""
        self._state = STATE_STARTING
        await self.set_state(STATE_STARTING)
        header = {"Connection": "keep-alive, Upgrade", "handshakeTimeout": "10000"}

        error: Exception | None = None
        close_code: int | str | None = None

        try:
            async with self.session.ws_connect(
                self.url,
                heartbeat=15,
                headers=header,
                receive_timeout=5
                * 60,  # Should see something from Ryobi about every 5 minutes
            ) as ws_client:
                self._ws_client = ws_client
                LOGGER.debug("Websocket connection established to %s", self.url)
                self._last_msg = time.time()

                # Auth to server and subscribe to topic
                if self._state != STATE_CONNECTED:
                    await self.websocket_auth()
                    await asyncio.sleep(0.5)
                    await self.websocket_subscribe()

                self._state = STATE_CONNECTED
                await self.set_state(STATE_CONNECTED)
                self.failed_attempts = 0
                async for message in ws_client:
                    if self._state == STATE_STOPPED:
                        break

                    match message.type:
                        case aiohttp.WSMsgType.TEXT:
                            msg = message.json()
                            self._last_msg = time.time()
                            LOGGER.debug(
                                "Websocket message received at %s",
                                datetime.now(tz=UTC).isoformat(),
                            )
                            await self.callback("data", msg)
                        case aiohttp.WSMsgType.CLOSED:
                            LOGGER.warning("Websocket connection closed")
                            break
                        case aiohttp.WSMsgType.ERROR:
                            LOGGER.error("Websocket error")
                            break
                        case aiohttp.WSMsgType.PING:
                            LOGGER.debug("Websocket ping received")
                        case aiohttp.WSMsgType.PONG:
                            LOGGER.debug("Websocket pong received")
                close_code = ws_client.close_code if ws_client else None

        except Exception as err:  # noqa: BLE001
            error = err
            close_code = getattr(self._ws_client, "close_code", None)

        # Unified post-connection and error handling
        is_auth_error = (
            isinstance(error, aiohttp.ClientResponseError)
            and getattr(error, "status", None) == 401
        )
        is_abnormal_close = close_code == 1006 or close_code is None

        if is_auth_error:
            LOGGER.error("Credentials rejected: %s", error)
            self._error_reason = ERROR_AUTH_FAILURE
            self._state = STATE_STOPPED
            await self.set_state(STATE_STOPPED)
            return

        if error is not None and isinstance(error, aiohttp.ClientResponseError):
            LOGGER.error("Unexpected response received: %s", error)
            self._error_reason = ERROR_UNKNOWN

        if error is not None or (
            close_code is not None and self._state != STATE_STOPPED
        ):
            if is_abnormal_close:
                LOGGER.warning(
                    "Websocket closed abnormally (code 1006), will attempt reconnect"
                )
                self._state = STATE_DISCONNECTED
                await self.set_state(STATE_DISCONNECTED)
            else:
                LOGGER.error("Websocket closed with unexpected error: %s", error)
                self._state = STATE_STOPPED
                await self.set_state(STATE_STOPPED)
                return

            self.failed_attempts += 1
            if self.failed_attempts >= MAX_FAILED_ATTEMPTS:
                self._error_reason = ERROR_TOO_MANY_RETRIES
                self._state = STATE_STOPPED
                await self.set_state(STATE_STOPPED)
                return

            retry_delay = (
                0
                if self.failed_attempts == 1
                else min(2 ** (self.failed_attempts - 2) * 30, 300)
            )
            LOGGER.error(
                "Websocket connection failed, retrying in %ds (close_code: %s, error: %s)",
                retry_delay,
                close_code,
                error,
            )
            await asyncio.sleep(retry_delay)
        elif close_code is not None and self._state != STATE_STOPPED:
            LOGGER.debug("Websocket closed with code: %s", close_code)
            await self.set_state(STATE_DISCONNECTED)
            await asyncio.sleep(5)

    async def listen(self):
        """Start the listening websocket."""
        self.failed_attempts = 0
        while self._state != STATE_STOPPED:
            await self.running()

    async def close(self):
        """Close the listening websocket."""
        self._state = STATE_STOPPED
        await self.set_state(STATE_STOPPED)
        if self._ws_client is not None:
            await self._ws_client.close()
        await self.session.close()

    async def websocket_auth(self) -> None:
        """Authenticate with Ryobi server."""
        LOGGER.debug("Websocket attempting authenticate with server")
        auth_request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "srvWebSocketAuth",
            "params": {"varName": self._user, "apiKey": self._apikey},
        }
        await self.websocket_send(auth_request)

    async def websocket_subscribe(self) -> None:
        """Send subscription for device updates."""
        LOGGER.debug("Websocket subscribing to notifications for %s", self._device_id)
        subscribe = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "wskSubscribe",
            "params": {"topic": self._device_id + ".wskAttributeUpdateNtfy"},
        }
        await self.websocket_send(subscribe)

    async def websocket_send(self, message: dict) -> bool:
        """Send websocket message."""
        json_message = json.dumps(message)
        LOGGER.debug("Websocket sending data: %s", self.redact_api_key(message))

        try:
            if self._ws_client is not None:
                await self._ws_client.send_str(json_message)
                LOGGER.debug("Websocket message sent")
                return True

            LOGGER.error("Websocket client is not connected, cannot send message")
            self._error_reason = "Websocket client not connected"
            self._state = STATE_DISCONNECTED
            await self.set_state(STATE_DISCONNECTED)

        except Exception as err:  # noqa: BLE001
            LOGGER.error("Websocket error sending message: %s", err)
            self._error_reason = err
            self._state = STATE_DISCONNECTED
            await self.set_state(STATE_DISCONNECTED)
        return False

    def redact_api_key(self, message: dict) -> str:
        """Clear API key data from logs."""
        redacted = json.loads(json.dumps(message))  # Deep copy
        if "params" in redacted and "apiKey" in redacted["params"]:
            redacted["params"]["apiKey"] = "***"
        return json.dumps(redacted)

    async def send_message(self, *args):
        """Send message to API."""
        if self._state != STATE_CONNECTED:
            LOGGER.warning("Websocket not yet connected, unable to send command")
            return

        LOGGER.debug("Send message args: %s", args)

        ws_command = {
            "jsonrpc": "2.0",
            "method": "gdoModuleCommand",
            "params": {
                "msgType": 16,
                "moduleType": int(args[1]),
                "portId": int(args[0]),
                "moduleMsg": {args[2]: args[3]},
                "topic": self._device_id,
            },
        }
        LOGGER.debug(
            "Sending command: %s value: %s portId: %s moduleType: %s",
            args[2],
            args[3],
            args[0],
            args[1],
        )
        LOGGER.debug("Full message: %s", ws_command)
        await self.websocket_send(ws_command)

    @property
    def last_msg(self) -> float:
        """Return timestamp of last received message."""
        return self._last_msg

    def inactive(self, timeout: int) -> bool:
        """Return True if last message exceeds timeout seconds."""
        return time.time() - self._last_msg > timeout


class APIKeyError(Exception):
    """Exception for missing API key."""
