import logging
import asyncio
import time
import pandas as pd
from pocketoptionapi import PocketOptionAPI, PocketOption
from settings import (
    TIMEFRAME, MIN_PAYOUT, ASSETS, SORT_BY, SORT_ORDER,
    RSI_BUY_THRESHOLD, RSI_SELL_THRESHOLD, MACD_INDICATOR,
    INDICATOR_TIMEOUT, STOCHASTIC_BUY_THRESHOLD, STOCHASTIC_SELL_THRESHOLD
)
from indicators import calculate_indicators

logger = logging.getLogger(__name__)

def extract_price(candle_data):
    if isinstance(candle_data, pd.DataFrame) and not candle_data.empty:
        try:
            return float(candle_data['close'].iloc[-1])
        except Exception as e:
            logger.warning(f"Error extracting price from DataFrame: {e}")
            return None
    return None

async def get_realtime_prices(client: PocketOption, assets: list) -> dict:
    prices = {}
    if not assets:
        return prices

    semaphore = asyncio.Semaphore(10)

    async def fetch_price(asset):
        async with semaphore:
            try:
                candles = await asyncio.wait_for(
                    client.get_candles(asset, TIMEFRAME, count=1),
                    timeout=3.0
                )
                price = extract_price(candles)
                if isinstance(price, (int, float)):
                    prices[asset] = price
                else:
                    logger.debug(f"No numeric price for {asset}: {candles}")
            except Exception as e:
                logger.warning(f"Failed to fetch price for {asset}: {e}")

    await asyncio.gather(*(fetch_price(asset) for asset in assets))
    return prices

async def list_open_otc_assets(client: PocketOption):
    valid_timeframes = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 43200, 86400, 604800, 2592000]
    payout_tf = TIMEFRAME if TIMEFRAME in valid_timeframes else 60
    if TIMEFRAME not in valid_timeframes:
        logger.warning(f"Invalid TIMEFRAME '{TIMEFRAME}', defaulting to 60s")

    sort_by = SORT_BY if SORT_BY in ('payout', 'price') else 'payout'
    if SORT_BY not in ('payout', 'price'):
        logger.warning(f"Invalid SORT_BY '{SORT_BY}', defaulting to 'payout'")
    sort_order = SORT_ORDER if SORT_ORDER in ('asc', 'desc') else 'desc'
    if SORT_ORDER not in ('asc', 'desc'):
        logger.warning(f"Invalid SORT_ORDER '{SORT_ORDER}', defaulting to 'desc'")

    try:
        payout_data = client.GetPayoutData()
        import json
        payout_data = json.loads(payout_data)
        otc_assets = [asset[1] for asset in payout_data if asset[1].lower().endswith('_otc')]
        logger.debug(f"All available OTC assets: {otc_assets}")
    except Exception as e:
        logger.error(f"Error fetching OTC assets: {e}")
        return []

    if ASSETS and any(a.strip() for a in ASSETS):
        selected = [a.strip() for a in ASSETS if isinstance(a, str) and a.strip()]
        otc_assets = [a for a in selected if a in otc_assets]
        logger.info(f"Assets filter applied from .env: {selected}, remaining: {otc_assets}")
        if not otc_assets:
            logger.info("No specified OTC assets available after filtering")
            return []
    else:
        logger.info("No ASSETS specified in .env, processing all OTC assets")

    candidates = []
    for asset in otc_assets:
        try:
            payout = client.GetPayout(asset)
            if payout is None:
                logger.debug(f"Skipping {asset}: No payout data available")
                continue
            if isinstance(payout, (int, float)) and payout >= MIN_PAYOUT and payout > 0:
                candidates.append((asset, payout))
            else:
                logger.debug(f"Skipping {asset}: Payout {payout} below minimum {MIN_PAYOUT}%")
        except Exception as e:
            logger.warning(f"Payout fetch error for {asset}: {e}")
            continue

    if not candidates:
        logger.info("No open OTC assets meet the minimum payout criteria")
        return []

    assets_list = [a for a, _ in candidates]
    logger.debug(f"Processing {len(assets_list)} assets for indicator calculation: {assets_list}")

    prices = await get_realtime_prices(client, assets_list)
    try:
        indicators = await asyncio.wait_for(calculate_indicators(client, assets_list, timeframe=TIMEFRAME), timeout=INDICATOR_TIMEOUT)
    except Exception as e:
        logger.exception(f"Indicator calculation error for assets: {assets_list}")
        indicators = {a: {} for a in assets_list}

    tradable = []
    discard_counts = {
        'price_invalid': 0,
        'missing_indicators': 0,
        'invalid_stochastic': 0,
        'no_signal': 0
    }

    for asset, payout in candidates:
        vals = indicators.get(asset, {})
        rsi = vals.get("RSI")
        sma = vals.get("SMA")
        stoch_k = vals.get("STOCHASTIC", {}).get("k")
        stoch_d = vals.get("STOCHASTIC", {}).get("d")
        macd = vals.get("MACD", {}).get("macd")
        signal = vals.get("MACD", {}).get("signal")
        bb_upper = vals.get("BB_upper")
        bb_lower = vals.get("BB_lower")
        price = prices.get(asset)

        logger.debug(f"{asset}: price={price}, rsi={rsi}, sma={sma}, stoch_k={stoch_k}, stoch_d={stoch_d}, macd={macd}, signal={signal}, bb_upper={bb_upper}, bb_lower={bb_lower}")

        if price is None or price < 0.0001:
            logger.warning(f"Skipping {asset}: Invalid or too low price ({price})")
            discard_counts['price_invalid'] += 1
            continue
        if rsi is None or sma is None:
            logger.warning(f"Skipping {asset}: Missing indicators (RSI={rsi}, SMA={sma})")
            discard_counts['missing_indicators'] += 1
            continue
        if stoch_k is None or stoch_d is None:
            logger.warning(f"Skipping {asset}: Invalid or missing Stochastic indicators (K={stoch_k}, D={stoch_d})")
            discard_counts['invalid_stochastic'] += 1
            continue

        buy = rsi < RSI_BUY_THRESHOLD and (stoch_k < STOCHASTIC_BUY_THRESHOLD or not stoch_k) and \
              (not MACD_INDICATOR or (macd is not None and signal is not None and macd != 0.0 and signal != 0.0 and macd > signal)) and \
              (bb_lower is None or price > bb_lower)
        sell = rsi > RSI_SELL_THRESHOLD and (stoch_k > STOCHASTIC_SELL_THRESHOLD or not stoch_k) and \
               (not MACD_INDICATOR or (macd is not None and signal is not None and macd != 0.0 and signal != 0.0 and macd < signal)) and \
               (bb_upper is None or price < bb_upper)
        if buy or sell:
            tradable.append((asset, payout))
        else:
            logger.debug(f"Skipping {asset}: Did not meet trading criteria")
            discard_counts['no_signal'] += 1

    logger.info(f"Discard summary: price_invalid={discard_counts['price_invalid']}, missing_indicators={discard_counts['missing_indicators']}, invalid_stochastic={discard_counts['invalid_stochastic']}, no_signal={discard_counts['no_signal']}")

    rev = sort_order == 'desc'
    keyfn = (lambda x: x[1]) if sort_by == 'payout' else (lambda x: prices.get(x[0], 0))
    tradable.sort(key=keyfn, reverse=rev)

    logger.info(f"Tradable assets: {tradable}")
    return tradable