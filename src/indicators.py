import logging
import asyncio
import time
import pandas as pd
import talib
from pocketoptionapi import PocketOptionAPI, PocketOption
from settings import (
    RSI_INDICATOR, RSI_PERIOD, RSI_MIN, RSI_MAX,
    SMA_INDICATOR, SMA_PERIOD, SMA_MIN, SMA_MAX,
    EMA_INDICATOR, EMA_PERIOD, EMA_MIN, EMA_MAX,
    STOCHASTIC_INDICATOR, STOCHASTIC_K_PERIOD, STOCHASTIC_D_PERIOD,
    MACD_INDICATOR, MACD_FAST_PERIOD, MACD_SLOW_PERIOD, MACD_SIGNAL_PERIOD
)
from cachetools import TTLCache
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

def get_indicator_cache(timeframe: int):
    ttl = timeframe * 5
    return TTLCache(maxsize=100, ttl=ttl)

candle_cache = TTLCache(maxsize=100, ttl=300)

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=15),
    retry=retry_if_exception_type((asyncio.TimeoutError, ConnectionError, asyncio.CancelledError))
)
async def get_candles_with_retry(client: PocketOption, asset: str, timeframe: int, count: int):
    try:
        return await client.get_candles(asset, timeframe, count=count)
    except Exception as e:
        logger.warning(f"Failed to fetch candles for {asset}: {e}. Retrying...")
        raise

async def calculate_indicators(client: PocketOption, assets: list, timeframe: int = 60) -> dict:
    if not assets:
        logger.debug("No assets provided for indicator calculation. Returning empty dictionary.")
        return {}

    indicator_cache = get_indicator_cache(timeframe)
    cache_key = f"{timeframe}_{','.join(sorted(assets))}"
    if cache_key in indicator_cache:
        logger.debug(f"Returning cached indicators for key: {cache_key}")
        return indicator_cache[cache_key]

    results = {asset: {} for asset in assets}
    semaphore = asyncio.Semaphore(5)

    for asset in assets:
        async with semaphore:
            try:
                max_period = max([
                    RSI_PERIOD if RSI_INDICATOR else 0,
                    SMA_PERIOD if SMA_INDICATOR else 0,
                    EMA_PERIOD if EMA_INDICATOR else 0,
                    STOCHASTIC_K_PERIOD if STOCHASTIC_INDICATOR else 0,
                    max(MACD_SLOW_PERIOD, MACD_SIGNAL_PERIOD) if MACD_INDICATOR else 0,
                    50
                ])
                history_size = max(1800, timeframe * (max_period * 3))

                cache_key_candles = f"{asset}_{timeframe}_{history_size}"
                if cache_key_candles in candle_cache:
                    candles = candle_cache[cache_key_candles]
                    logger.debug(f"Using cached candles for {asset}")
                else:
                    try:
                        candles = await get_candles_with_retry(client, asset, timeframe, history_size)
                        candle_cache[cache_key_candles] = candles
                        logger.debug(f"Cached candles for {asset}")
                    except Exception as e:
                        logger.warning(f"Failed to fetch candles for {asset} after retries: {e}. Skipping.")
                        continue

                if candles is None or candles.empty:
                    logger.warning(f"No candle data for {asset}. Skipping.")
                    continue

                prices = candles['close'].astype(float)
                highs = candles['high'].astype(float)
                lows = candles['low'].astype(float)

                logger.debug(f"Candle data for {asset}: {len(candles)} candles, last 50 closes={prices[-50:]}")

                price_range = max(prices[-50:]) - min(prices[-50:]) if len(prices) >= 50 else 0
                if price_range < 1e-10:
                    logger.warning(f"No price variation for {asset} over last 50 candles. Skipping Bollinger Bands.")
                    results[asset]['BOLLINGER'] = None
                    results[asset]['BB_upper'] = None
                    results[asset]['BB_lower'] = None
                else:
                    try:
                        upper, middle, lower = talib.BBANDS(prices, timeperiod=50, nbdevup=2, nbdevdn=2)
                        if len(upper) > 0 and len(lower) > 0 and upper[-1] > lower[-1]:
                            results[asset]['BB_upper'] = upper[-1]
                            results[asset]['BB_lower'] = lower[-1]
                            logger.debug(f"Calculated Bollinger Bands for {asset}: BB_upper={upper[-1]:.5f}, BB_lower={lower[-1]:.5f}")
                        else:
                            results[asset]['BOLLINGER'] = None
                            results[asset]['BB_upper'] = None
                            results[asset]['BB_lower'] = None
                    except Exception as e:
                        logger.error(f"Error calculating Bollinger Bands for {asset}: {e}")
                        results[asset]['BOLLINGER'] = None
                        results[asset]['BB_upper'] = None
                        results[asset]['BB_lower'] = None

                if RSI_INDICATOR:
                    try:
                        rsi = talib.RSI(prices, timeperiod=RSI_PERIOD)
                        value = rsi[-1] if len(rsi) > 0 else None
                        if value is not None and RSI_MIN <= value <= RSI_MAX:
                            results[asset]['RSI'] = value
                            logger.debug(f"Calculated RSI for {asset}: {value:.5f}")
                        else:
                            results[asset]['RSI'] = None
                    except Exception as e:
                        logger.error(f"Error calculating RSI for {asset}: {e}")
                        results[asset]['RSI'] = None

                if SMA_INDICATOR:
                    try:
                        sma = talib.SMA(prices, timeperiod=SMA_PERIOD)
                        value = sma[-1] if len(sma) > 0 else None
                        if value is not None and SMA_MIN <= value <= SMA_MAX:
                            results[asset]['SMA'] = value
                            logger.debug(f"Calculated SMA for {asset}: {value:.5f}")
                        else:
                            results[asset]['SMA'] = None
                    except Exception as e:
                        logger.error(f"Error calculating SMA for {asset}: {e}")
                        results[asset]['SMA'] = None

                if EMA_INDICATOR:
                    try:
                        ema = talib.EMA(prices, timeperiod=EMA_PERIOD)
                        value = ema[-1] if len(ema) > 0 else None
                        if value is not None and EMA_MIN <= value <= EMA_MAX:
                            results[asset]['EMA'] = value
                            logger.debug(f"Calculated EMA for {asset}: {value:.5f}")
                        else:
                            results[asset]['EMA'] = None
                    except Exception as e:
                        logger.error(f"Error calculating EMA for {asset}: {e}")
                        results[asset]['EMA'] = None

                if STOCHASTIC_INDICATOR:
                    try:
                        slowk, slowd = talib.STOCH(highs, lows, prices, fastk_period=STOCHASTIC_K_PERIOD, slowk_period=STOCHASTIC_D_PERIOD, slowd_period=STOCHASTIC_D_PERIOD)
                        value = {'k': slowk[-1] if len(slowk) > 0 else None, 'd': slowd[-1] if len(slowd) > 0 else None}
                        if value['k'] is not None and value['d'] is not None:
                            results[asset]['STOCHASTIC'] = value
                            logger.debug(f"Calculated Stochastic for {asset}: K={value['k']:.5f}, D={value['d']:.5f}")
                        else:
                            results[asset]['STOCHASTIC'] = None
                    except Exception as e:
                        logger.error(f"Error calculating Stochastic for {asset}: {e}")
                        results[asset]['STOCHASTIC'] = None

                if MACD_INDICATOR:
                    try:
                        macd, signal, _ = talib.MACD(prices, fastperiod=MACD_FAST_PERIOD, slowperiod=MACD_SLOW_PERIOD, signalperiod=MACD_SIGNAL_PERIOD)
                        value = {
                            'macd': macd[-1] if len(macd) > 0 else None,
                            'signal': signal[-1] if len(signal) > 0 else None
                        }
                        if value['macd'] is not None and value['signal'] is not None:
                            results[asset]['MACD'] = value
                            logger.debug(f"Calculated MACD for {asset}: MACD={value['macd']:.5f}, Signal={value['signal']:.5f}")
                        else:
                            results[asset]['MACD'] = None
                    except Exception as e:
                        logger.error(f"Error calculating MACD for {asset}: {e}")
                        results[asset]['MACD'] = None

            except Exception as e:
                logger.error(f"Error fetching candles or processing data for {asset}: {e}", exc_info=True)

    indicator_cache[cache_key] = results
    logger.debug(f"Cached indicators for key: {cache_key} with TTL={indicator_cache.ttl} seconds")

    logger.debug("Completed indicator calculation process.")
    return results