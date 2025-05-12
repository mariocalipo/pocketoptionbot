import asyncio
from datetime import datetime, timedelta, timezone

import websockets
import json
import logging
import ssl

import pocketoptionapi.constants as OP_code
import pocketoptionapi.global_value as global_value
from pocketoptionapi.constants import REGION
from pocketoptionapi.ws.objects.timesync import TimeSync
from pocketoptionapi.ws.objects.time_sync import TimeSynchronizer

logger = logging.getLogger(__name__)

timesync = TimeSync()
sync = TimeSynchronizer()

async def on_open():  # pylint: disable=unused-argument
    """Method to process websocket open."""
    logger.info("WebSocket connected successfully")
    global_value.websocket_is_connected = True

async def send_ping(ws):
    while global_value.websocket_is_connected is False:
        await asyncio.sleep(0.1)
    while True:
        await asyncio.sleep(20)
        try:
            await ws.send('42["ps"]')
            logger.debug("Sent ping")
        except Exception as e:
            logger.warning(f"Error sending ping: {e}")
            break

async def process_message(message):
    try:
        data = json.loads(message)
        logger.debug(f"Received message: {data}")
        if isinstance(data, dict) and 'uid' in data:
            logger.info(f"UID: {data['uid']}")
        elif isinstance(data, list) and len(data) > 0:
            event_type = data[0]
            event_data = data[1]
            logger.info(f"Event type: {event_type}, Event data: {event_data}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
    except KeyError as e:
        logger.error(f"Key error: {e}")
    except Exception as e:
        logger.error(f"Error processing message: {e}")

class WebsocketClient(object):
    def __init__(self, api) -> None:
        """
        Inicializa el cliente WebSocket.

        :param api: Instancia de la clase PocketOptionApi
        """
        self.updateHistoryNew = None
        self.updateStream = None
        self.history_data_ready = None
        self.successCloseOrder = False
        self.api = api
        self.message = None
        self.url = None
        self.ssid = global_value.SSID
        self.websocket = None
        self.region = REGION()
        self.loop = asyncio.get_event_loop()
        self.wait_second_message = False
        self._updateClosedDeals = False

    async def websocket_listener(self, ws):
        try:
            async for message in ws:
                await self.on_message(message)
        except Exception as e:
            logger.warning(f"Error occurred: {e}")

    async def connect(self):
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        try:
            await self.api.close()
        except:
            pass

        attempt = 0
        max_attempts = 5
        while not global_value.websocket_is_connected and attempt < max_attempts:
            attempt += 1
            for url in self.region.get_regions(True):
                logger.info(f"Attempting to connect to {url}")
                try:
                    async with websockets.connect(
                        url,
                        ssl=ssl_context,
                        user_agent_header="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ) as ws:
                        self.websocket = ws
                        self.url = url
                        global_value.websocket_is_connected = True

                        on_message_task = asyncio.create_task(self.websocket_listener(ws))
                        sender_task = asyncio.create_task(self.send_message(self.message))
                        ping_task = asyncio.create_task(send_ping(ws))

                        await asyncio.gather(on_message_task, sender_task, ping_task)

                except websockets.ConnectionClosed as e:
                    global_value.websocket_is_connected = False
                    await self.on_close(e)
                    logger.warning(f"Connection closed: {e}. Trying another server.")

                except Exception as e:
                    global_value.websocket_is_connected = False
                    await self.on_error(e)
                    logger.warning(f"Connection failed: {e}. Trying another server.")

                await asyncio.sleep(2)  # Delay between connection attempts

            if not global_value.websocket_is_connected:
                logger.warning(f"Connection attempt {attempt}/{max_attempts} failed. Retrying...")
                await asyncio.sleep(5)  # Delay before retrying all servers

        if not global_value.websocket_is_connected:
            logger.error("Failed to connect after maximum attempts")
            return False
        return True

    async def send_message(self, message):
        while global_value.websocket_is_connected is False:
            await asyncio.sleep(0.1)

        self.message = message

        if global_value.websocket_is_connected and message is not None:
            try:
                await self.websocket.send(message)
                logger.debug(f"Sent message: {message}")
            except Exception as e:
                logger.warning(f"Error sending message: {e}")
        elif message is not None:
            logger.warning("WebSocket not connected")

    @staticmethod
    def dict_queue_add(self, dict, maxdict, key1, key2, key3, value):
        if key3 in dict[key1][key2]:
            dict[key1][key2][key3] = value
        else:
            while True:
                try:
                    dic_size = len(dict[key1][key2])
                except:
                    dic_size = 0
                if dic_size < maxdict:
                    dict[key1][key2][key3] = value
                    break
                else:
                    del dict[key1][key2][sorted(dict[key1][key2].keys(), reverse=False)[0]]

    async def on_message(self, message):  # pylint: disable=unused-argument
        """Method to process websocket messages."""
        logger.debug(f"Raw message: {message}")

        if isinstance(message, bytes):
            message = message.decode('utf-8')

        try:
            if message.startswith('0') and "sid" in message:
                await self.websocket.send("40")
                logger.debug("Sent: 40")

            elif message == "2":
                await self.websocket.send("3")
                logger.debug("Sent: 3 (pong)")

            elif "40" in message and "sid" in message:
                await self.websocket.send(self.ssid)
                logger.debug(f"Sent SSID: {self.ssid}")

            elif message.startswith('451-['):
                json_part = message.split("-", 1)[1]
                message = json.loads(json_part)
                event_type = message[0]
                logger.info(f"Event type: {event_type}")

                if event_type == "successauth":
                    await on_open()
                    logger.info("Authentication successful")

                elif event_type == "successupdateBalance":
                    global_value.balance_updated = True
                    logger.info("Balance updated successfully")

                elif event_type == "successopenOrder":
                    global_value.result = True
                    logger.info("Order opened successfully")

                elif event_type == "updateClosedDeals":
                    self._updateClosedDeals = True
                    self.wait_second_message = True
                    await self.websocket.send('42["changeSymbol",{"asset":"AUDNZD_otc","period":60}]')
                    logger.debug("Sent changeSymbol request")

                elif event_type == "successcloseOrder":
                    self.successCloseOrder = True
                    self.wait_second_message = True
                    logger.info("Order closed successfully")

                elif event_type == "loadHistoryPeriod":
                    self.history_data_ready = True
                    logger.info("History period loaded")

                elif event_type == "updateStream":
                    self.updateStream = True
                    logger.info("Stream updated")

                elif event_type == "updateHistoryNew":
                    self.updateHistoryNew = True
                    logger.info("History updated")

            elif message.startswith("42") and "NotAuthorized" in message:
                logging.error("User not Authorized: Please Change SSID for one valid")
                global_value.ssl_Mutual_exclusion = False
                await self.websocket.close()
                return

            # Process JSON messages
            try:
                data = json.loads(message)
                if isinstance(data, dict):
                    if "balance" in data:
                        if "uid" in data:
                            global_value.balance_id = data["uid"]
                        global_value.balance = data["balance"]
                        global_value.balance_type = data["isDemo"]
                        global_value.balance_updated = True
                        logger.info(f"Balance updated: {data['balance']} (Demo: {data['isDemo']})")

                    elif "requestId" in data and data["requestId"] == 'buy':
                        global_value.order_data = data
                        logger.info(f"Buy order response: {data}")

                    elif self.wait_second_message and isinstance(data, list):
                        self.wait_second_message = False
                        self._updateClosedDeals = False
                        logger.debug("Processed second message")

                    elif isinstance(data, dict) and self.successCloseOrder:
                        self.api.order_async = data
                        self.successCloseOrder = False
                        logger.info("Order async data received")

                    elif self.history_data_ready and isinstance(data, dict):
                        self.history_data_ready = False
                        self.api.history_data = data["data"]
                        logger.info("History data received")

                    elif self.updateStream and isinstance(data, list):
                        self.updateStream = False
                        self.api.time_sync.server_timestamp = data[0][1]
                        logger.info("Stream timestamp updated")

                    elif self.updateHistoryNew and isinstance(data, dict):
                        self.updateHistoryNew = False
                        self.api.historyNew = data
                        logger.info("New history data received")

            except json.JSONDecodeError:
                logger.debug("Message is not JSON")

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def on_error(self, error):  # pylint: disable=unused-argument
        logger.error(error)
        global_value.websocket_error_reason = str(error)
        global_value.check_websocket_if_error = True

    async def on_close(self, error):  # pylint: disable=unused-argument
        global_value.websocket_is_connected = False