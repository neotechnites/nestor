"""
lip_v5.quote — the PURE half of the requoting stage (spec §4 / v1 §4.1-4.7), ported from
v4's prod-proven shapes on their merits.

This module holds only decisions computable from numbers: the §4.3 trigger set, at-best,
the shed geometry, and the whole-second policy.  The STATEFUL half — make-before-break, the
cancel-first degrade, the one path to the wire — lives in `engine.requote_pass`, because it
must go through `Maker.place`/`Maker.cancel` and nothing else.

The finding this file answers (finish-round charter, item A): the assembled v5 computed
allocations and DROPPED them — `Maker.place()` had zero call sites.  A maker with no
requoter is a metering appliance.
"""

from . import config as C


# §4.3's five triggers, v4's names kept so the two tapes read alike.
TRIG_OFF_BEST, TRIG_REFILL, TRIG_S_MOVED, TRIG_QUALIFIES, TRIG_RESYNC = (
    "a_off_best", "b_refill", "c_S_moved", "d_qualifies_flipped", "e_safety_resync")


def at_best(our_price_c, best_price_c):
    """§4.5 — our resting price equals the same-side best."""
    return our_price_c is not None and best_price_c is not None and \
        int(our_price_c) == int(best_price_c)


def requote_triggers(our_price_c, best_price_c, remaining, target_q, S_now, S_ref,
                     qualifies_now, qualifies_ref, resting_age_s, since_resync_s,
                     refill_frac=C.REFILL_TRIGGER_FRAC,
                     s_move_frac=C.S_MOVE_TRIGGER_FRAC,
                     resync_s=C.SAFETY_RESYNC_S,
                     min_life_s=C.MIN_RESTING_LIFE_S):
    """spec §4.1 / v1 §4.3 — requote triggers are BOOK EVENTS, not timers, evaluated each
    cycle:

        (a) off same-side best           (overrides the minimum resting life)
        (b) remaining < 50% of target    (refill)
        (c) S moved > 25%                (the share landscape changed)
        (d) qualification flipped        (the side appeared/vanished — overrides min life)
        (e) safety resync at 60 s        (catches missed stream events; doubles as WS re-proof)

    §4.4 / anti-gaming P1: the 30 s MINIMUM RESTING LIFE suppresses every trigger except
    (a) and (d) — a voluntary requote inside 30 s is indistinguishable from cancel-on-
    approach, and honorable quotes rest.  MIRROR (requoting too eagerly ↔ a stale quote):
    the min-life is the eager end's guard; trigger (e) is the stale end's — no quote can sit
    unexamined longer than SAFETY_RESYNC_S even if every book event was missed.
    """
    trig = []
    if not at_best(our_price_c, best_price_c):
        trig.append(TRIG_OFF_BEST)                                   # (a), overrides §4.4
    if target_q and float(remaining) < float(refill_frac) * float(target_q) - 1e-12:
        trig.append(TRIG_REFILL)                                     # (b)
    if S_ref and abs(float(S_now) - float(S_ref)) > float(s_move_frac) * float(S_ref):
        trig.append(TRIG_S_MOVED)                                    # (c)
    if bool(qualifies_now) != bool(qualifies_ref):
        trig.append(TRIG_QUALIFIES)                                  # (d)
    if float(since_resync_s) >= float(resync_s):
        trig.append(TRIG_RESYNC)                                     # (e)
    if float(resting_age_s) < float(min_life_s):
        trig = [t for t in trig if t in (TRIG_OFF_BEST, TRIG_QUALIFIES)]
    return trig


def shed_side(held_leg):
    """v1 §5.4 / v4 D4 — the shed of a YES position is an ASK order, which IS a NO bid: it
    still scores, needs no fresh collateral (the position covers it), and unwinds the
    inventory.  The shed is not a separate action; it is the OPPOSING slot's quote."""
    return "ask" if held_leg == "yes" else "bid"


def held_leg_of(net_yes):
    """Which leg a net-YES position holds.  net > 0 holds YES; net < 0 holds NO."""
    return "yes" if float(net_yes) > 0 else "no"


def shed_price(held_leg, yes_bid, yes_ask):
    """The shed's price on the YES axis: the OPPOSING side's best — joining that queue, never
    crossing (G6 stays off; a crossing exit is a Ryan-gated spend).

    MIRROR (a shed that crosses ↔ a shed that never fills): pricing at the opposing best is
    the never-cross guard, and the CROSSED-BOOK refusal below is its assert — if best ask ≤
    best bid the book is broken and any "join" would in fact cross, so we refuse to price at
    all.  The never-fills end is bounded by settlement: `expiration_ts` backstops the order
    and L_eff already prices the wait.

    Returns the YES-axis price in dollars, or None when it cannot be priced safely.
    """
    if yes_bid is None or yes_ask is None:
        return None
    if float(yes_ask) <= float(yes_bid):
        return None                           # crossed/locked book: nothing joins safely
    return float(yes_ask) if held_leg == "yes" else float(yes_bid)


def shed_qty(net_yes, target=None):
    """C8 (v4, kept): clamp the shed at |net| so it can never FLIP the position — a 40-lot
    shed against 20 held is not a shed, it is a fresh opposite position wearing a shed's
    name.  Sub-contract dust is untradeable and reports as 0."""
    n = abs(float(net_yes))
    if target is not None:
        n = min(n, abs(float(target)))
    return int(n)                             # floor: never round UP into a flip


def same_second(now, placed_ts):
    """spec §4.2's whole-second policy, degraded path only: never voluntarily cancel-and-
    replace inside the same integer second as the placement — the exchange snapshot cadence
    is ~1/s, so a sub-second cancel risks a coverage sample for zero price improvement.
    (MBB has g = 0 and needs no policy; this is the cancel-first path's guard.)"""
    return placed_ts is not None and int(float(now)) == int(float(placed_ts))
