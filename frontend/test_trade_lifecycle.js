/* Guards the trade LIFECYCLE and its accounting in the live frontend.
 *
 * The server is the authority on both the RESULT and the DEADLINE, so these
 * are the frontend rules that must hold no matter what the backend does:
 *
 *   1. the close request sends NO value - a client cannot name what it is paid
 *   2. closing refreshes BOTH the balance and the realized profit/loss cards
 *   3. CLOSE ALL uses the atomic /api/trade/close-all endpoint, scoped to the
 *      active market, instead of looping single closes
 *   4. CLOSE ALL books the server's per-trade profit into the realized ledger
 *      and reports the total
 *   5. reaching 00:00 STOPS the countdown and hands the position to the server
 *   6. expiry goes through the ordinary idempotent close, priced by the server
 *   7. NO timer ever force-settles a position into a loss, live or in practice
 *   8. an expired position is never drawn as an open one
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
  ok('banks each trade profit', /vantaNoteRealizedTrade/.test(sellAll));
  ok('reads profit off the server response', /Number\(s\.profit/.test(sellAll));
  ok('sums the batch for the toast', /reduce\(/.test(sellAll));
  ok('removes the closed positions', /trades\s*=\s*trades\.filter/.test(sellAll));
}

console.log('\n[5] reaching 00:00 STOPS the countdown');
const updateTrades = fnBody('updateTrades');
ok('updateTrades exists', !!updateTrades);
if (updateTrades) {
  ok('reads the deadline through the shared helper',
    /vantaTimeLeft\(/.test(updateTrades) && /vantaIsExpired\(/.test(updateTrades));
  /* The defect: the countdown reached 00:00 and was rewritten every frame
     forever, so a live-looking row sat at 00:00 indefinitely. The fix is an
     early `continue` that must come BEFORE the countdown write, so an expired
     position is never re-touched at all. Order is the whole point, so this
     compares positions rather than matching a formatted block. */
  const bail = updateTrades.search(/\bcontinue\s*;/);
  const write = updateTrades.search(/countdown\s*\.\s*textContent|\[data-countdown/);
  ok('there is an early bail-out in the loop', bail >= 0);
  ok('there is a countdown write in the loop', write >= 0);
  ok('the bail-out comes BEFORE the countdown write, so an expired position is never re-written',
    bail >= 0 && write >= 0 && bail < write,
    'continue at ' + bail + ', countdown write at ' + write);
  ok('the bail-out is guarded by the expiry test',
    /if\s*\(\s*expired\s*\)/.test(updateTrades));
  ok('flags the position as expired', /vantaMarkExpired\(/.test(updateTrades));
  ok('hands expired positions to the server', /vantaExpireDueTrades\(\)/.test(updateTrades));
}
const timeLeft = fnBody('vantaTimeLeft');
ok('remaining is clamped at zero, never negative',
  !!timeLeft && /Math\.max\(\s*0\s*,/.test(timeLeft));
const parse = fnBody('vantaParseUTC');
ok('a bad trade timestamp yields 0, never NaN',
  !!parse && /Number\.isNaN\(d\)\s*\?\s*\(fallback === undefined \? 0 : fallback\)/.test(parse));
ok('the trade path calls it with no fallback, so bad dates fail towards "settle"',
  /vantaParseUTC\(t && t\.closes_at\)/.test(src) &&
  /vantaParseUTC\(t && t\.opened_at\)/.test(src));
/* Candles are the one caller that wants a bar at the live edge rather than at
   the epoch, so they must opt in rather than inherit the trade default. */
ok('candles opt into the live-edge fallback explicitly',
  /vantaParseUTC\(row\.open_time,\s*Date\.now\(\)\)/.test(src));

console.log('\n[6] expiry goes through the ordinary server-priced close');
const expire = fnBody('vantaExpireDueTrades');
ok('vantaExpireDueTrades exists', !!expire);
if (expire) {
  /* The request body is the only place a client-supplied price could sneak in,
     so the check is scoped to that JSON.stringify payload rather than to the
     whole function: the result panel legitimately shows a `value` it read back
     from the server, and that is not a client price. */
  const req = expire.match(/vantaApi\(\s*"\/api\/trade\/close"[\s\S]{0,400}?\}\s*\)/);
  ok('posts to the normal /api/trade/close', !!req);
  const body = req ? req[0] : '';
  ok('sends only the trade id', /trade_id:\s*t\.id/.test(body), body.slice(0, 160));
  ok('sends NO value', !/\bvalue\s*:/.test(body), 'client value still sent');
  ok('guards against re-firing every frame', /t\.ending\s*=\s*true/.test(expire));
  ok('retries after a failure', /t\.ending\s*=\s*false/.test(expire));
  ok('reads the profit off the server response', /sold\s*&&\s*sold\.profit/.test(expire));
  ok('reports the reason as EXPIRED', /reason:\s*expired\s*\?\s*"EXPIRED"/.test(expire));
  ok('removes the settled position', /trades\s*=\s*trades\.filter/.test(expire));
  ok('refreshes balance and cards', /refreshBalance\(\)/.test(expire) &&
    /vantaRefreshInfoCards|refreshInfoCards/.test(expire));
  ok('practice settles at the live value, not a flat loss',
    /closeValueFor|closePositions\(due\)/.test(expire));
  /* A dead server must not leave a permanently stuck row: the client has to
     be willing to ask again, because the server's own sweep is the real
     guarantee. */
  ok('backs off instead of asking every frame',
    /__vantaExpireRetryAt\s*=\s*now\s*\+\s*\d+/.test(expire));
}

console.log('\n[7] NO timer force-settles a position into a loss');
ok('the old practice sweep is still gone', !/function vantaSettleDemoTrades/.test(src));
ok('no interval schedules it', !/setInterval\(\s*vantaSettleDemoTrades/.test(src));
ok('the old -amount settlement helper is still gone',
  !/settle_due_trades/.test(src));
ok('no source books profit as the negated stake',
  !/profit\s*[:=]\s*-{1,2}\s*(amt|amount|t\.amount|trade\.amount)\s*;/.test(src));
ok('sync does not book a missing history row as a loss',
  !/:\s*-amt\s*;/.test(src));
ok('sync waits for a real history row before reporting', /if\s*\(\s*!\s*h\s*\)/.test(src));

console.log('\n[8] an expired position is never drawn as an open one');
const renderOpen = fnBody('renderOpenTrades');
ok('renderOpenTrades exists', !!renderOpen);
if (renderOpen) {
  ok('filters expired positions out of the open list',
    /filter\(t\s*=>\s*!vantaIsExpired\(t,\s*now\)\)/.test(renderOpen));
  ok('the count badge counts the filtered set',
    /openCountEl\.textContent\s*=\s*activeTrades\.length/.test(renderOpen));
}
const markExpired = fnBody('vantaMarkExpired');
ok('the chart marker is marked expired', !!markExpired && /is-expired/.test(markExpired));
ok('an expired marker is styled distinctly',
  /\.trade-marker\.is-expired/.test(src));
const sync = fnBody('vantaSyncTrades');
ok('the sync re-checks expiry against the server', !!sync && /vantaExpireDueTrades\(\)/.test(sync));
ok('the sync reads the reason from the server',
  !!sync && /close_reason\s*===\s*"EXPIRED"/.test(sync));
/* Entering the app used to start a second animation loop and a second set of
   intervals, so one trade was driven by two countdowns at once. */
const enterApp = fnBody('vantaEnterApp');
ok('entering the app is idempotent', !!enterApp && /__vantaAppEntered\s*=\s*true/.test(src));

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
ok('the positions note explains the real rule',
  /banks its gain rather than being lost/.test(src));
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

console.log();
if (fails) {
  console.log('FAILED (' + fails + '):');
  process.exit(1);
}
console.log('All trade-lifecycle frontend checks passed.');
