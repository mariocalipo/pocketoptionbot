import asyncio
import threading
import sys
from tzlocal import get_localzone
import json
from pocketoptionapi.api import PocketOptionAPI
import pocketoptionapi.constants as OP_code
import time
import logging
import operator
import pocketoptionapi.global_value as global_value
from collections import defaultdict
from collections import deque
import pandas as pd

# Obtener la zona horaria local del sistema como una cadena en el formato IANA
local_zone_name = get_localzone()

def nested_dict(n, type):
    if n == 1:
        return defaultdict(type)
    else:
        return defaultdict(lambda: nested_dict(n - 1, type))

def get_balance():
    return None

class PocketOption:
    __version__ = "1.0.0"

    def __init__(self, ssid, demo):
        self.size = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
                     3600, 7200, 14400, 28800, 43200, 86400, 604800, 2592000]
        global_value.SSID = ssid
        global_value.DEMO = demo
        self.suspend = 0.5
        self.thread = None
        self.subscribe_candle = []
        self.subscribe_candle_all_size = []
        self.subscribe_mood = []
        self.get_digital_spot_profit_after_sale_data = nested_dict(2, int)
        self.get_realtime_strike_list_temp_data = {}
        self.get_realtime_strike_list_temp_expiration = 0
        self.SESSION_HEADER = {
            "User-Agent": r"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          r"Chrome/66.0.3359.139 Safari/537.36"}
        self.SESSION_COOKIE = {}
        self.api = PocketOptionAPI()
        self.loop = asyncio.get_event_loop()

    def get_server_timestamp(self):
        return self.api.time_sync.server_timestamp

    def Stop(self):
        sys.exit()

    def get_server_datetime(self):
        return self.api.time_sync.server_datetime

    def set_session(self, header, cookie):
        self.SESSION_HEADER = header
        self.SESSION_COOKIE = cookie

    def get_async_order(self, buy_order_id):
        if self.api.order_async["deals"][0]["id"] == buy_order_id:
            return self.api.order_async["deals"][0]
        else:
            return None

    def get_async_order_id(self, buy_order_id):
        return self.api.order_async["deals"][0][buy_order_id]

    def start_async(self):
        asyncio.run(self.api.connect())

    def disconnect(self):
        try:
            if global_value.websocket_is_connected:
                asyncio.run(self.api.close())
                logging.info("WebSocket connection closed successfully.")
            else:
                logging.info("WebSocket was not connected.")
            if self.loop is not None:
                for task in asyncio.all_tasks(self.loop):
                    task.cancel()
                if not self.loop.is_closed():
                    self.loop.stop()
                    self.loop.close()
                    logging.info("Event loop stopped and closed successfully.")
            if self.api.websocket_thread is not None and self.api.websocket_thread.is_alive():
                self.api.websocket_thread.join()
                logging.info("WebSocket thread joined successfully.")
        except Exception as e:
            logging.error(f"Error during disconnect: {e}")

    def connect(self):
        try:
            websocket_thread = threading.Thread(target=self.api.connect, daemon=True)
            websocket_thread.start()
            time.sleep(1)  # Give the thread time to establish connection
            if global_value.websocket_is_connected:
                return True
            else:
                logging.warning("WebSocket connection not established")
                return False
        except Exception as e:
            logging.error(f"Error al conectar: {e}")
            return False

    def GetPayout(self, pair):
        try:
            data = self.api.GetPayoutData()
            data = json.loads(data)
            data2 = None
            for i in data:
                if i[1] == pair:
                    data2 = i
            return data2[5] if data2 else None
        except Exception as e:
            logging.error(f"Error fetching payout for {pair}: {e}")
            return None

    def check_connect(self):
        return bool(global_value.websocket_is_connected)

    def get_balance(self):
        if global_value.balance_updated:
            return global_value.balance
        else:
            logging.warning("Balance not updated")
            return None

    @staticmethod
    def check_open():
        return global_value.order_open

    @staticmethod
    def check_order_closed(ido):
        while ido not in global_value.order_closed:
            time.sleep(0.1)
        for pack in global_value.stat:
            if pack[0] == ido:
                logging.info(f"Order Closed: {pack[1]}")
        return pack[0]

    def buy(self, amount, active, action, expirations):
        self.api.buy_multi_option = {}
        self.api.buy_successful = None
        req_id = "buy"
        try:
            if req_id not in self.api.buy_multi_option:
                self.api.buy_multi_option[req_id] = {"id": None}
            else:
                self.api.buy_multi_option[req_id]["id"] = None
        except Exception as e:
            logging.error(f"Error initializing buy_multi_option: {e}")
            return False, None
        global_value.order_data = None
        global_value.result = None
        self.api.buyv3(amount, active, action, expirations, req_id)
        start_t = time.time()
        while True:
            if global_value.result is not None and global_value.order_data is not None:
                break
            if time.time() - start_t >= 5:
                if isinstance(global_value.order_data, dict) and "error" in global_value.order_data:
                    logging.error(global_value.order_data["error"])
                else:
                    logging.error("Unknown error occurred during buy operation")
                return False, None
            time.sleep(0.1)
        return global_value.result, global_value.order_data.get("id", None)

    def check_win(self, id_number):
        start_t = time.time()
        order_info = None
        while True:
            try:
                order_info = self.get_async_order(id_number)
                if order_info and "id" in order_info and order_info["id"] is not None:
                    break
            except:
                pass
            if time.time() - start_t >= 120:
                logging.error("Timeout: Could not retrieve order info in time.")
                return None, "unknown"
            time.sleep(0.1)
        if order_info and "profit" in order_info:
            status = "win" if order_info["profit"] > 0 else "lose"
            return order_info["profit"], status
        else:
            logging.error("Invalid order info retrieved.")
            return None, "unknown"

    @staticmethod
    def last_time(timestamp, period):
        timestamp_redondeado = (timestamp // period) * period
        return int(timestamp_redondeado)

    def get_candles(self, active, period, start_time=None, count=6000, count_request=1):
        try:
            if start_time is None:
                time_sync = self.get_server_timestamp()
                time_red = self.last_time(time_sync, period)
            else:
                time_red = start_time
                time_sync = self.get_server_timestamp()
            all_candles = []
            for _ in range(count_request):
                self.api.history_data = None
                while True:
                    try:
                        self.api.getcandles(active, period, count, time_red)
                        for i in range(1, 100):
                            if self.api.history_data is None:
                                time.sleep(0.1)
                            if i == 99:
                                break
                        if self.api.history_data is not None:
                            all_candles.extend(self.api.history_data)
                            break
                    except Exception as e:
                        logging.error(f"Error fetching candles: {e}")
                all_candles = sorted(all_candles, key=lambda x: x["time"])
                if all_candles:
                    time_red = all_candles[0]["time"]
            df_candles = pd.DataFrame(all_candles)
            df_candles = df_candles.sort_values(by='time').reset_index(drop=True)
            df_candles['time'] = pd.to_datetime(df_candles['time'], unit='s')
            df_candles.set_index('time', inplace=True)
            df_candles.index = df_candles.index.floor('1s')
            df_resampled = df_candles['price'].resample(f'{period}s').ohlc()
            df_resampled.reset_index(inplace=True)
            return df_resampled
        except Exception as e:
            logging.error(f"Error in get_candles: {e}")
            return None

    @staticmethod
    def process_data_history(data, period):
        df = pd.DataFrame(data['history'], columns=['timestamp', 'price'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='s', utc=True)
        df['minute_rounded'] = df['datetime'].dt.floor(f'{period / 60}min')
        ohlcv = df.groupby('minute_rounded').agg(
            open=('price', 'first'),
            high=('price', 'max'),
            low=('price', 'min'),
            close=('price', 'last')
        ).reset_index()
        ohlcv['time'] = ohlcv['minute_rounded'].apply(lambda x: int(x.timestamp()))
        ohlcv = ohlcv.drop(columns='minute_rounded')
        ohlcv = ohlcv.iloc[:-1]
        ohlcv_dict = ohlcv.to_dict(orient='records')
        return ohlcv_dict

    @staticmethod
    def process_candle(candle_data, period):
        data_df = pd.DataFrame(candle_data)
        data_df.sort_values(by='time', ascending=True, inplace=True)
        data_df.drop_duplicates(subset='time', keep="first", inplace=True)
        data_df.reset_index(drop=True, inplace=True)
        data_df.ffill(inplace=True)
        diferencias = data_df['time'].diff()
        diff = (diferencias[1:] == period).all()
        return data_df, diff

    def change_symbol(self, active, period):
        return self.api.change_symbol(active, period)

    def sync_datetime(self):
        return self.api.synced_datetime