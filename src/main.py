#!/usr/bin/env python3
import logging
import sys
import asyncio
from pathlib import Path
from logging.handlers import RotatingFileHandler
import time
from datetime import datetime, timezone
from pocketoptionapi import PocketOptionAPI, PocketOption
import pocketoptionapi.global_value as global_value

root = Path(__file__).parent
sys.path.insert(0, str(root))

from settings import EMAIL, PASSWORD, SSID, DEMO_MODE, TRADE_COOLDOWN, TIMEFRAME, STRATEGY
from assets import list_open_otc_assets, extract_price
from trade import execute_trades
from indicators import calculate_indicators

def setup_logging():
    if not logging.getLogger().hasHandlers():
        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG)
        log_format = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(log_format)
        console_handler.setLevel(logging.INFO)
        logger.addHandler(console_handler)
        log_file = root.parent / "pocketoptionbot.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
        file_handler.setFormatter(log_format)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)

logger = logging.getLogger(__name__)

async def check_connection(client: PocketOption) -> bool:
    logger.debug("Checking connection status...")
    try:
        balance = client.get_balance()
        if balance is None:
            logger.warning("Balance not updated yet")
            return False
        logger.debug(f"Connection check successful. Current balance: {balance:.2f} USD")
        return True
    except Exception as e:
        logger.warning(f"Connection check failed: {e}", exc_info=False)
        return False

async def reconnect(client: PocketOption, max_attempts: int = 5) -> bool:
    logger.info("Attempting to reconnect...")
    for attempt in range(1, max_attempts + 1):
        logger.info(f"Reconnection attempt {attempt}/{max_attempts}...")
        try:
            success = client.connect()
            if success:
                logger.info("Reconnection successful!")
                balance = client.get_balance()
                account_type = "Demo" if global_value.DEMO else "Real"
                if balance is not None:
                    logger.info(f"Account balance ({account_type}) after reconnection: {balance:.2f} USD")
                else:
                    logger.warning("Balance not updated after reconnection")
                return True
            logger.warning(f"Reconnection attempt {attempt} failed")
        except Exception as e:
            logger.error(f"Error during reconnection attempt {attempt}: {e}", exc_info=False)
        delay = min(5 * (2 ** attempt), 60)
        await asyncio.sleep(delay)
    logger.critical("Failed to reconnect after maximum attempts.")
    return False

async def main():
    setup_logging()
    logger.info("Starting PocketOption trading bot...")

    if not SSID:
        logger.critical("SSID not provided in .env file. Exiting.")
        return

    logger.info("Initializing PocketOption client...")
    client = PocketOption(SSID, DEMO_MODE)

    max_init_attempts = 5
    init_attempt = 1
    init_delay = 5
    connected = False

    while init_attempt <= max_init_attempts and not connected:
        logger.info(f"Attempting to connect to PocketOption (Attempt {init_attempt}/{max_init_attempts})...")
        try:
            success = client.connect()
            if success:
                logger.info("Connection established successfully!")
                connected = True
            else:
                logger.error(f"Connection attempt {init_attempt} failed")
        except Exception as e:
            logger.error(f"Unexpected error during connection attempt {init_attempt}: {e}", exc_info=False)

        if not connected:
            if init_attempt == max_init_attempts:
                logger.critical("Failed to establish initial connection after maximum attempts. Exiting.")
                return
            init_attempt += 1
            await asyncio.sleep(init_delay)

    if not connected:
        logger.critical("Could not establish initial connection. Exiting.")
        return

    try:
        balance = client.get_balance()
        account_type = "Demo" if global_value.DEMO else "Real"
        if balance is not None:
            logger.info(f"Account balance ({account_type}): {balance:.2f} USD")
        else:
            logger.warning("Failed to retrieve initial account balance: Balance not updated")
        logger.info("-" * 50)
    except Exception as e:
        logger.warning(f"Failed to retrieve initial account balance: {e}", exc_info=False)

    logger.info("Entering main trading cycle loop.")
    while True:
        start_time_cycle = time.time()
        utc_hour = datetime.now(timezone.utc).hour
        if 22 <= utc_hour <= 2:
            logger.info("Low liquidity period (22h-2h UTC). Pausing for 1 hour.")
            await asyncio.sleep(3600)
            continue

        logger.info("-" * 50)
        logger.info("Starting new trading cycle...")
        logger.info(f"Active trading strategy: {STRATEGY.upper()}")

        try:
            if not await check_connection(client):
                logger.warning("Connection lost. Attempting to reconnect...")
                if not await reconnect(client):
                    logger.critical("Failed to reconnect. Exiting.")
                    return

            logger.info("Listing and filtering open OTC assets...")
            open_assets_details = await list_open_otc_assets(client)
            logger.debug(f"Open assets details: {open_assets_details}")
            assets_for_trade = []
            payout_map = {}
            skipped_items_count = 0

            if not open_assets_details:
                logger.info("No open OTC assets meet payout or trading criteria.")
            else:
                for item in open_assets_details:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        try:
                            elem1, elem2 = item
                            if isinstance(elem1, str) and isinstance(elem2, (int, float)):
                                asset, payout = elem1, elem2
                            elif isinstance(elem2, str) and isinstance(elem1, (int, float)):
                                asset, payout = elem2, elem1
                            else:
                                skipped_items_count += 1
                                logger.warning(f"Invalid asset or payout: item={item}")
                                continue
                            assets_for_trade.append(asset)
                            payout_map[asset] = payout
                        except Exception as e:
                            skipped_items_count += 1
                            logger.warning(f"Error processing item {item}: {e}")
                    else:
                        skipped_items_count += 1
                        logger.warning(f"Invalid item format: {item} (type={type(item)})")

                if skipped_items_count > 0:
                    logger.warning(f"Filtered out {skipped_items_count} items due to invalid format.")

            logger.debug(f"Assets for trade after filtering: {assets_for_trade}")

            assets_for_trade = [asset for asset in assets_for_trade if isinstance(asset, str) and asset]
            if not assets_for_trade:
                logger.warning("No valid assets in assets_for_trade after validation")
            elif not all(isinstance(asset, str) for asset in assets_for_trade):
                logger.error(f"Invalid assets in assets_for_trade: {assets_for_trade}")
                assets_for_trade = []

            initial_indicators_log = {}
            initial_prices_log = {}
            if assets_for_trade:
                initial_indicators_log = await calculate_indicators(client, assets_for_trade, timeframe=TIMEFRAME)
                try:
                    price_tasks = [client.get_candles(asset, TIMEFRAME, count=1) for asset in assets_for_trade]
                    price_data_list = await asyncio.gather(*price_tasks, return_exceptions=True)
                    for i, asset in enumerate(assets_for_trade):
                        price_data = price_data_list[i]
                        price = extract_price(price_data)
                        initial_prices_log[asset] = price if price is not None else "N/A"
                except Exception as e:
                    logger.warning(f"Unexpected error during initial price fetching: {e}", exc_info=False)

                logger.info(f"--- Final list of assets for trade execution ({len(assets_for_trade)}) ---")
                for asset in assets_for_trade:
                    payout = payout_map.get(asset, "N/A")
                    price = initial_prices_log.get(asset, "N/A")
                    indicator_values_log = initial_indicators_log.get(asset, {})
                    indicator_str = ", ".join(f"{ind}: {val:.5f}" if isinstance(val, (int, float)) and val is not None else f"{ind}: N/A" for ind, val in indicator_values_log.items())
                    logger.info(f"    - {asset}: Payout {payout}%, Price: {price}, Indicators: [{indicator_str}]")
                logger.info("---------------------------------------------------")
            else:
                logger.info("No assets passed filtering and trading criteria.")

            if assets_for_trade:
                logger.info("Executing trading logic...")
                logger.debug(f"Passing assets to execute_trades: {assets_for_trade}")
                await execute_trades(client, assets_for_trade, initial_indicators_log)
            else:
                logger.info("Skipping trade execution: No tradable assets.")

            if not await check_connection(client):
                logger.warning("Connection lost. Attempting to reconnect...")
                if not await reconnect(client):
                    logger.critical("Failed to reconnect. Exiting.")
                    return

        except Exception as e:
            logger.error(f"Unexpected error in trading cycle: {e}", exc_info=True)

        end_time_cycle = time.time()
        cycle_duration = end_time_cycle - start_time_cycle
        wait_time_needed = max(0, TRADE_COOLDOWN - cycle_duration)

        if wait_time_needed > 0:
            logger.info(f"Trading cycle completed in {cycle_duration:.2f} seconds.")
            logger.info(f"Waiting {wait_time_needed:.2f} seconds before next cycle (TRADE_COOLDOWN={TRADE_COOLDOWN}s).")
            sleep_interval = 10
            start_wait_time = time.time()
            while True:
                time_passed_in_wait = time.time() - start_wait_time
                remaining_wait = max(0, wait_time_needed - time_passed_in_wait)
                if remaining_wait <= 1.0:
                    break
                time_until_next_log_tick = sleep_interval - (time_passed_in_wait % sleep_interval)
                if time_until_next_log_tick <= 0.01:
                    time_until_next_log_tick = sleep_interval
                sleep_duration = min(time_until_next_log_tick, remaining_wait)
                if sleep_duration > 0:
                    await asyncio.sleep(sleep_duration)
                remaining_wait = max(0, wait_time_needed - (time.time() - start_wait_time))
                if remaining_wait > 0:
                    logger.info(f"Time until next cycle: {remaining_wait:.2f} seconds remaining.")
            logger.debug("Waiting period completed.")
        else:
            logger.info(f"Trading cycle completed in {cycle_duration:.2f} seconds. No waiting needed.")

if __name__ == "__main__":
    setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped manually by user (KeyboardInterrupt).")
    except Exception as e:
        logger.critical(f"Fatal error during bot execution: {e}", exc_info=True)