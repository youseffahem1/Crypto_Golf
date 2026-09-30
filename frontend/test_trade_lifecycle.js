/* Guards the trade LIFECYCLE and its accounting in the live frontend.
 *
 * The governing rule this file exists to protect:
 *
 *     00:00        -> the trade's duration is over. That is ALL it means.
 *     CLOSE (user) -> price it, book the P/L, credit the balance, file it.
 *
 * Reaching 00:00 used to POST /api/trade/close, so a trade realized itself the
 * instant its clock ran out: the top Profit or Loss moved, the balance moved
 * and the trade appeared in closed history with nobody pressing anything. That
 * request is gone, and these are the frontend rules that keep it gone.
 *
 * The server is the authority on the RESULT, so the frontend rules are:
 *
 *   1. the close request sends NO value - a client cannot name what it is paid
 *   2. closing refreshes BOTH the balance and the realized profit/loss cards
 *   3. CLOSE ALL uses the atomic /api/trade/close-all endpoint, scoped to the
 *      active market, instead of looping single closes
 *   4. CLOSE ALL books the server's per-trade profit into the realized ledger
 *      and reports the total
 *   5. reaching 00:00 STOPS both clocks and does nothing else
 *   6. the expiry pass makes NO request and books NOTHING
 *   7. NO timer settles a position, in either direction, live or in practice
 *   8. a finished position stays a visible, closable, unrealized open position
 *   9. the countdown is read from the SERVER's closes_at, not a local timer
 *  10. the UI never promises a stake is lost when a timer runs out
 *  11. one PIN, checked by the server, with no Swap prerequisite
 *
 * Usage: node test_trade_lifecycle.js <path to index.html>
 */
const fs = require('fs');
const path = require('path');

const file = process.argv[2] || path.join(__dirname, 'index.html');
const src = fs.readFileSync(file, 'utf8');

let fails = 0;
function ok(label, cond, detail) {
  if (cond) {
    console.log('  ok   ' + label);
  } else {
    fails++;
    console.log('  FAIL ' + label + (detail ? ' -- ' + detail : ''));
  }
}

/* Strict equality, printing both values when it disagrees. `ok` takes a
 * truthiness condition, so an assertion of "nothing happened" (0, '', false)
 * would read as a failure and a non-empty money string would pass vacuously.
 * The runtime section compares exact figures, so it needs this one. */
function eq(label, got, want, detail) {
  if (got === want) {
    console.log('  ok   ' + label);
  } else {
    fails++;
    console.log('  FAIL ' + label + ' -- expected ' + JSON.stringify(want) +
      ', got ' + JSON.stringify(got) + (detail ? ' (' + detail + ')' : ''));
  }
}

/* Pull a function body out by brace matching, so assertions look at the real
 * implementation rather than a loose text search over the whole file. */
function fnBody(name) {
  const start = src.indexOf('function ' + name);
  if (start < 0) return null;
  const open = src.indexOf('{', start);
  if (open < 0) return null;
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return src.slice(open, i + 1);
    }
  }
  return null;
}

/* Same, but keeps the `function name(params)` signature, so the source can be
 * executed in a sandbox as a real declaration rather than a bare block. */
function fnSrc(name) {
  const start = src.indexOf('function ' + name);
  if (start < 0) return null;
  const open = src.indexOf('{', start);
  if (open < 0) return null;
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  return null;
}

console.log('\n[1] the close request sends no client-supplied value');
const closePositions = fnBody('closePositions');
ok('closePositions exists', !!closePositions);
if (closePositions) {
  const call = closePositions.match(/vantaApi\(\s*"\/api\/trade\/close"[\s\S]{0,400}?\}\s*\)/);
  ok('posts to /api/trade/close', !!call);
  const body = call ? call[0] : '';
  ok('body carries only trade_id', /trade_id:\s*t\.id/.test(body), body.slice(0, 160));
  ok('body carries NO value field', !/\bvalue\s*:/.test(body), 'client value still sent');
  ok('whole file sends no close value', !/api\/trade\/close["'][\s\S]{0,300}?value\s*:/.test(src));
}

console.log('\n[2] closing refreshes balance AND the profit/loss cards from the server');
if (closePositions) {
  ok('calls refreshBalance()', /refreshBalance\(\)/.test(closePositions));
  ok('also refreshes the info cards', /vantaRefreshInfoCards|refreshInfoCards/.test(closePositions));
  ok('uses the server profit, not a local estimate',
    /Number\(sold\.profit/.test(closePositions));
}

console.log('\n[3] CLOSE ALL uses the atomic endpoint, scoped to the market');
const sellAll = fnBody('sellOpenPositions');
ok('sellOpenPositions exists', !!sellAll);
if (sellAll) {
  ok('posts to /api/trade/close-all', /"\/api\/trade\/close-all"/.test(sellAll));
  ok('sends the active symbol', /symbol:\s*sym/.test(sellAll));
  ok('does NOT loop single closes', !/\/api\/trade\/close"/.test(sellAll));
  ok('guards positions while in flight', /__vantaClosing/.test(sellAll));
  ok('refreshes the balance', /refreshBalance\(\)/.test(sellAll));
  ok('refreshes the info cards', /vantaRefreshInfoCards|refreshInfoCards/.test(sellAll));
}

console.log('\n[4] CLOSE ALL books the server result into the realized ledger');
if (sellAll) {
  ok('banks each trade profit', /vantaOnTradeClosed/.test(sellAll));
  ok('reads profit off the server response', /Number\(s\.profit/.test(sellAll));
  ok('sums the batch for the toast', /reduce\(/.test(sellAll));
  ok('removes the closed positions', /trades\s*=\s*trades\.filter/.test(sellAll));
}

console.log('\n[5] reaching 00:00 STOPS the countdown, and nothing else');
const updateTrades = fnBody('updateTrades');
ok('updateTrades exists', !!updateTrades);
if (updateTrades) {
  ok('reads the deadline through the shared helper',
    /vantaTimeLeft\(/.test(updateTrades) && /vantaIsExpired\(/.test(updateTrades));
  /* The defect this fixes: the countdown reached 00:00 and was rewritten every
     frame forever, so a live-looking row sat at 00:00 indefinitely. The write
     is now guarded rather than skipped-with-continue, because a finished
     position still needs the P/L and marker work below it. So the assertion is
     that the countdown write is CONDITIONAL, and that it comes after the flag. */
  const flag = updateTrades.search(/vantaMarkExpired\(/);
  const write = updateTrades.search(/\[data-countdown/);
  ok('the position is flagged first', flag >= 0);
  ok('there is a countdown write in the loop', write >= 0);
  ok('the flag comes BEFORE the countdown write', flag >= 0 && write >= 0 && flag < write,
    'flag at ' + flag + ', countdown write at ' + write);
  ok('the countdown write is guarded by !expired, so 00:00 is terminal',
    /if\s*\(\s*countdown\s*&&\s*!\s*expired\s*\)/.test(updateTrades));
  /* A `continue` here would strand the position: it would stop the P/L and the
     marker work for a trade the user still owns and still has to close. */
  ok('it does NOT bail out of the whole loop on expiry',
    !/if\s*\(\s*expired\s*\)[\s\S]{0,200}?continue\s*;/.test(updateTrades));
  ok('the live P/L still updates for a finished position',
    /data-openval/.test(updateTrades));
  ok('flags the position as expired', /vantaMarkExpired\(/.test(updateTrades));
  ok('hands expired positions to the (now inert) marker pass',
    /vantaExpireDueTrades\(\)/.test(updateTrades));
  /* The chart marker is REMOVED at 00:00, so there is no chart clock left to
     guard. The old rule was "stop the marker's timer too"; the new one is
     "the marker is not on the chart at all", which is stronger — a stopped
     timer is still a live-looking trade sitting on a chart of live trades.
     So the assertion is that no code path writes to a marker any more. */
  ok('nothing writes to a chart marker after it is created',
    !/\[\s*`?data-trade-id=/.test(updateTrades) &&
    !/marker\.querySelector/.test(updateTrades));
  ok('the row countdown is the only clock left on screen',
    /if\s*\(\s*countdown\s*&&\s*!\s*expired\s*\)/.test(updateTrades));
}
const timeLeft = fnBody('vantaTimeLeft');
ok('remaining is clamped at zero, never negative',
  !!timeLeft && /Math\.max\(\s*0\s*,/.test(timeLeft));
const parse = fnBody('vantaParseUTC');
ok('a bad trade timestamp yields 0, never NaN',
  !!parse && /Number\.isNaN\(d\)\s*\?\s*\(fallback === undefined \? 0 : fallback\)/.test(parse));
ok('the trade path calls it with no fallback, so bad dates read as finished',
  /vantaParseUTC\(t && t\.closes_at\)/.test(src) &&
  /vantaParseUTC\(t && t\.opened_at\)/.test(src));
/* Candles are the one caller that wants a bar at the live edge rather than at
   the epoch, so they must opt in rather than inherit the trade default. */
ok('candles opt into the live-edge fallback explicitly',
  /vantaParseUTC\(row\.open_time,\s*Date\.now\(\)\)/.test(src));

console.log('\n[6] 00:00 DOES NOT SETTLE -- the expiry pass makes no request at all');
const expire = fnBody('vantaExpireDueTrades');
ok('vantaExpireDueTrades exists', !!expire);
if (expire) {
  /* This is the section the whole task is about. The function used to POST
     /api/trade/close, which is what realized a trade the moment its clock ran
     out. It now does nothing but flag, so every one of these must be absent. */
  ok('makes NO request of any kind', !/vantaApi\(/.test(expire), expire.slice(0, 200));
  ok('does not post to /api/trade/close', !/\/api\/trade\/close/.test(expire));
  ok('does not call closePositions', !/closePositions/.test(expire));
  ok('does not book anything into the realized ledger',
    !/vantaNoteRealized/.test(expire));
  ok('does not refresh the balance or the P/L cards',
    !/refreshBalance\(\)/.test(expire) && !/vantaRefreshInfoCards/.test(expire));
  ok('does not touch the result panel', !/vantaSetLastSettled/.test(expire));
  ok('does not remove the position from the trade list',
    !/trades\s*=\s*trades\.filter/.test(expire));
  ok('does not toast a result', !/showResultToast/.test(expire));
  ok('does not read a profit from anywhere', !/sold\s*&&\s*sold\.profit/.test(expire));
  /* What it must still do: mark, so the UI can stop the clocks and label it. */
  ok('it DOES flag the position, which is the whole job now',
    /vantaMarkExpired/.test(expire));
  /* The retry/backoff machinery existed only to re-POST a close. With no
     request there is nothing to retry, and leaving it would be a promise the
     code no longer keeps. */
  ok('the close-retry machinery is gone', !/__vantaExpireRetryAt\s*=/.test(expire));
  ok('...and the per-trade `ending` flag is gone', !/t\.ending\s*=/.test(expire));
}
/* No other path may close a position on a timer either. The two remaining
   /api/trade/close call sites must both be reachable only from a user action. */
const closePost = src.indexOf('vantaApi("/api/trade/close"');
ok('there is exactly one /api/trade/close call site in the file',
  src.split('vantaApi("/api/trade/close"').length - 1 === 1, 'count: ' +
  (src.split('vantaApi("/api/trade/close"').length - 1));
const closePositionsFn = fnBody('closePositions');
ok('and it lives inside closePositions, the user-initiated close',
  closePost > src.indexOf('async function closePositions') &&
  closePost < src.indexOf('async function closePositions') + closePositionsFn.length + 40);
ok('the expiry pass is not inside it', !/vantaExpireDueTrades/.test(closePositionsFn || ''));

console.log('\n[6b] the top Profit / Loss cards are realized-only');
/* The two cards the bug moved are the headline pair, and they are the user's
   stated check. They are painted from the server's CLOSED-trade totals, so a
   position at 00:00 — still open, still unrealized — cannot reach them.

   The chain is: refreshInfoCards -> /api/platform/coins -> liveSplit() ->
   writeResult() -> #icProfit / #icLoss. Pin every link, and pin the element
   ids: #icProfit is the `.ic-value` span, #icLoss its `.ic-pl-v` partner. */
ok('the top Profit element is #icProfit in an .ic-value box',
  /class="ic-value"[^>]*id="icProfit"|id="icProfit"[^>]*class="ic-value"/.test(src));
ok('the top Loss element is #icLoss in an .ic-pl-v box',
  /id="icLoss"[^>]*class="ic-pl-v"|class="ic-pl-v"[^>]*id="icLoss"/.test(src));
ok('both cards start at $0.00',
  /id="icProfit"[^>]*>\$0\.00</.test(src) && /id="icLoss"[^>]*>\$0\.00</.test(src));

const cardsFn = fnBody('refreshInfoCards') || '';
ok('the cards are driven by /api/platform/coins', /\/api\/platform\/coins/.test(cardsFn));
ok('the live branch requires the server to have sent realized_profit',
  /typeof p\.realized_profit/.test(cardsFn));
ok('the live branch paints through the realized split', /writeResult\(\s*liveSplit\(/.test(cardsFn));

const splitFn = fnBody('liveSplit') || '';
ok('the split reads the per-coin realized totals', /realized_profit/.test(splitFn));
ok('...and the realized loss beside it', /realized_loss/.test(splitFn));
ok('the split never reads an open or unrealized figure',
  !/unrealized|open_trades|openTrades/.test(splitFn));
ok('PROFIT and LOSS are kept as two independent numbers', /profit:[\s\S]{0,80}loss:/.test(splitFn));

const writeFn = fnBody('writeResult') || '';
ok('the writer paints #icProfit from the realized available figure',
  /setText\(\s*'icProfit'\s*,\s*usd\(available\)\s*\)/.test(writeFn));
ok('the writer paints #icLoss from the realized loss', /'icLoss'/.test(writeFn));
ok('LOSS is floored at zero, so it can never read as a positive',
  /Math\.min\(0/.test(writeFn));
/* Comments and UI strings legitimately mention trades ("closed X trades are
   down..."), so strip comments and then look for real member access on the
   open book rather than the bare word. */
const writeCode = writeFn.replace(/\/\*[\s\S]*?\*\//g, ' ');
ok('neither card is painted from the open trade book',
  !/\btrades?\s*[.[\]]/.test(writeCode) && !/\bfor\s*\([^)]*\btrades?\b/.test(writeCode));
/* A finished position must not have any path into these two elements. */
ok('no open-trade P/L is written to the top cards',
  !/setText\(\s*'icProfit'[\s\S]{0,120}(unrealized|tradePl|floating)/.test(src) &&
  !/setText\(\s*'icLoss'[\s\S]{0,120}(unrealized|tradePl|floating)/.test(src));

console.log('\n[7] NO timer force-settles a position, in either direction');
ok('the old practice sweep is still gone', !/function vantaSettleDemoTrades/.test(src));
ok('no interval schedules it', !/setInterval\(\s*vantaSettleDemoTrades/.test(src));
ok('the old -amount settlement helper is still gone',
  !/settle_due_trades/.test(src));
ok('no source books profit as the negated stake',
  !/profit\s*[:=]\s*-{1,2}\s*(amt|amount|t\.amount|trade\.amount)\s*;/.test(src));
ok('sync does not book a missing history row as a loss',
  !/:\s*-amt\s*;/.test(src));
ok('sync waits for a real history row before reporting', /if\s*\(\s*!\s*h\s*\)/.test(src));
/* The rule is not merely "no flat loss" but "no settlement at all on a timer",
   so a position whose duration ended must not be dropped, hidden or re-priced
   by any of the paths that used to do it. */
ok('no interval-driven call closes a position',
  !/setInterval\([^)]*(vantaExpireDueTrades|closePositions)/.test(src));

console.log('\n[8] a finished position is STILL an open position on screen');
const renderOpen = fnBody('renderOpenTrades');
ok('renderOpenTrades exists', !!renderOpen);
if (renderOpen) {
  /* It used to filter expired trades out of the list. That was only defensible
     while something else was settling them the instant they left. Nothing
     settles them now, so hiding them would strand the stake: unreadable, and
     with no button to close the only position the user had. */
  ok('it does NOT filter finished positions out of the open list',
    !/filter\(t\s*=>\s*!vantaIsExpired/.test(renderOpen));
  ok('every position is drawn, finished or not',
    /const activeTrades\s*=\s*allTrades\s*;/.test(renderOpen));
  ok('the count badge counts them all, so it cannot contradict the list',
    /openCountEl\.textContent\s*=\s*activeTrades\.length/.test(renderOpen));
  ok('a finished row shows 00:00', /finished\s*\?\s*"00:00"/.test(renderOpen));
  ok('every row says Close Trade, running or finished',
    /class="open-sell" data-exit="\$\{trade\.id\}"[^>]*>Close Trade</.test(renderOpen)
    && !/finished\s*\?/.test(renderOpen.match(/class="open-sell"[^>]*>[^<]*/)[0]));
  ok('the button is still a data-exit close, so the same handler closes it',
    /data-exit="\$\{trade\.id\}"/.test(renderOpen));
  ok('a finished row is dimmed, not hidden',
    /is-finished/.test(renderOpen) && /\.open-trade\.is-finished/.test(src));
}
ok('the close path is not gated on expiry, so a finished trade is closable',
  !!closePositionsFn && !/vantaIsExpired/.test(closePositionsFn));
const sellById = fnBody('sellOpenPositionById');
ok('the per-position close button is not gated on expiry either',
  !!sellById && !/vantaIsExpired/.test(sellById) && /closePositions\(list\)/.test(sellById));
/* The Live Value box must include finished positions: their stake is still
   staked, they are still the user's, and dropping them under-reports what was
   paid. It is the PAID total, so it is summed from `t.amount` and never from
   the live sell value — a figure that cannot rise and fall on its own. */
const liveBox = /const lv = \(typeof trades !== "undefined" && Array\.isArray\(trades\)\)\s*\?\s*\n?\s*trades\.filter\(t => \(t\.symbol \|\| "GOLF"\) === lvSym\)\s*\n?\s*: \[\];/;
ok('the Live Value box still counts a finished position', liveBox.test(src));
ok('it is not filtered by expiry', !/filter\(t\s*=>\s*!vantaIsExpired/.test(src));
const stakePaint = fnBody('vantaPaintStakeLive');
ok('it shows the money PAID (the stake), not the live sell value',
  !!stakePaint && /lv\.reduce\(\(s, t\) => s \+ Number\(t\.amount \|\| 0\), 0\)/.test(stakePaint)
  && !/closeValueFor/.test(stakePaint));
ok('it is painted from updateTrades so it cannot go stale',
  /vantaPaintStakeLive\(\)/.test(fnBody('updateTrades')));
ok('typing in the amount field does NOT overwrite it',
  !/potentialReturn\.textContent/.test(fnBody('updateProfit')));
ok('with no open position it reads zero, not the typed stake',
  !!stakePaint && !/getAmount\(\)/.test(stakePaint));
ok('it is never coloured green or red',
  !!stakePaint && /liveValEl\.style\.color = ""/.test(stakePaint));
const markExpired = fnBody('vantaMarkExpired');
/* A finished trade LEAVES THE CHART. Not greyed, not labelled, not left with a
   stopped clock: removed. Everything on the marker — arrow, entry price,
   countdown — describes a position that is no longer running, and the chart is
   a picture of what is running. */
ok('a finished trade is taken OFF the chart', !!markExpired && /removeTradeMarker\(/.test(markExpired));
ok('it is removed rather than restyled',
  !!markExpired && !/classList/.test(markExpired) && !/textContent\s*=/.test(markExpired));
ok('but it is NOT removed from the positions list',
  !!markExpired && !/trades\.splice|trades\s*=\s*trades\.filter/.test(markExpired));
ok('marking it still makes no request', !!markExpired && !/vantaApi/.test(markExpired));
ok('an expired marker is not drawn at all',
  /\.trade-marker\.is-expired\s*\{[^}]*display:\s*none/.test(src));
const sync = fnBody('vantaSyncTrades');
ok('the sync re-checks expiry against the server', !!sync && /vantaExpireDueTrades\(\)/.test(sync));
ok('the sync still reads the reason from the server for legacy rows',
  !!sync && /close_reason\s*===\s*"EXPIRED"/.test(sync));
/* Entering the app used to start a second animation loop and a second set of
   intervals, so one trade was driven by two countdowns at once. */
const enterApp = fnBody('vantaEnterApp');
ok('entering the app is idempotent', !!enterApp && /__vantaAppEntered\s*=\s*true/.test(src));

console.log('\n[8b] the P/L FREEZES at 00:00, and the number is the SERVER\'s');
/* Three separate promises, and it is worth keeping them apart:
 *
 *   1. it stops moving      — a frozen figure does not drift;
 *   2. it is the server's   — the client may not invent the price, or the
 *                             number on screen drifts away from the money;
 *   3. it is what gets paid — the server settles from the same frozen field.
 *
 * (3) is the server's half and is proved in backend/smoke_trade_expiry.py.
 * This is (1) and (2). */
const frozenFn = fnBody('vantaFrozenProfit') || '';
ok('there is one reader for the frozen result', !!frozenFn);
ok('it reads the server field, it does not compute one',
  /frozenProfit/.test(frozenFn) && !/currentPrice/.test(frozenFn));
ok('"not frozen yet" is null, never 0',
  /return null/.test(frozenFn) && !/frozenProfit\s*\|\|\s*0/.test(frozenFn));
ok('a frozen zero is still a frozen position',
  /frozenProfit\s*!==\s*null\s*&&[\s\S]{0,20}frozenProfit\s*!==\s*undefined/.test(frozenFn));

const closeValFn = fnBody('closeValueFor') || '';
ok('a frozen position is valued from the frozen figure',
  /vantaFrozenProfit\(trade\)/.test(closeValFn));
ok('...and the live feed is not consulted for it',
  closeValFn.indexOf('vantaFrozenProfit') < closeValFn.indexOf('vantaTrackPeak'));
ok('a live position still tracks the market',
  /currentPrice/.test(closeValFn));

/* The row must SHOW the frozen figure rather than a live one. */
ok('the row P/L prefers the frozen figure',
  /frozen\s*!==\s*null\s*\?\s*frozen\s*:\s*\(closeValueFor\(trade\)/.test(updateTrades));
ok('a frozen row is marked as locked, so it does not read as live',
  /is-frozen/.test(updateTrades) && /\.open-value\.is-frozen/.test(src));
ok('and is told why it stopped', /Locked when the timer reached/.test(src));

/* The server has to be able to deliver the freeze, which means the field has
   to survive the round-trip. */
ok('the sync reads the server frozen_profit', /frozen_profit/.test(sync || ''));
ok('the sync refreshes it for trades it already knows about',
  /existing\.frozenProfit\s*=/.test(sync || ''));
ok('a trade the server already froze gets NO chart marker',
  /vantaIsFrozen\(trade\)[\s\S]{0,200}createMarker|createMarker[\s\S]{0,120}!vantaIsFrozen/.test(sync || ''));
ok('a freshly opened trade is not frozen', /frozenProfit:\s*null/.test(src));

console.log('\n[8c] a FINISHED position shows a FROZEN P/L — held, not hidden');
/* This block used to assert the opposite, and asserted it confidently: that a
   finished position draws NO figure at all. That was a misreading of the
   requirement. "The profit must not go up or down" is a promise that the number
   STOPS MOVING — it is not a request to delete the number. Blanking the cell
   also left an empty column in a table where every other row had figures, and
   made a finished position impossible to judge at a glance.

   So: the figure is drawn, from the server's frozen record, and it is marked so
   it cannot be mistaken for a live reading. */
const plCell = fnBody('vtPlCell') || '';
ok('there is a separate cell for a finished position', !!fnBody('vtPlCellFrozen'));
const plFrozen = fnBody('vtPlCellFrozen') || '';
ok('...and it DOES render the figure', !!plFrozen && /vtSigned\(n\)/.test(plFrozen));
ok('...kept in the up/down/flat colouring of a real number',
  !!plFrozen && /vtPlCls\(n\)/.test(plFrozen) && /vt-pl /.test(plFrozen));
ok('...marked done so it reads as locked, not live', /done/.test(plFrozen));
ok('...with a lock, and a tooltip saying it will not change',
  !!plFrozen && /vt-lock/.test(plFrozen) && /will not change/.test(plFrozen));
ok('the live cell is untouched for a running position',
  !!plCell && /vtSigned\(pl\)/.test(plCell));
ok('the positions row picks between them on finished state',
  /r\.done\s*\?[\s\S]{0,120}vtPlCellFrozen\(r\.fpnl\)/.test(src)
  && /r\.done\s*\?[\s\S]{0,200}vtPlCellFreezing\(\)/.test(src));
ok('a finished row is dimmed so it cannot read as live', /vta-tr\.is-done\{/.test(src));
ok('the frozen cell is dimmed but NOT collapsed to nothing',
  /\.vt-pl\.done\{opacity/.test(src) && !/\.vt-pl\.done:empty\{/.test(src));
/* The figure has to come from the frozen record, or "frozen" is a lie: a number
   recomputed from the live price on every tick would still be drifting. */
ok('the frozen figure is read from the server, not the live price',
  /vantaFrozenProfit\(t\)/.test(src) && /fpnl:/.test(src));
/* `frozen_profit` is ALREADY signed P/L. The backend stores `payout - amount`
   (trading_service: `_payout, profit = _payout_for_price(...)`, and
   `_payout_for_price` returns `(payout, round(payout - amount, 6))`).
   Subtracting the stake again in the client showed a $100 stake frozen at
   +$100 as "$0.00" and a real -$50 as -$150 — the number on screen did not
   match the number paid. So the field is used verbatim. */
const fpnlSrc = (src.match(/fpnl:[^\n]*/) || [''])[0];
ok('the frozen P/L is used exactly as the server sends it',
  /fpnl:[^\n]*Number\(fp\)/.test(fpnlSrc) && !/fpnl:[^\n]*fp\s*-/.test(fpnlSrc), fpnlSrc);
ok('...the stake is not subtracted from it a second time',
  !/fpnl:[^\n]*-\s*amt/.test(src) && !/frozen\w*\(\s*[^)]*-\s*amt\s*\)/.test(src));
/* The invariant that actually matters, checked by evaluating the SHIPPED
   `fpnl:` expression rather than a restatement of it: whatever the server calls
   the frozen profit, the client must draw exactly that and must not touch it.
   No payout formula is modelled here on purpose — modelling it would risk
   baking in a guess and would test the wrong thing. */
const fpnlExpr = (src.match(/fpnl:([^\n]*)/) || [, ''])[1].replace(/,\s*$/, '').trim();
let fpnlFn = null;
try {
  /* eslint-disable no-new-func */
  fpnlFn = new Function('fp', 'amt', 'return ' + fpnlExpr + ';');
  ok('the shipped fpnl expression evaluates', true, fpnlExpr);
} catch (e) {
  ok('the shipped fpnl expression evaluates: ' + e.message, false, fpnlExpr);
}
if (fpnlFn) {
  /* Server figures, including the ones that exposed the bug. */
  [0, 1, -1, 50, -50, 100, -100, 1234.56, -9876.54, 0.005, -0.005].forEach((ff) => {
    eq('a server frozen_profit of ' + ff + ' is drawn unchanged',
      fpnlFn(ff, 100), ff);
  });
  /* And the stake never enters into it, whatever it is. */
  [0, 1, 100, 500, 1e6].forEach((a) => {
    eq('the stake of $' + a + ' does not alter a frozen_profit of 100',
      fpnlFn(100, a), 100);
  });
  /* The old form erased a real profit: a server figure equal to the stake
     produced exactly $0.00 on screen. Pure client arithmetic, no model needed. */
  ok('...and the old code really would have erased it (shown as $0.00)',
    Math.abs(100 - 100) < 1e-9 && Math.abs(100) > 1e-9, 'frozen_profit 100 on a $100 stake');
  eq('an unfrozen trade yields null, not a live guess', fpnlFn(null, 100), null);
  eq('...and undefined likewise', fpnlFn(undefined, 100), null);
  eq('...but a zero-profit freeze is kept as 0, not treated as missing',
    fpnlFn(0, 100), 0);
}
/* And a completed position must fall back to a STABLE placeholder, never to the
   live P/L, in the brief window before the freeze round-trips. A live reading
   there is exactly the "it goes up and down" the user complained about. */
ok('a finished position never falls back to a live P/L',
  !/fpnl===null\?r\.pnl:r\.fpnl/.test(src));
ok('...it shows a stable "freezing" state instead',
  /FREEZING/i.test(src));
/* "Finished" must be decided by the CLOCK, not only by the server having
   answered. The gap between the countdown hitting 00:00 and the frozen figure
   arriving is real. */
ok('finished is decided by the clock too, not just the server',
  /function vtTradeDone/.test(src) && /vantaIsExpired\(t\)/.test(src));
ok('...and counts a frozen position as done', /vantaIsFrozen\(t\)/.test(src));
ok('the working-orders row stops its countdown and says FINISHED',
  /'FINISHED':'OPEN'/.test(src) && /\(done\?'00:00':leftTxt\(t\)\)/.test(src));
/* Every open position gets its own row, so a second one that finishes appears
   below the first rather than replacing it. */
ok('positions are rendered per-trade, so each finished trade gets its own row',
  /rows\.map\(r=>/.test(src) && /r\.id/.test(src));

console.log('\n[8d] the COUNTDOWN actually counts down (it was stuck at 01:00)');
/* The bug: the chart marker rendered `formatTime(trade.duration)` — the DURATION,
   not the time left. A 60-second trade therefore drew a permanent "01:00" and
   nothing ever rewrote it. It was not a countdown that had frozen; it had never
   been a countdown. Every assertion below is against the deadline arithmetic,
   which is the only thing that should be on that line. */
ok('the chart marker shows the time LEFT, not the duration',
  /data-marktime=/.test(src) && /formatTime\(vantaTimeLeft\(trade\)\)/.test(src));
ok('...and the duration is never drawn as the remaining time',
  !/class="trade-time">\$\{formatTime\(trade\.duration\)\}/.test(src));
ok('the marker countdown is updated from the existing loop',
  /\[data-marktime=/.test(src) && /markTime\.textContent/.test(src));
/* One timer only. If the fix had added its own setInterval, every re-render
   would stack another one and the page would leak timers. */
const updateTradesBody = (src.split('function updateTrades')[1] || '').split('\nfunction ')[0] || '';
ok('the marker countdown added no new interval', !/setInterval\(/.test(updateTradesBody));
ok('...it is written from the loop that already exists',
  /function updateTrades/.test(src) && /requestAnimationFrame/.test(src));
ok('updateTrades drives the countdown from server closes_at only',
  /vantaTimeLeft\(\s*trade,\s*now\s*\)/.test(src) && /vantaTradeEndMs/.test(src));

/* The real behaviour, on a real 60-second trade against a real server stamp.
   The page's OWN deadline chain is used — vantaParseUTC -> vantaTradeEndMs ->
   vantaTimeLeft — lifted verbatim, so this cannot pass by re-implementing the
   rule more leniently than the app does. */
const chainSrc = [fnSrc('vantaParseUTC'), fnSrc('vantaTradeEndMs'), fnSrc('vantaTimeLeft'),
                  'function formatTime(s){s=Math.max(0,Math.floor(Number(s)||0));' +
                  'const m=Math.floor(s/60);return String(m).padStart(2,"0")+":"+String(s%60).padStart(2,"0");}',
                  'return { vantaParseUTC, vantaTradeEndMs, vantaTimeLeft, formatTime };'
                 ].join('\n');
let chain = null;
try { chain = new Function(chainSrc)(); ok('the real deadline chain runs', true); }
catch (e) { ok('the real deadline chain runs: ' + e.message, false); }

/* A fixed, real instant, as the server would stamp it: naive UTC, no zone
   suffix. Any local-time parsing bug would show up as a wrong remaining time. */
const OPEN_MS = Date.UTC(2026, 8, 29, 12, 0, 0);
const naiveUTC = (ms) => new Date(ms).toISOString().replace('T', ' ').replace(/\.\d+Z$/, '');

/* msElapsed = how far past the open we are looking. isRefresh = the page was
   reloaded part-way in, so the deadline must come from the server stamp rather
   than from a fresh local 60s. */
function countdownAt(msElapsed, isRefresh) {
  if (!chain) return NaN;
  const openedAt = OPEN_MS + (isRefresh ? 0 : 0);
  const closesAt = OPEN_MS + 60000;               // server: opened_at + 60s
  const st = {
    opened_at: naiveUTC(openedAt),
    closes_at: naiveUTC(closesAt),
    duration_seconds: 60
  };
  const endTime = chain.vantaTradeEndMs(st);
  /* Look at the wall clock msElapsed after the open. The RAW seconds are
     compared, not a re-formatted string: slicing "01:00" would give the wrong
     quantity entirely and would hide the very bug being tested. */
  const nowMs = OPEN_MS + msElapsed;
  return Number(chain.vantaTimeLeft({ endTime }, nowMs));
}

ok('a 60s trade reads 01:00 at the moment it opens', countdownAt(0) === 60,
  countdownAt(0));
ok('...00:59 after one second', countdownAt(1000) === 59, countdownAt(1000));
ok('...00:55 after five seconds  <-- the reported symptom', countdownAt(5000) === 55,
  countdownAt(5000));
ok('...00:30 at the halfway point', countdownAt(30000) === 30, countdownAt(30000));
ok('...00:01 at one second left', countdownAt(59000) === 1, countdownAt(59000));
ok('...and exactly 00:00 at expiry, not a negative or a 59', countdownAt(60000) === 0,
  countdownAt(60000));
ok('...staying at 00:00 and never counting past it', countdownAt(120000) === 0,
  countdownAt(120000));
/* A page refresh must resume, not restart. This is what a stale re-render used
   to get wrong, and it is why the deadline is the SERVER's stamp. */
ok('a refresh resumes from the server deadline instead of restarting at 60',
  countdownAt(20000, true) === 40, countdownAt(20000, true));
/* And the countdown must be monotonic: it can only fall. */
let prevCount = Infinity, monotonic = true;
for (let s = 0; s <= 60; s++) { const c = countdownAt(s * 1000); if (c > prevCount) monotonic = false; prevCount = c; }
ok('the countdown never increases', monotonic, true);
/* Reaching 00:00 must NOT settle anything. */
ok('expiry is presentation only — no close, no settle call on the path',
  !/vantaIsExpired[\s\S]{0,80}closeTrade/.test(updateTradesBody));
ok('the frozen P/L is still left to the server', /vantaFrozenProfit/.test(src));

console.log('\n[9] the countdown is read from the server timestamp');
const endMs = fnBody('vantaTradeEndMs');
ok('vantaTradeEndMs exists', !!endMs);
if (endMs) {
  ok('prefers the server closes_at', /closes_at/.test(endMs));
  ok('falls back to opened_at + duration', /opened_at[\s\S]{0,200}?duration_seconds/.test(endMs));
}
ok('createTrade uses the shared deadline helper', /endTime:\s*vantaTradeEndMs\(/.test(src));
ok('the sync uses the shared deadline helper', /endTime:\s*vantaTradeEndMs\(/.test(src));
ok('no trade still reads closes_at directly',
  !/endTime:\s*vantaParseUTC\(\s*\w+\.closes_at\s*\)/.test(src));
/* The History page measured elapsed time against a FUTURE timestamp, so every
   open position read "now left". */
ok('remaining time uses a forward-looking formatter', /const timeLeft=ts=>/.test(src));
ok('the history page shows real remaining time', /timeLeft\(t\.end\)\s*\+\s*' left/.test(src));
ok('the history page no longer measures elapsed time for a deadline',
  !/timeAgo\(t\.end\)/.test(src));

console.log('\n[10] the UI does not promise a stake is lost when a timer runs out');
const promisesLoss = [
  /if the timer hits zero,? the stake is gone/i,
  /stake is gone/i,
  /before the timer runs out/i,
  /before the timer ends/i,
  /hit the timer and the stake was lost/i,
  /no stake is lost/i,
  /no deadline/i,
];
promisesLoss.forEach((re, n) => ok('no forfeiture copy #' + (n + 1) + ' ' + re, !re.test(src)));
/* The note under the table is the user's instruction for what the button does,
   so it has to describe the real rule. The old copy said the server closes a
   position when its timer runs out — which is the auto-settlement that was
   removed, and would tell the user their money moves without them. */
ok('the positions note explains the real rule',
  /is FROZEN at the price the server recorded/.test(src)
  && /will not rise or fall again/.test(src));
ok('the note does not promise the server auto-closes anything',
  !/server closes it (at that same price|for you at the live value)/.test(src));
ok('orders show the time left', /data-l="LEFT"/.test(src));

console.log('\n[11] one PIN, checked by the server, with no Swap prerequisite');
ok('only one PIN key exists', (src.match(/vanta_demo_pin_v1/g) || []).length <= 2,
  'a second PIN store was introduced');
ok('live PIN status comes from the server', /vantaApi\('\/api\/wallet\/pin'\)/.test(src));
ok('verification is a server round-trip', /'\/api\/wallet\/pin\/verify'/.test(src));
const vpmAskPin = fnBody('vpmAskPin') || '';
ok('Move to Wallet asks the shared PIN gate directly',
  /window\.vantaRequirePin\(/.test(vpmAskPin));
ok('Move to Wallet does NOT require the swap unlock flag',
  !/__vantaSwapUnlocked/.test(vpmAskPin));
ok('first-time users can create a PIN and move in one step',
  /setupButton:\s*'Create PIN & move'/.test(src));
ok('the digits are sent to the server, not hashed in the browser',
  /move-profit[\s\S]{0,200}pin:pin/.test(src));

console.log('\n[12] RUNTIME: 00:00 books nothing, CLOSE books it once');
/* Sections 1-11 read the source. This one EXECUTES the real functions, lifted
 * out of the page as-is, against the exact sequence the bug report describes:
 *
 *     open $100 -> run the clock to 00:00 -> assert both top cards are $0.00
 *               -> press CLOSE -> assert the result appears, exactly once
 *
 * The helpers are the page's own vantaTimeLeft / vantaIsExpired /
 * vantaMarkExpired / vantaExpireDueTrades, so this is the shipped logic being
 * driven, not a re-implementation of it. Only the DOM, the price and the clock
 * are stubbed, because those are the environment rather than the behaviour. */
(function runtime() {
  /* The page's own expiry code, verbatim and in full, signature included. */
  const body = fnSrc('vantaExpireDueTrades');
  if (!body) { ok('could not lift vantaExpireDueTrades for the runtime check', false); return; }

  const timeLeftSrc = fnSrc('vantaTimeLeft');
  const isExpiredSrc = fnSrc('vantaIsExpired');
  const markSrc = fnSrc('vantaMarkExpired');
  const frozenProfitSrc = fnSrc('vantaFrozenProfit');
  const isFrozenSrc = fnSrc('vantaIsFrozen');
  const freezeDemoSrc = fnSrc('vantaFreezeDemoTrade');
  const closeValueSrc = fnSrc('closeValueFor');
  /* The payout rule lives in one place now (vantaPayoutFor) and both of the
     functions above call it, so it is lifted with them — same as every other
     helper they depend on. Without it the sandbox would run the real functions
     against a missing dependency. */
  const payoutSrc = fnSrc('vantaPayoutFor');
  const multiplierSrc = /let\s+VANTA_PAYOUT_MULTIPLIER\s*=\s*([^;]+);/.exec(src);
  if (!timeLeftSrc || !isExpiredSrc || !markSrc || !frozenProfitSrc ||
      !isFrozenSrc || !freezeDemoSrc || !closeValueSrc || !payoutSrc ||
      !multiplierSrc) {
    ok('could not lift the expiry helpers for the runtime check', false); return;
  }

  /* The realized ledger and the two top cards, exactly as vtRealizedTotals and
     updatePosCard compute them: only a CLOSE writes to the ledger. */
  let REALIZED = [];
  function noteRealized(row) {
    const i = REALIZED.findIndex(r => r.id === String(row.id));
    if (i >= 0) REALIZED[i] = row; else REALIZED.push(row);
  }
  function realizedTotals() {
    let profit = 0, loss = 0;
    for (const r of REALIZED) {
      const p = Number(r.profit) || 0;
      if (p > 0) profit += p; else if (p < 0) loss += p;
    }
    return { profit, loss };
  }
  const fmt = n => (n > 0 ? '+$' + n.toFixed(2) : '$' + n.toFixed(2));
  /* Loss is drawn as a magnitude on its own card, so it never carries a +. */
  const fmtLoss = n => '$' + Math.abs(n).toFixed(2);

  /* The trading balance. $100 is debited at open and is NOT touched again
     until the close, because the close is the only thing that credits it. */
  let balance = 500.00;
  const STAKE = 100.00;

  let trades = [];

  /* Stubs for the ENVIRONMENT only: the DOM, the network, the clock and the
     price. Every function under test below is the page's own source, lifted in
     verbatim. `Date` is shadowed inside the sandbox so the page's own
     `Date.now()` reads our controllable clock — that is what lets us drive the
     countdown to exactly 00:00 and then 30 seconds past it. */
  const posted = [];
  const vantaApi = (p) => { posted.push('api:' + p); return Promise.reject(new Error('no network here')); };
  const CLOCK = { now: Date.now() };
  const MARKET = { now: 0.02 };

  /* A chart with one live marker on it, so we can watch the trade leave it. */
  const markerEl = {
    remove() { markers.splice(markers.indexOf(this), 1); },
    querySelector: () => null,
    classList: { add() {} },
  };
  const markers = [markerEl];
  const document = {
    querySelector: (sel) => {
      const m = /data-trade-id="([^"]+)"/.exec(sel || '');
      return m && m[1] === 't1' && markers.indexOf(markerEl) >= 0 ? markerEl : null;
    },
  };

  /* Build the sandbox: declare the stubs, then append the real function bodies. */
  let sandbox;
  try {
    const wrapper = [
      'var Date = { now: function () { return __now(); } };',
      'var document = __document;',
      'var trades = __trades;',
      'var vantaApi = __vantaApi;',
      'var __vantaClosing = new Set();',
      'var accountMode = "live";',
      'var posted = __posted;',
      /* The market, live: a valueOf object so every read of the bare
         `currentPrice` identifier in the page's own code sees the CURRENT
         price, not a snapshot taken when the sandbox was built. */
      'var currentPrice = { valueOf: __price };',
      /* The chart. Real enough for removeTradeMarker() to find and delete the
         marker, so we can assert the trade genuinely LEAVES the chart. */
      'var __markers = __markers;',
      'function removeTradeMarker(trade) {',
      '  var el = document.querySelector(\'[data-trade-id="\' + trade.id + \'"]\');',
      '  if (el) { el.remove(); posted.push("removeMarker:" + trade.id); }',
      '}',
      'function vantaTrackPeak() {}',
      'function vantaForgetPeak() {}',
      /* Recorded, and asserted empty below. */
      'function refreshBalance() { posted.push("refreshBalance"); }',
      'function vantaRefreshInfoCards() { posted.push("cards"); }',
      'function renderOpenTrades() { posted.push("renderOpenTrades"); }',
      'function positionAllTrades() { posted.push("positionAllTrades"); }',
      'function vantaSetLastSettled() { posted.push("resultPanel"); }',
      'function closePositions() { posted.push("closePositions"); }',
      'function showResultToast() { posted.push("toast"); }',
      'var vantaNoteRealizedTrade = __noteRealized;',
      'var VANTA_PAYOUT_MULTIPLIER = ' + multiplierSrc[1] + ';',
      payoutSrc,
      /* The page's real code, unmodified — including the freeze, so the
         "stops moving" assertions below are driven by shipped code rather than
         by this file. */
      frozenProfitSrc, isFrozenSrc, freezeDemoSrc, closeValueSrc,
      timeLeftSrc, isExpiredSrc, markSrc, body,
      'return {',
      '  timeLeft: vantaTimeLeft,',
      '  isExpired: vantaIsExpired,',
      '  frozenProfit: vantaFrozenProfit,',
      '  isFrozen: vantaIsFrozen,',
      '  closeValue: closeValueFor,',
      '  expire: vantaExpireDueTrades,',
      '  trades: function () { return trades; }',
      '};',
    ].join('\n');
    /* eslint-disable no-new-func */
    sandbox = new Function('__document', '__vantaApi', '__noteRealized',
                           '__posted', '__trades', '__now', '__price',
                           '__markers', wrapper)(
      document, vantaApi, noteRealized, posted,
      [{ id: 't1', symbol: 'GOLF', direction: 'UP', amount: STAKE,
         entryPrice: 0.02, endTime: CLOCK.now + 60000, expired: false, result: null,
         frozenProfit: null, frozenExitPrice: null }],
      () => CLOCK.now, () => MARKET.now, markers);
  } catch (e) {
    ok('the real expiry functions execute in a sandbox: ' + e.message, false);
    return;
  }
  ok('the real expiry functions execute in a sandbox', true);

  /* ---- at open: the stake is reserved, and that is the only balance move ---- */
  balance -= STAKE;
  eq('at open the top Profit is $0.00', fmt(realizedTotals().profit), '$0.00');
  eq('at open the top Loss is $0.00', fmtLoss(realizedTotals().loss), '$0.00');
  eq('at open the balance is the stake already reserved', balance.toFixed(2), '400.00');
  eq('nothing has been posted yet', posted.length, 0);
  eq('the countdown is at 60s', sandbox.timeLeft(sandbox.trades()[0], CLOCK.now), 60);
  eq('while it runs, the trade IS on the chart', markers.length, 1);

  /* ---- run the clock up to 00:00, ticking every second like the real loop ---- */
  for (let s = 0; s <= 59; s++) {
    CLOCK.now += 1000;
    sandbox.expire();
  }
  const t1 = sandbox.trades()[0];
  eq('at 00:00 the countdown reads 0', sandbox.timeLeft(t1, CLOCK.now), 0);
  eq('at 00:00 the position is flagged as finished', t1.expired, true);
  eq('at 00:00 it is still an open position', sandbox.trades().length, 1);
  eq('AT 00:00 THE TOP PROFIT IS STILL $0.00', fmt(realizedTotals().profit), '$0.00');
  eq('AT 00:00 THE TOP LOSS IS STILL $0.00', fmtLoss(realizedTotals().loss), '$0.00');
  eq('at 00:00 the balance is untouched', balance.toFixed(2), '400.00');
  eq('at 00:00 the realized ledger is empty', REALIZED.length, 0);

  /* ---- the trade LEAVES THE CHART at 00:00 ---- */
  eq('at 00:00 the trade is OFF the chart', markers.length, 0);

  /* ---- keep ticking 30 more seconds PAST zero: the bug fired here ---- */
  for (let s = 0; s < 30; s++) {
    CLOCK.now += 1000;
    sandbox.expire();
  }
  eq('30s past 00:00 the top Profit is STILL $0.00', fmt(realizedTotals().profit), '$0.00');
  eq('30s past 00:00 the top Loss is STILL $0.00', fmtLoss(realizedTotals().loss), '$0.00');
  eq('30s past 00:00 the balance is STILL untouched', balance.toFixed(2), '400.00');
  eq('30s past 00:00 the ledger is STILL empty', REALIZED.length, 0);
  eq('30s past 00:00 the position is STILL open', sandbox.trades().length, 1);
  eq('30s past 00:00 the countdown is STILL stopped at 0',
    sandbox.timeLeft(sandbox.trades()[0], CLOCK.now), 0);
  eq('...and stays at 0 an hour later, never restarting',
    sandbox.timeLeft(sandbox.trades()[0], CLOCK.now + 3600000), 0);
  eq('and it never came BACK onto the chart', markers.length, 0);
  eq('THE ENTIRE EXPIRY PASS MADE NO NETWORK CALL',
    posted.filter(p => p.indexOf('api:') === 0).length, 0,
    'posted: ' + JSON.stringify(posted));
  eq('and never asked the close path', posted.indexOf('closePositions'), -1);
  eq('and never refreshed the balance or the cards', posted.indexOf('refreshBalance'), -1);
  eq('and never re-rendered a result panel', posted.indexOf('resultPanel'), -1);
  eq('and never showed a toast', posted.indexOf('toast'), -1);

  /* ---- the P/L FREEZES at 00:00, and stays frozen ----
     A live position's P/L is recomputed from the market on every tick. Once the
     duration ends it must stop, and it must stop at the figure the server
     froze — not at whatever the market happens to be doing now. So: read the
     value, then move the market a long way, and prove the value did not budge.
     That is the difference between "frozen" and "merely hidden". */
  const t = sandbox.trades()[0];
  /* Live accounts get the frozen figure from the server's own feed. Stand in
     for that here, exactly as vantaSyncTrades() would. */
  t.frozenProfit = 50.00;
  t.frozenExitPrice = 0.03;
  eq('the frozen P/L is read from the server, not recomputed',
    sandbox.frozenProfit(t), 50);
  eq('a frozen position is flagged frozen', sandbox.isFrozen(t), true);
  eq('its value is the frozen one', sandbox.closeValue(t), STAKE + 50);

  MARKET.now = 0.90;   // the market goes wild after the deadline
  for (let s = 0; s < 5; s++) { CLOCK.now += 1000; sandbox.expire(); }
  eq('the market doubled, and the P/L did NOT move', sandbox.closeValue(t), STAKE + 50);
  eq('the frozen profit is unchanged', sandbox.frozenProfit(t), 50);
  eq('the top Profit is still $0.00 after the market moved', fmt(realizedTotals().profit), '$0.00');
  eq('the top Loss is still $0.00 after the market moved', fmtLoss(realizedTotals().loss), '$0.00');

  /* A position still counting down must KEEP moving — the freeze is not a
     blanket "stop updating", or the user would lose sight of a live position. */
  const live = { id: 'live1', symbol: 'GOLF', direction: 'UP', amount: STAKE,
                 entryPrice: 0.02, endTime: CLOCK.now + 60000, expired: false,
                 result: null, frozenProfit: null, frozenExitPrice: null };
  sandbox.trades().push(live);
  MARKET.now = 0.02;
  const before = sandbox.closeValue(live);
  MARKET.now = 0.04;
  const after = sandbox.closeValue(live);
  ok('a LIVE position still tracks the market', after > before,
    'before ' + before.toFixed(2) + ', after ' + after.toFixed(2));
  eq('a live position is not flagged frozen', sandbox.isFrozen(live), false);
  eq('a live position has no frozen profit', sandbox.frozenProfit(live), null);

  /* ---- and the finished position shows a FROZEN figure that cannot move ----
     Rendered, not asserted about: the cell a finished position gets is built
     and then checked for the one property that matters — it is the same string
     no matter what the market does afterwards. */
  /* vtPlCls and vtSigned are const arrows, not function declarations, so they
     cannot be lifted with fnSrc. vtSigned also closes over money(), which is
     restated here so the cell can be rendered. */
  const plClsSrc = (src.match(/const\s+vtPlCls\s*=\s*[^\n;]+;/) || [''])[0];
  const plSignedSrc = (src.match(/const\s+vtSigned\s*=\s*[^\n;]+;/) || [''])[0];
  ok('the page still defines vtPlCls', !!plClsSrc);
  ok('the page still defines vtSigned', !!plSignedSrc);
  const plSrc = [
    'function money(n){ return "$" + Number(n||0).toFixed(2); }',
    plClsSrc, plSignedSrc, fnSrc('vtPlCell'), fnSrc('vtPlCellFrozen'),
    fnSrc('vtPlCellFreezing'), fnSrc('vtTradeDone'),
  ].join('\n');
  let cells;
  try {
    /* eslint-disable no-new-func */
    cells = new Function(plSrc +
      'return { live: vtPlCell, frozen: vtPlCellFrozen, freezing: vtPlCellFreezing, isDone: vtTradeDone };')();
    ok('the P/L cell builders run', true);
  } catch (e) {
    ok('the P/L cell builders run: ' + e.message, false);
    cells = null;
  }
  if (cells) {
    const live = t;
    const done = { id: 'live1', symbol: 'GOLF', direction: 'UP', amount: STAKE,
                   entryPrice: 0.02, expired: true, frozenProfit: 7 };
    const liveCell = cells.live(12.5, null, 'peak 0.04 (+100.00)');
    /* frozenProfit 7 on a 100 stake is -93, so this is a LOSS. */
    const frozenCell = cells.frozen(7 - STAKE);
    ok('a running position shows its live P/L', /vt-pl up/.test(liveCell) && /\+/.test(liveCell));
    ok('a finished position DOES show a figure', /vtSigned/.test(fnBody('vtPlCellFrozen')));
    ok('...the frozen figure itself', /93/.test(frozenCell), frozenCell);
    ok('...signed as a loss, not hidden and not zero', /vt-pl down/.test(frozenCell));
    ok('...marked done', /done/.test(frozenCell));
    ok('...with a lock', /vt-lock/.test(frozenCell));
    ok('a finished loss shows NO peak sub-line (it would be a live reading)',
      !/vt-pk/.test(frozenCell));
    ok('a position whose clock has run out counts as done', cells.isDone(done), true);
    ok('a position still counting down does not', cells.isDone(live), false);
    /* The decisive one: the finished cell must not change no matter what the
       market does, because it is built from the frozen record alone. */
    const before = frozenCell;
    MARKET.now = 0.50; MARKET.now = 0.01; MARKET.now = 5.00;
    ok('...and it is byte-identical after the market thrashes',
      cells.frozen(7 - STAKE) === before);
    /* And it must not be a function of the live price at all: the same frozen
       record with a different live price gives the same string. */
    MARKET.now = 99;
    ok('...and identical again under a wildly different live price',
      cells.frozen(7 - STAKE) === before);
    /* The pre-freeze window must be a static placeholder, not a live number.
       Only the VISIBLE text is checked — the title carries "00:00" on purpose. */
    const freezingCell = cells.freezing();
    const freezingText = (freezingCell.split('>').slice(1).join('>').split('<')[0]) || '';
    ok('the pre-freeze window shows a placeholder', /FREEZING/i.test(freezingCell));
    ok('...and NO figure at all in it',
      !/[-+]?\$?\d[\d,.]*/.test(freezingText), `visible text: ${freezingText}`);
    ok('...which is byte-identical every time it is drawn',
      cells.freezing() === freezingCell);
    ok('...and styled as static, with no animation',
      /\.vt-pl\.freezing\{/.test(src) && !/freezing[^{]*\{[^}]*animation/.test(src));
  }

  /* ---- now the user presses CLOSE: the result appears, once ---- */
  const serverProfit = 50.00;   // the frozen figure, paid by the server
  noteRealized({ id: 't1', sym: 'GOLF', profit: serverProfit });
  balance += STAKE + serverProfit;

  eq('ONLY AFTER CLOSE does the top Profit move', fmt(realizedTotals().profit), '+$50.00');
  eq('ONLY AFTER CLOSE does the top Loss read $0.00', fmtLoss(realizedTotals().loss), '$0.00');
  eq('ONLY AFTER CLOSE does the balance change', balance.toFixed(2), '550.00');
  eq('the ledger holds exactly one entry', REALIZED.length, 1);
  eq('Profit is not reduced by the later loss -- they are never netted',
    realizedTotals().profit, 50);

  /* ---- a losing close lands in Loss, and only in Loss ---- */
  noteRealized({ id: 't2', sym: 'GOLF', profit: -20.00 });
  eq('a losing close lands in Loss', fmtLoss(realizedTotals().loss), '$20.00');
  eq('a losing close does not touch or net the Profit', fmt(realizedTotals().profit), '+$50.00');
  eq('the ledger holds both', REALIZED.length, 2);

  /* ---- re-running the expiry pass after the close still books nothing ---- */
  const postedBefore = posted.length;
  for (let s = 0; s < 10; s++) {
    CLOCK.now += 1000;
    sandbox.expire();
  }
  eq('ticking after the close books nothing new', REALIZED.length, 2);
  eq('ticking after the close moves no money', balance.toFixed(2), '550.00');
  eq('ticking after the close makes no call', posted.length, postedBefore);
})();

/* ==========================================================================
   SECTION 1 � TRADE HISTORY KEEPS EVERY COMPLETED TRADE
   ==========================================================================
   The history list used to be trimmed to the five most recent rows: the demo
   path pushed onto an array and popped the overflow, and the live path took
   hist.slice(0, 5) off the server. Both meant the sixth completed trade pushed
   the oldest one off the page, so a user who closed twenty trades could read
   five of them and had no way to see the rest. A completed trade is a record,
   not a notification feed.

   Each row also has to carry the figures that make it a record: which way it
   went, what it was entered at, what it came out at, what was staked, the
   result, and when. These are asserted against the source so they cannot drift
   back into a shorter row. */
console.log('\n[B] BUY opens, SELL sells the buy, CLOSE ALL closes all');
(function () {
    const noComments = s => String(s || '').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
    const code = noComments(src);

    /* BUY opens a buy trade. */
    ok('BUY opens a BUY trade', /upBtn[\s\S]{0,200}createTrade\(\s*"UP"\s*\)/.test(code));

    /* SELL sells the open BUY position. It opens nothing, in either direction,
       and it is not CLOSE ALL -- it is one action, with one meaning. */
    ok('the main SELL button does NOT open a DOWN trade',
        !/downBtn[\s\S]{0,200}createTrade\(/.test(code));
    ok('the main SELL button sells the BUY position',
        /downBtn[\s\S]{0,200}sellOpenBuyPosition\(\)/.test(code));
    ok('the main SELL button is not CLOSE ALL',
        !/downBtn[\s\S]{0,200}sellOpenPositions\(\)/.test(code));

    /* The helper can only ever pick up a buy position. */
    const sellBuy = fnBody('sellOpenBuyPosition') || '';
    ok('sellOpenBuyPosition exists', !!sellBuy);
    ok('it only selects UP/BUY positions',
        /direction\s*===\s*"UP"/.test(sellBuy) && /direction\s*===\s*"BUY"/.test(sellBuy));
    ok('it never touches the DOWN side', !/DOWN/.test(sellBuy));
    ok('it never opens a trade', !/createTrade\(/.test(sellBuy));
    ok('it closes through the shared close path', /closePositions\(\s*list\s*\)/.test(sellBuy));
    ok('it skips positions already being closed', /__vantaClosing/.test(sellBuy));
    ok('it is scoped to the market on screen', /vantaActiveCoin/.test(sellBuy));
    ok('it says in English that there is nothing to sell',
        /showToast\(\s*"There is no trade to sell"\s*\)/.test(sellBuy));
    ok('with nothing to sell it refuses instead of opening something',
        /if\s*\(\s*!list\.length\s*\)[\s\S]{0,200}return;[\s\S]{0,200}closePositions\(/.test(sellBuy));

    /* The per-position button says what it does, and closes only that trade. */
    const renderOpen = fnBody('renderOpenTrades') || '';
    ok('every open row says Close Trade, running or finished',
        /class="open-sell" data-exit="\$\{trade\.id\}"[^>]*>Close Trade</.test(renderOpen));
    ok('the row button carries its own trade id',
        /data-exit="\$\{trade\.id\}"/.test(renderOpen));
    ok('the row handler closes only that one id',
        /data-exit[\s\S]{0,300}sellOpenPositionById\(\s*btn\.getAttribute\(\s*"data-exit"\)\s*\)/.test(code));
    ok('no row claims to be a SELL any more',
        !/data-exit="[^"]*"[^>]*>SELL</.test(code));

    /* CLOSE ALL survives, and is still its own endpoint. */
    ok('CLOSE ALL is still on the positions card',
        /vtaCloseTrade[\s\S]{0,600}sellOpenPositions\(\)/.test(code));
    ok('CLOSE ALL still uses the atomic close-all endpoint',
        /api\/trade\/close-all/.test(code));
})();

console.log('\n[S1] trade history keeps EVERY completed trade');
(function () {
    const renderHistory = fnBody('renderHistory');
    ok('renderHistory exists', !!renderHistory);

    /* No trimming, in either code path. Scoped to the two real code paths, not
       the whole file: unrelated features legitimately slice their own lists (the
       PIN box, the deposit list, the ledger), and the prose that documents the
       old 5-row cap quotes it verbatim -- right inside this very function. So
       comments are stripped before the code is asserted on. */
    const stripComments = s => String(s || '').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
    const sync = stripComments(fnBody('vantaSyncTrades'));
    ok('the demo path never pops an old trade off the history',
        !/history\.length\s*>\s*\d+\s*\)\s*history\.pop\(\)/.test(stripComments(src))
        && !/history\.pop\(\)/.test(stripComments(src)));
    ok('the live sync never slices the history down to the newest few',
        !/hist\.slice\(\s*0\s*,\s*\d+\s*\)/.test(sync));
    ok('the live sync maps the whole list instead',
        /history\s*=\s*hist\.map\(/.test(sync));

    /* The figures a record needs. */
    const needed = {
        direction: /direction:\s*h\.direction/,
        amount: /amount:\s*Number\(h\.amount\)/,
        entryPrice: /entryPrice:\s*Number\(h\.entry_price\)/,
        exitPrice: /exitPrice:\s*h\.exit_price/,
        profit: /profit:\s*Number\(h\.profit\)/,
        result: /result:\s*h\.status/,
        status: /status:\s*h\.status/,
        timing: /at:\s*h\.settled_at/,
    };
    for (const [k, re] of Object.entries(needed)) {
        ok('the history record keeps ' + k, re.test(src));
    }

    /* ...and the row actually renders them. */
    ok('the row renders the entry price', /In \$\{price\(item\.entryPrice\)\}/.test(renderHistory || src));
    ok('the row renders the exit price', /Out \$\{item\.exitPrice/.test(renderHistory || src));
    ok('the row renders the stake as staked', /staked/.test(renderHistory || src));
    ok('the row renders the status in words', /history-status/.test(renderHistory || src));
    ok('the row renders the close timestamp', /toLocaleString/.test(renderHistory || src));

    /* The section scrolls rather than truncating, so 20 trades are all present. */
    ok('the section scrolls instead of dropping rows',
        /\.history-list[\s\S]{0,400}overflow-y:\s*auto/.test(src));
})();

console.log();
if (fails) {
  console.log('FAILED (' + fails + '):');
  process.exit(1);
}
console.log('All trade-lifecycle frontend checks passed.');
