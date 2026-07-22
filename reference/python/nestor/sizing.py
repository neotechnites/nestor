"""Sizing for the weather sleeve: flat small dollars, not % of bankroll
(these markets are thin). Contracts = floor(stake / entry_price)."""


def contracts_for(stake_usd, entry_price_cents):
    entry_dollars = entry_price_cents / 100.0
    if entry_dollars <= 0:
        return 0
    return int(stake_usd // entry_dollars)
