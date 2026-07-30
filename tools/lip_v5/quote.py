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
    # -----------------------------------------------------------------------------------------
    # NO-CHANGE SUPPRESSION (2026-07-29).  A requote that alters neither our PRICE nor our SIZE
    # cannot alter our score, and it is not free: cancel-then-place surrenders queue position,
    # opens a coverage gap in a metric sampled once per second, and spends a rate-budget round
    # trip.  MEASURED on the live tape: median order lifetime 1.9 SECONDS, and 73.9% of 4,267
    # re-posts were at the SAME PRICE as the post they replaced.  The book had an order actually
    # resting 10.6% of the time, and since accrual is proportional to presence that alone capped
    # earnings at a tenth of the same capital's potential before any other decision was made.
    # WHICH TRIGGERS THIS TOUCHES.  (c) S_MOVED and (e) RESYNC are the two that can fire while
    # nothing about our order needs to differ — (c) reacts to a 25% move in the RIVAL score,
    # which changes the share landscape but not our own quote, and (e) is a staleness timer.
    # (a) OFF_BEST changes the price, (b) REFILL changes the size and (d) QUALIFIES changes
    # whether the side exists at all: those must never be suppressed.
    # MIRROR (suppressing too much ↔ churning): the stale end is still guarded, because (a) and
    # (d) are exempt and both are book events — a quote that has drifted off best or whose side
    # vanished is requoted immediately regardless of age.  What is removed is only the
    # cancel-and-replace-identically path, which by construction cannot improve anything.
    at_touch = at_best(our_price_c, best_price_c)
    refill_needed = bool(target_q) and \
        float(remaining) < float(refill_frac) * float(target_q) - 1e-12
    if at_touch and not refill_needed:
        trig = [t for t in trig if t not in (TRIG_S_MOVED, TRIG_RESYNC)]
    return trig


# ── `shed_side`, `held_leg_of`, `shed_price` AND `shed_qty` ARE GONE. ────────────────────────
# They were the whole geometry of an exit: which slot a held leg sells into, which leg is
# held, what price joins the OPPOSING queue, and how many contracts unwind without flipping.
# The bot never sells (owner decision, 2026-07-30), so none of the four has a question left to
# answer.  Deleting them is also the structural guarantee: `engine` cannot accidentally
# re-derive an exit price, because there is no function in this module that computes one.
#
# NOT AFFECTED, and the confusion worth naming: an ASK is not a shed.  `would_cross` below
# still guards ask-side quoting, and the requoter still prices an ask at its own same-side
# best.  An ask posts NO-side collateral to OPEN a position and earn the NO half of the pool.
# A shed posted at the opposing best to REDUCE one.  Same wire verb, opposite acts.


def same_second(now, placed_ts):
    """spec §4.2's whole-second policy, degraded path only: never voluntarily cancel-and-
    replace inside the same integer second as the placement — the exchange snapshot cadence
    is ~1/s, so a sub-second cancel risks a coverage sample for zero price improvement.
    (MBB has g = 0 and needs no policy; this is the cancel-first path's guard.)"""
    return placed_ts is not None and int(float(now)) == int(float(placed_ts))


def would_cross(side, price_yes, yes_bid, yes_ask):
    """**A MAKER NEVER TAKES.**  True iff posting `price_yes` (YES axis) on `side` would be
    marketable against the opposing best.

    Paid for live 2026-07-28: v5 posted a 3c bid on a rung whose opposing side was AT 3c, the
    order was taken on contact — 4 placements, ~200 contracts, $6.05 — and because a fully
    taken order leaves nothing resting, the next cycle saw an empty slot and posted again.
    Crossing therefore costs twice: the spread we pay, and the presence we never establish.
    `shed_price` already refused a crossed book on the EXIT path; the entry path had no such
    guard, which is the asymmetry this closes.

    Freshness cannot substitute for this.  Even a one-second-old book can lock or cross before
    our POST lands, so the check must be structural rather than a matter of polling harder.

    MIRROR (refusing to cross ↔ never quoting at all): we do not widen away from the book —
    the caller SKIPS this cycle and re-derives next cycle from a fresh read.  A missed cycle
    costs presence-seconds; a crossed quote costs the spread AND the presence, so the
    asymmetry favours skipping.  Unknown opposing side ⇒ NOT crossing: a book we cannot see is
    not evidence of a lock, and refusing on missing data would silently empty the whole book.
    """
    p = float(price_yes)
    if side == "bid":
        return yes_ask is not None and p >= float(yes_ask) - 1e-12
    return yes_bid is not None and p <= float(yes_bid) + 1e-12
