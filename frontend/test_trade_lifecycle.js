/* Guards the trade LIFECYCLE and its accounting in the live frontend.
 *
 * The server is the authority on results, so these are the frontend rules
 * that must hold no matter what the backend does:
 *
 *   1. the close request sends NO value - a client cannot name what it is paid
 *   2. closing refreshes BOTH the balance and the realized profit/loss cards
 *   3. CLOSE ALL uses the atomic /api/trade/close-all endpoint, scoped to the
 *      active market, instead of looping single closes
 *   4. CLOSE ALL books the server's per-trade profit into the realized ledger
 *      and reports the total
 *   5. no timer force-settles a position into a loss, in live or in practice
 *   6. nothing in the UI claims a stake is lost when a timer runs out
 *   7. a position's duration is display metadata, never a deadline
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

console.log('\n[5] nothing force-settles a position into a loss');
ok('practice expiry sweep is gone', !/function vantaSettleDemoTrades/.test(src));
ok('no interval schedules it', !/setInterval\(\s*vantaSettleDemoTrades/.test(src));
ok('no server auto-settlement call',
  !/settle_due_trades/.test(src));
ok('no EXPIRED outcome is produced', !/reason:\s*"EXPIRED"/.test(src));
ok('no "EXPIRED" result label is rendered',
  !/vta-res-out-l[^;]*EXPIRED/.test(src));
ok('sync does not book a missing history row as a loss',
  !/:\s*-amt\s*;/.test(src));
ok('sync waits for a real history row before reporting', /if\s*\(\s*!\s*h\s*\)/.test(src));

console.log('\n[6] the UI does not promise a stake is lost when a timer runs out');
const promisesLoss = [
  /if the timer hits zero,? the stake is gone/i,
  /stake is gone/i,
  /before the timer runs out/i,
  /before the timer ends/i,
  /hit the timer and the stake was lost/i,
];
promisesLoss.forEach((re, n) => ok('no forfeiture copy #' + (n + 1) + ' ' + re, !re.test(src)));

console.log('\n[7] duration is display metadata, not a deadline');
ok('no "closes in" countdown tooltip', !/closes in /.test(src));
ok('no T− countdown column', !/data-l="T−"/.test(src));
ok('orders show a duration instead', /data-l="DUR"/.test(src));
ok('the note explains positions stay open',
  /stays open until you close/i.test(src));

console.log('\n[8] one PIN, checked by the server, with no Swap prerequisite');
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
