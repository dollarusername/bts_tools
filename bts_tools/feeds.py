#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# bts_tools - Tools to easily manage the bitshares client
# Copyright (c) 2014 Nicolas Wack <wackou@gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from . import core
from .core import hashabledict
from .feed_providers import FeedPrice, FeedSet, YahooFeedProvider, BterFeedProvider, Btc38FeedProvider,\
    PoloniexFeedProvider, GoogleFeedProvider, BloombergFeedProvider, BitcoinAverageFeedProvider,\
    BitfinexFeedProvider, BitstampFeedProvider, YunbiFeedProvider,\
    CoinCapFeedProvider, CoinMarketCapFeedProvider, BittrexFeedProvider,\
    CurrencyLayerFeedProvider,\
    ALL_FEED_PROVIDERS
from collections import deque, defaultdict
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import threading
import itertools
import statistics
import json
import arrow
import pendulum
import re
import logging

log = logging.getLogger(__name__)

"""BitAssets for which we check and publish feeds."""
BASE_ASSETS = {'BTC', 'USD', 'CNY', 'GOLD', 'EUR', 'GBP', 'CAD', 'CHF', 'HKD', 'MXN', 'RUB', 'SEK', 'SGD',
               'AUD', 'SILVER', 'TRY', 'KRW', 'JPY', 'NZD', 'ARS'}

YAHOO_ASSETS = BASE_ASSETS - {'BTC', 'USD', 'CNY'}

OTHER_ASSETS = {'TUSD', 'CASH.USD', 'TCNY', 'CASH.BTC', 'ALTCAP', 'GRIDCOIN', 'STEEM',
                'BTWTY', 'RUBLE', 'HERO'}

# BIT_ASSETS_INDICES = {'SHENZHEN': 'CNY',
#                       'SHANGHAI': 'CNY',
#                       'NASDAQC': 'USD',
#                       'NIKKEI': 'JPY',
#                       'HANGSENG': 'HKD'}
# deactivate those indices for now
BIT_ASSETS_INDICES = {}

BIT_ASSETS = BASE_ASSETS | OTHER_ASSETS | BIT_ASSETS_INDICES.keys()

"""List of feeds that should be shown on the UI and in the logs. Note that we
always check and publish all feeds, regardless of this variable."""
DEFAULT_VISIBLE_FEEDS = ['USD', 'BTC', 'CNY', 'SILVER', 'EUR']

cfg = None


history_len = None
price_history = None
feeds = {}

feed_control = None
#nfeed_checked = 0
#visible_feeds = DEFAULT_VISIBLE_FEEDS


def load_feeds():
    global cfg, history_len, price_history, visible_feeds, feed_control
    cfg = core.config['monitoring']['feeds']
    history_len = int(cfg['median_time_span'] / cfg['check_time_interval'])
    price_history = {cur: deque(maxlen=history_len) for cur in BIT_ASSETS}
    visible_feeds = cfg.get('visible_feeds', DEFAULT_VISIBLE_FEEDS)
    feed_control = BitSharesFeedControl(cfg=cfg, visible_feeds=visible_feeds)


def get_multi_feeds(func, args, providers, stddev_tolerance=None):
    result = FeedSet()
    provider_list = []

    def get_price(pargs):
        args, provider = pargs
        return provider, args, getattr(provider, func)(*args)

    with ThreadPoolExecutor(max_workers=4) as e:
        for f in [e.submit(get_price, pargs)
                  for pargs in itertools.product(args, providers)]:
            with suppress(Exception):
                provider, args, price = f.result()
                result.append(price)
                provider_list.append(provider)

    return result


def is_valid_bit20_publication(trx):
    """
    check that the transaction is a valid one, ie:
      - it contains a single operation
      - it is a transfer from 'bittwenty' (1.2.111226) to 'bittwenty.feed' (1.2.126782)

    note: this does not check the contents of the transaction, it only
          authenticates it
    """
    try:
        # we only want a single operation
        if len(trx['op']['op']) != 2:  # (trx_id, content)
            return False

        # authenticates sender and receiver
        trx_metadata = trx['op']['op'][1]
        if trx_metadata['from'] != '1.2.111226':  # 'bittwenty'
            log.debug('invalid sender for bit20 publication: {}'.format(json.dumps(trx, indent=4)))
            return False
        if trx_metadata['to'] != '1.2.126782':  # 'bittwenty.feed'
            log.debug('invalid receiver for bit20 publication: {}'.format(json.dumps(trx, indent=4)))
            return False

        return True

    except KeyError:
        # trying to access a non-existent field -> probably looking at something we don't want
        log.warning('invalid transaction for bit20 publication: {}'.format(json.dumps(trx, indent=4)))
        return False


def get_bit20_feed(node, usd_price):
    # read composition of the index

    # need to import the following key to be able to decrypt memos
    #   import_key "announce" 5KJJNfiSyzsbHoVb81WkHHjaX2vZVQ1Fqq5wE5ro8HWXe6qNFyQ
    if node.type() != 'bts':
        return
    if not node.is_online():
        log.warning('Wallet is offline, will not be able to read bit20 composition')
        return
    if not node.is_synced():
        log.warning('Client is not synced yet, will not try to read bit20 composition')
        return
    if node.is_locked():
        log.warning('Wallet is locked, will not be able to read bit20 composition')
        return

    bit20 = None  # contains the composition of the feed

    bit20feed = node.get_account_history('bittwenty.feed', 15)

    if not bit20feed:
        # import the 'announce' key to be able to read the memo publications
        log.info('Importing the "announce" key in the wallet')
        node.import_key('announce', '5KJJNfiSyzsbHoVb81WkHHjaX2vZVQ1Fqq5wE5ro8HWXe6qNFyQ')
        # try again
        bit20feed = node.get_account_history('bittwenty.feed', 15)

    # find bit20 composition
    for f in bit20feed:
        if not is_valid_bit20_publication(f):
            log.warning('Hijacking attempt of the bit20 feed? trx: {}'.format(json.dumps(f, indent=4)))
            continue

        if f['memo'].startswith('COMPOSITION'):
            last_updated = re.search('\((.*)\)', f['memo'])
            if last_updated:
                last_updated = arrow.get(last_updated.group(1), 'YYYY/MM/DD')

            bit20 = json.loads(f['memo'].split(')', maxsplit=1)[1])
            log.debug('Found bit20 composition, last update = {}'.format(last_updated))
            break

    else:
        log.warning('Did not find any bit20 composition in the last {} messages '
                    'to account bittwenty.feed'.format(len(bit20feed)))
        log.warning('Make sure in the following order that:')
        log.warning('  - your wallet is unlocked')
        log.warning('  - your client is synced')
        log.warning('  - if you use the "track-accounts" option, make sure to include 1.2.111226 and 1.2.126782 accounts')
        log.warning('  - the "account_history" plugin is active and that your client is compiled to support it')
        log.warning('  - you have imported the private key needed for reading bittwenty.feed memos: '
                    'import_key "announce" 5KJJNfiSyzsbHoVb81WkHHjaX2vZVQ1Fqq5wE5ro8HWXe6qNFyQ')
        return

    # look for custom market parameters
    for f in bit20feed:
        if not is_valid_bit20_publication(f):
            log.warning('Hijacking attempt of the bit20 feed? trx: {}'.format(json.dumps(f, indent=4)))
            continue

        if f['memo'].startswith('MARKET'):
            # only take the most recent into account
            market_params = json.loads(f['memo'][len('MARKET :: '):])
            log.debug('Got market params for bit20: {}'.format(market_params))
            # FIXME: this affects the global config object
            cfg['asset_params']['BTWTY'] = {'maintenance_collateral_ratio': market_params['MCR'],
                                            'maximum_short_squeeze_ratio': market_params['MSSR'],
                                            'core_exchange_factor': cfg['asset_params']['default']['core_exchange_factor']}
            break
    else:
        log.debug('Did not find any custom market parameters in the last {} messages '
                  'to account bittwenty.feed'.format(len(bit20feed)))
        log.debug('Make sure that your wallet is unlocked and you have imported '
                  'the private key needed for reading bittwenty.feed memos: ')
        log.debug('import_key "announce" 5KJJNfiSyzsbHoVb81WkHHjaX2vZVQ1Fqq5wE5ro8HWXe6qNFyQ')

    if len(bit20['data']) < 3:
        log.warning('Not enough assets in bit20 data: {}'.format(bit20['data']))
        return

    bit20_value_cmc = 0
    cmc_missing_assets = []
    bit20_value_cc = 0
    coincap_missing_assets = []

    try:
        cmc_assets = CoinMarketCapFeedProvider().get_all()
        for bit20asset, qty in bit20['data']:
            try:
                price = cmc_assets.price(bit20asset, 'USD')
                log.debug('CoinMarketcap {} {} at ${} = ${}'.format(qty, bit20asset, price, qty * price))
                bit20_value_cmc += qty * price
            except ValueError as e:
                log.debug('Unknown asset on CMC: {}'.format(bit20asset))
                cmc_missing_assets.append(bit20asset)

    except Exception as e:
        log.warning('Could not get bit20 assets feed from CoinMarketCap: {}'.format(e))

    try:
        coincap_assets = CoinCapFeedProvider().get_all()
        for bit20asset, qty in bit20['data']:
            try:
                price = coincap_assets.price(bit20asset, 'USD')
                #log.debug('CoinCap {} {} at ${} = ${}'.format(qty, bit20asset, price, qty * price))
                bit20_value_cc += qty * price

            except ValueError as e:
                log.debug('Unknown asset on CoinCap: {}'.format(bit20asset))
                coincap_missing_assets.append(bit20asset)

    except Exception as e:
        log.warning('Could not get bit20 assets feed from CoinCap: {}'.format(e))


    bit20_feeds = FeedSet()
    cmc_feed = FeedPrice(bit20_value_cmc, 'BTWTY', 'USD', provider=CoinMarketCapFeedProvider.NAME)
    cc_feed = FeedPrice(bit20_value_cc, 'BTWTY', 'USD', provider=CoinCapFeedProvider.NAME)

    # TODO: simple logic, could do something better here
    # take the feed for the providers that provide a price for all the assets inside the index
    # if none of them can (ie: they all have at least one asset that is not listed), then we
    # take the weighted mean anyway, and hope for the best...
    if not cmc_missing_assets:
        bit20_feeds.append(cmc_feed)
    if not coincap_missing_assets:
        bit20_feeds.append(cc_feed)
    if not bit20_feeds:
        log.warning('All providers have at least one asset not listed:')
        log.warning('- CoinMarketCap: {}'.format(cmc_missing_assets))
        log.warning('- CoinCap: {}'.format(coincap_missing_assets))
        bit20_feeds = FeedSet([cmc_feed, cc_feed])

    bit20_value = bit20_feeds.price(stddev_tolerance=0.02)
    log.debug('Total value of bit20 asset: ${}'.format(bit20_value))

    # get bit20 value in BTS
    bit20_value /= usd_price
    log.debug('Value of the bit20 asset in BTS: {} BTS'.format(bit20_value))

    return 1 / bit20_value


def get_feed_prices(node):
    provider_names = {p.lower() for p in cfg['feed_providers']}
    active_providers = set()
    for name, provider in ALL_FEED_PROVIDERS.items():
        if name in provider_names:
            active_providers.add(provider())

    # get currency rates from yahoo
    # do not include:
    # - BTC as we don't get it from yahoo
    # - USD as it is our base currency
    yahoo = YahooFeedProvider()
    yahoo_prices = yahoo.get(YAHOO_ASSETS | {'CNY'}, 'USD')  # still get CNY, we might need it later

    base_usd_price = yahoo_prices

    CURRENCYLAYER_ACTIVE = False
    if CURRENCYLAYER_ACTIVE:
        try:
            currency_layer = CurrencyLayerFeedProvider()
            currency_layer_prices = currency_layer.get(YAHOO_ASSETS | {'CNY'}, 'USD')

            base_usd_price = FeedSet(yahoo_prices + currency_layer_prices)

        except Exception as e:
            log.debug('Could not get feeds from CurrencyLayer: {}'.format(e))


    # 1- get the BitShares price in major markets: BTC, USD and CNY
    btcavg = core.config['credentials']['bitcoinaverage']

    bitcoinavg = BitcoinAverageFeedProvider(btcavg['secret_key'], btcavg['public_key'])
    bitfinex = BitfinexFeedProvider()
    bitstamp = BitstampFeedProvider()
    bittrex = BittrexFeedProvider()
    btc38 = Btc38FeedProvider()
    bter = BterFeedProvider()
    cmc = CoinMarketCapFeedProvider()
    coincap = CoinCapFeedProvider()
    poloniex = PoloniexFeedProvider()
    yunbi = YunbiFeedProvider()

    # 1.1- first get the bts/btc valuation
    providers_bts_btc = {poloniex, bittrex} & active_providers
    if not providers_bts_btc:
        log.warning('No feed providers for BTS/BTC feed price')
    all_feeds = get_multi_feeds('get', [('BTS', 'BTC')], providers_bts_btc)

    feeds_bts_btc = all_feeds.filter('BTS', 'BTC')
    if not feeds_bts_btc:
        raise core.NoFeedData('Could not get any BTS/BTC feeds')

    btc_price = feeds_bts_btc.price()

    # 1.2- get the btc/usd (bitcoin avg)
    try:
        feeds_btc_usd = FeedSet([bitcoinavg.get('BTC', 'USD')])
    except Exception:
        # fall back on Bitfinex, Bitstamp if BitcoinAverage is down or not configured - TODO: add Kraken, others? CMC
        log.debug('Could not get BTC/USD using BitcoinAverage, trying other sources')
        feeds_btc_usd = get_multi_feeds('get', [('BTC', 'USD')], {bitfinex, bitstamp})

    btc_usd = feeds_btc_usd.price()

    usd_price = btc_price * btc_usd

    # 1.3- get the bts/cny valuation directly from cny markets. Going from bts/btc and
    #      btc/cny to bts/cny introduces a slight difference (2-3%) that doesn't exist on
    #      the actual chinese markets
    providers_bts_cny = {bter, btc38, yunbi} & active_providers

    # TODO: should go at the beginning: submit all fetching tasks to an event loop / threaded executor,
    # compute valuations once we have everything
    #all_feeds.append(get_multi_feeds('get', [('BTS', 'CNY')], providers_bts_cny))
    feeds_bts_cny = get_multi_feeds('get', [('BTS', 'CNY')], providers_bts_cny)
    if not feeds_bts_cny:
        # if we couldn't get the feeds for cny, go BTS->BTC, BTC->CNY
        log.warning('Could not get any BTS/CNY feeds, going BTS->BTC, BTC->CNY')
        bts_cny = btc_price * btc_usd / base_usd_price.price('CNY')

        # # if we couldn't get the feeds for cny, try picking up our last value
        # if price_history.get('cny'):
        #     log.warning('Could not get any BTS/CNY feeds, using last feed price')
        #     bts_cny = price_history['cny'][-1]
        # else:
        #     raise core.NoFeedData('Could not get any BTS/CNY feeds')
    else:
        bts_cny = feeds_bts_cny.price()

    cny_price = bts_cny

    feeds = {}  # TODO: do we really want to reset the global var 'feeds' everytime we come here?
    feeds['BTC'] = btc_price
    feeds['CASH.BTC'] = btc_price
    feeds['USD'] = usd_price
    feeds['TUSD'] = usd_price
    feeds['CASH.USD'] = usd_price
    feeds['CNY'] = cny_price
    feeds['TCNY'] = cny_price

    feeds['HERO'] = usd_price / (1.05 ** ((date.today() - date(1913, 12, 23)).days / 365.2425))

    log.debug('Got btc/usd price: {}'.format(btc_usd))
    log.debug('Got usd price: {}'.format(usd_price))
    log.debug('Got cny price: {}'.format(cny_price))

    # 2- now get the BitShares price in all other required currencies
    for asset in YAHOO_ASSETS:
        feeds[asset] = usd_price / base_usd_price.price(asset)

    # 2.1- RUBLE is used temporarily by RUDEX instead of bitRUB (black swan)
    #      see https://bitsharestalk.org/index.php/topic,24004.0/all.html
    feeds['RUBLE'] = feeds['RUB']

    # 3- get the feeds for major composite indices
    providers_quotes = {yahoo, GoogleFeedProvider(), BloombergFeedProvider()}

    all_quotes = get_multi_feeds('query_quote',
                                 BIT_ASSETS_INDICES.items(),
                                 providers_quotes & active_providers)

    for asset, base in BIT_ASSETS_INDICES.items():
        feeds[asset] = 1 / all_quotes.price(asset, base, stddev_tolerance=0.02)

    # 4- get other assets
    altcap = get_multi_feeds('get', [('ALTCAP', 'BTC')], {coincap, cmc})
    altcap = altcap.price(stddev_tolerance=0.08)
    feeds['ALTCAP'] = altcap

    gridcoin = get_multi_feeds('get', [('GRIDCOIN', 'BTC')], {poloniex, bittrex})
    feeds['GRIDCOIN'] = btc_price / gridcoin.price(stddev_tolerance=0.1)

    steem_btc = get_multi_feeds('get', [('STEEM', 'BTC')], {poloniex, bittrex})
    steem_usd = steem_btc.price() * btc_usd
    feeds['STEEM'] = steem_usd

    # 5- Bit20 asset
    if 'BTWTY' not in get_disabled_assets():
        bit20 = get_bit20_feed(node, usd_price)
        if bit20 is not None:
            feeds['BTWTY'] = bit20

    # 6- update price history for all feeds
    for cur, price in feeds.items():
        price_history[cur].append(price)

    return feeds


def median_str(cur):
    try:
        return statistics.median(price_history[cur])
    except Exception:
        return 'N/A'


def is_extended_precision(asset):
    return asset in {'BTC', 'GOLD', 'SILVER', 'BTWTY'} | set(BIT_ASSETS_INDICES.keys())


def format_qualifier(asset):
    if is_extended_precision(asset):
        return '%g'
    return '%f'


def get_fraction(price, asset_precision, base_precision, N=6):
    """Find nice fraction with at least N significant digits in
    both the numerator and denominator."""
    numerator = int(price * 10 ** asset_precision)
    denominator = 10 ** base_precision
    multiplier = 0
    while len(str(numerator)) < N or len(str(denominator)) < N:
        multiplier += 1
        numerator = round(price * 10 ** (asset_precision + multiplier))
        denominator = 10 ** (base_precision + multiplier)
    return numerator, denominator


def get_price_for_publishing(node, median_feeds, asset, price):
    c = dict(cfg['asset_params']['default'])  # make a copy, we don't want to update the default value
    c.update(cfg['asset_params'].get(asset) or {})
    asset_id = node.asset_data(asset)['id']
    asset_precision = node.asset_data(asset)['precision']

    base = 'BTS'
    if asset == 'ALTCAP':
        base = 'BTC'
    base_id = node.asset_data(base)['id']
    base_precision = node.asset_data(base_id)['precision']
    bts_precision = node.asset_data('BTS')['precision']

    numerator, denominator = get_fraction(price, asset_precision, base_precision)

    # CER price needs to be priced in BTS always. Get conversion rate
    # from the base currency to BTS, and use it to scale the CER price

    if base != 'BTS':
        base_bts_price = median_feeds[base]
        cer_numerator, cer_denominator = get_fraction(price * base_bts_price,
                                                      asset_precision, bts_precision)
        cer_denominator *= c['core_exchange_factor']
    else:
        cer_numerator, cer_denominator = numerator, round(denominator * c['core_exchange_factor'])

    price_obj = {
        'settlement_price': {
            'quote': {
                'asset_id': base_id,
                'amount': denominator
            },
            'base': {
                'asset_id': asset_id,
                'amount': numerator
            }
        },
        'maintenance_collateral_ratio': c['maintenance_collateral_ratio'],
        'maximum_short_squeeze_ratio': c['maximum_short_squeeze_ratio'],
        'core_exchange_rate': {
            'quote': {
                'asset_id': '1.3.0',
                'amount': cer_denominator
            },
            'base': {
                'asset_id': asset_id,
                'amount': cer_numerator
            }
        }
    }
    # log.debug('Publishing feed for {}/{}: {} as {}/{} - CER: {}/{}'
    #          .format(asset, base, price, numerator, denominator, cer_numerator, cer_denominator))
    return price_obj


def get_disabled_assets():
    cfg_enabled = set(cfg.get('enabled_assets', []))
    cfg_disabled = set(cfg.get('disabled_assets', []))
    for asset in cfg_enabled:
        if asset in cfg_disabled:
            log.warning("Asset {} is both in 'enabled_assets' and 'disabled_assets'. Disabling it".format(asset))

    # Steem is disabled as it appears in the feed history but should not be published on BitShares
    # NOTE: the proper fix would be to maintain feeds per client type, and not as a global object
    disabled_assets = {'STEEM'}

    # these are not published by default as they are experimental or have some requirements
    # eg: need to be an approved witness to publish
    disabled_assets.update({'BTWTY', 'RUBLE', 'ALTCAP', 'HERO'})

    # enable plugins in cfg
    disabled_assets.difference_update(cfg_enabled)

    # disable plugins in cfg
    disabled_assets.update(cfg_disabled)

    return disabled_assets


# TODO: Need 2 main classes: FeedHistory is a database of historical prices, allows querying,
#       and FeedControl is a strategy for deciding when to publish

class BitSharesFeedControl(object):
    def __init__(self, *, cfg, visible_feeds=DEFAULT_VISIBLE_FEEDS):
        self.cfg = dict(cfg)
        self.visible_feeds = list(visible_feeds)

        # FIXME: deprecate self.feed_period
        try:
            self.feed_period = int(cfg['publish_time_interval'] / cfg['check_time_interval'])
        except KeyError:
            self.feed_period = None

        self.check_time_interval = pendulum.interval(seconds=cfg.get('check_time_interval', 300))
        self.publish_time_interval = None
        if 'publish_time_interval' in cfg:
            self.publish_time_interval = pendulum.interval(seconds=cfg['publish_time_interval'])

        self.feed_slot = cfg.get('publish_time_slot', None)
        if self.feed_slot is not None:
            self.feed_slot = int(self.feed_slot)

        self.nfeed_checked = 0
        self.last_published = pendulum.utcnow().subtract(days=1)

    def format_feeds(self, feeds):
        display_feeds = []
        for c in set(self.visible_feeds) | set(feeds.keys()):
            if c not in feeds:
                log.warning('No feed price available for {}, cannot display it'.format(c))
            else:
                display_feeds.append(c)
        display_feeds = list(sorted(feeds))

        fmt = ', '.join('%s %s' % (format_qualifier(c), c) for c in display_feeds)
        msg = fmt % tuple(feeds[c] for c in display_feeds)
        return msg

    def publish_status(self, feeds):
        status = ''
        if self.publish_time_interval:
            #status += ' [%d/%d]' % (self.nfeed_checked, self.feed_period)
            status += ' [every {}]'.format(self.publish_time_interval)
        if self.feed_slot:
            status += ' [t=HH:{:02d}]'.format(self.feed_slot)

        result = '{} {}'.format(self.format_feeds(feeds), status)
        #log.debug('Got feeds: {}'.format(result))
        return result


    def should_publish(self):
        # TODO: update according to: https://bitsharestalk.org/index.php?topic=9348.0;all

        #return False
        if self.nfeed_checked == 0:
            log.debug('Should publish at least once at launch of the bts_tools')
            return True

        if self.feed_period is not None and self.nfeed_checked % self.feed_period == 0:
            log.debug('Should publish because time interval has passed: {} seconds'.format(cfg['publish_time_interval']))
            return True



        now = pendulum.utcnow()

        if self.publish_time_interval and now - self.last_published > self.publish_time_interval:
            log.debug('Should publish because time interval has passed: {}'.format(self.publish_time_interval))
            return True

        target = now.replace(minute=self.feed_slot, second=0, microsecond=0)
        targets = [target.subtract(hours=1), target, target.add(hours=1)]
        diff = [now-t for t in targets]
        # check if we just passed our time slot
        if any(pendulum.interval() < d and abs(d) < 1.5*self.check_time_interval for d in diff):

            log.debug('Should publish because time slot has arrived: time {:02d}:{:02d}'.format(now.hour, now.minute))
            return True

        # check_time_interval_minutes = cfg['check_time_interval'] // 60 + 1
        # if self.feed_slot is not None:
        #     start_slot = self.feed_slot
        #     end_slot = self.feed_slot + check_time_interval_minutes
        #     if (((start_slot <= now.minute <= end_slot) or
        #          (end_slot >= 60 and now.minute <= end_slot % 60)) and
        #         now - self.last_published > timedelta(minutes=max(3*check_time_interval_minutes, 50))):
        #
        #         log.debug('Should publish because time slot has arrived: time {:02d}:{:02d} - target'.format(now.hour, now.minute))
        #         return True

        log.debug('No need to publish feeds')
        return False

    def should_publish_steem(self, node, price):
        # check whether we need to publish again:
        # - if published more than 12 hours ago, publish again
        # - if published price different by more than 3%, publish again
        if 'last_price' not in node.opts:  # make sure we have already published once
            log.debug('Steem should publish for the first time since launch of bts_tools')
            return True

        last_published_interval = pendulum.interval(hours=12)
        variance_trigger = 0.03

        if pendulum.utcnow() - node.opts['last_published'] > last_published_interval:
            log.debug('Steem should publish as it has not been published for {}'.format(last_published_interval))
            return True
        if abs(price - node.opts['last_price']) / node.opts['last_price'] >= variance_trigger:
            log.debug('Steem should publish as price has moved more than {}%'.format(100*variance_trigger))
            return True
        log.debug('No need for Steem to publish')
        return False


def check_feeds(nodes):
    # need to:
    # 1- get all feeds from all feed providers (FP) at once. Only use FP designated as active
    # 2- compute price from the FeedSet using adequate strategy. FP can (should) be weighted
    #    (eg: BitcoinAverage: 4, BitFinex: 1, etc.) if no volume is present
    # 3- order of computation should be clearly defined and documented, or configurable in the yaml file (preferable)
    #
    # details:
    #
    # 1-
    # - for each asset, get the list of authorized and authenticated feed providers able to give a feed for it
    # - for each feed provider, get the list of required assets
    # - execute in parallel all requests and gather the results in a FeedSet

    global feeds, feed_control

    try:
        feeds = get_feed_prices(nodes[0]) # use first node if we need to query the blockchain, eg: for bit20 asset composition
        feed_control.nfeed_checked += 1

        status = feed_control.publish_status(feeds)
        log.debug('Got feeds: {}'.format(status))

        for node in nodes:
            if node.role != 'feed_publisher':
                continue

            # TODO: dealt with as an exceptional case for now, should be refactored
            if node.type() == 'steem':
                price = statistics.median(price_history['STEEM'])

                # publish median value of the price, not latest one
                if feed_control.should_publish_steem(node, price):
                    if not node.is_online():
                        log.warning('Cannot publish feeds for steem witness %s: client is not running' % node.name)
                        continue
                    if node.is_locked():
                        log.warning('Cannot publish feeds for steem witness %s: wallet is locked' % node.name)
                        continue

                    ratio = cfg['steem_dollar_adjustment']
                    price_obj = {'base': '{:.3f} SBD'.format(price),
                                 'quote': '{:.3f} STEEM'.format(1/ratio)}
                    log.info('Node {} publishing feed price for steem: {:.3f} SBD (real: {:.3f} adjusted by {:.2f})'
                             .format(node.name, price*ratio, price, ratio))
                    node.publish_feed(node.name, price_obj, True)
                    node.opts['last_price'] = price
                    node.opts['last_published'] = pendulum.utcnow()

                continue


            # if an exception occurs during publishing feeds for a delegate (eg: standby delegate),
            # then we should still go on for the other nodes (and not let exceptions propagate)
            try:
                if feed_control.should_publish():
                    if not node.is_online():
                        log.warning('Cannot publish feeds for witness %s: client is not running' % node.name)
                        continue
                    if node.is_locked():
                        log.warning('Cannot publish feeds for witness %s: wallet is locked' % node.name)
                        continue
                    if not node.is_synced():
                        log.warning('Cannot publish feeds for witness {}: client is not synced'.format(node.name))
                        continue
                    # publish median value of the price, not latest one
                    median_feeds = {c: statistics.median(price_history[c]) for c in feeds}
                    disabled_assets = get_disabled_assets()
                    publish_feeds = {asset: price for asset, price in median_feeds.items() if asset not in disabled_assets}
                    log.info('Node %s publishing feeds: %s' % (node.name, feed_control.format_feeds(publish_feeds)))
                    log.debug('Not publishing: {}'.format(disabled_assets))

                    # first, try to publish all of them in a single transaction
                    try:
                        published = []
                        handle = node.begin_builder_transaction()
                        for asset, price in publish_feeds.items():
                            published.append(asset)
                            op = [19,  # id 19 corresponds to price feed update operation
                                  hashabledict({"asset_id": node.asset_data(asset)['id'],
                                                "feed": get_price_for_publishing(node, publish_feeds, asset, price),
                                                "publisher": node.get_account(node.name)["id"]})
                                  ]
                            node.add_operation_to_builder_transaction(handle, op)

                        # set fee
                        node.set_fees_on_builder_transaction(handle, '1.3.0')

                        # sign and broadcast
                        node.sign_builder_transaction(handle, True)
                        log.debug('Successfully published feeds for {}'.format(', '.join(published)))


                    except Exception as e:
                        log.warning('Tried to publish all feeds in a single transaction, but failed. Will try to publish each feed separately now')
                        msg_len = 400
                        log.debug(str(e)[:msg_len] + (' [...]' if len(str(e)) > msg_len else ''))

                        # if an error happened, publish feeds individually to make sure that
                        # at least the ones that work can get published
                        published = []
                        for asset, price in publish_feeds.items():
                            try:
                                price = hashabledict(get_price_for_publishing(node, publish_feeds, asset, price))
                                log.debug('Publishing {} {}'.format(asset, price))
                                node.publish_asset_feed(node.name, asset, price, True)  # True: sign+broadcast
                                published.append(asset)
                            except Exception as e:
                                #log.exception(e)
                                log.warning('Failed to publish feed for asset {}'.format(asset))
                                log.debug(str(e)[:msg_len] + ' [...]')

                        log.debug('Successfully published feeds for {}'.format(', '.join(published)))

                    #last_published = datetime.utcnow()
                    feed_control.last_published = pendulum.utcnow()

            except Exception as e:
                log.exception(e)

    except core.NoFeedData as e:
        log.warning(e)

    except Exception as e:
        log.exception(e)

    threading.Timer(cfg['check_time_interval'], check_feeds, args=(nodes,)).start()
