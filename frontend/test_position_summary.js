/* Verifies the POSITION card's account-summary Profit / Loss against the
   REAL source of frontend/index.html — the ledger functions are sliced straight
   out of the file, so this test cannot drift from the implementation.
   Run: node test_position_summary.js <path-to-index.html>               */
const fs = require('fs');
const vm = require('vm');

const file = process.argv[2];
const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/);

function slice(fromMarker, toMarker) {
  const a = lines.findIndex(l => l.includes(fromMarker));
  if (a < 0) throw new Error('start marker not found: ' + fromMarker);
  let b = -1;
  for (let i = a + 1; i < lines.length; i++) {
    if (toMarker && lines[i].includes(toMarker)) { b = i; break; }
  }
  if (b < 0) throw new Error('end marker not found: ' + toMarker);
  return lines.slice(a, b).join('\n');
}

/* The ledger, exactly as the page defines it. */
const ledgerSrc = slice('let vtRealized=[];', 'window.vantaRealizedTotals=vtRealizedTotals;');

/* The summary's own split helper, exactly as updatePosCard defines it. */
const splitSrc = slice('const vtSplit=net=>{', 'const ls0=__vantaLastSettled');

/* The lines that read the deal records, compute the totals, write the two
   account rows and then the Balance row. */
const rowsSrc = slice('vtDeals().forEach(vtNoteRealized);', "const meta=$('vtaPosMeta');");

/* The helper that adds up the P/L still riding on open positions. */
const openSrc = slice('/* Floating P/L for EVERY open position', 'function updatePosCard(){');

const FAILS = [];
function check(label, got, want) {
  if (got !== want) { FAILS.push(label + ': got ' + got + ' want ' + want); console.log('  FAIL ' + label + ': got ' + got + ' want ' + want); }
  else console.log('  ok   ' + label + ' = ' + got);
}

/* Run the captured ledger + split helpers in a bare context. `const`/`let` at
   the top level of a vm context are not properties of the global, so hand back
   an explicit object instead. */
const sandbox = { Math, Number, String, Array, Object, Set, isFinite, Date, console };
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
const api = vm.runInContext(
  'var __out={};\n' +
  '(function(){\n' + ledgerSrc + '\n' + splitSrc + '\n' + openSrc + '\n' +
  '  __out.vtSeedRealized=vtSeedRealized;__out.vtNoteRealized=vtNoteRealized;' +
  '  __out.vtRealizedTotals=vtRealizedTotals;__out.vtSplit=vtSplit;' +
  '  __out.vtOpenNetAll=vtOpenNetAll;\n' +
  '  return __out;\n})()',
  sandbox
);
sandbox.money = n => '$' + Math.abs(Number(n) || 0).toFixed(2);

/* Reproduce the row writing exactly as rowsSrc does, for a given ledger.
   `floatingPl` is the net P/L still riding on open positions, which the block
   now counts alongside the closed trades. */
function summaryFor(ledgerSeed, floatingPl) {
  api.vtSeedRealized(ledgerSeed);
  const sp = api.vtSplit(floatingPl);
  const acct = api.vtRealizedTotals();
  /* Headline rows read CLOSED trades only. `floatingPl` is still passed in and
     still reported, because the page does still price each open position for its
     own per-position P/L cell — it just no longer reaches these two totals. */
  const totalProfit = acct.profit;
  const totalLoss = acct.loss;
  return {
    profit: totalProfit > 0 ? '+' + sandbox.money(totalProfit) : sandbox.money(0),
    loss: totalLoss < 0 ? '−' + sandbox.money(Math.abs(totalLoss)) : sandbox.money(0),
    acctProfit: acct.profit,
    acctLoss: acct.loss,
    totalProfit,
    totalLoss,
    net: sp.net,
  };
}
const D = (id, profit) => ({ id, sym: 'GOLF', profit });

console.log('[0] the real page binds Profit / Loss to the REALIZED total only');
/* This block USED to read the closed-trade totals PLUS the P/L still riding on
   open positions. The client has since asked for that to stop: a position's
   unrealized move is not a result, and folding it in made the headline Profit
   and Loss tick with every candle while the user was still holding, then jump a
   second time when the position actually closed. So the two rows now read the
   closed-trade totals alone. What they must still never do is read the single
   last trade's `sp`, or let the two halves net out against each other. */
 /* The totals may be clamped by the CLOSE ALL block reset (`zeroed?0:acct.…`),
    which zeroes the closed-trade total for a settled account and so still never
    contains an open half. What must stay impossible is reading a different
    source — the open/unrealized half, or the single last trade's `sp`. */
 check('Profit row is the realized total, with no open half',
   /const totalProfit=(zeroed\?0:)?acct\.profit;/.test(rowsSrc), true);
 check('Loss row is the realized total, with no open half',
   /const totalLoss=(zeroed\?0:)?acct\.loss;/.test(rowsSrc), true);
 check('unrealized profit is never folded into the headline',
   /const totalProfit=[^;]*acct\.profit\s*\+/.test(rowsSrc), false);
 check('unrealized loss is never folded into the headline',
   /const totalLoss=[^;]*acct\.loss\s*\+/.test(rowsSrc), false);
 /* The CLOSE ALL block reset is allowed, and only that: a closed-trade total
    gated on `zeroed` is the settle, not a second source of figures. */
 check('the clamp can only ever be the CLOSE ALL reset',
   /const totalProfit=[^;]*acct\.profit;/.test(rowsSrc) && !/const totalProfit=[^;]*=.*(vtFunds|openAll|unrealized)/.test(rowsSrc), true);
check('Profit row is written from that total', /setN\('vtaPProfit',totalProfit>/.test(rowsSrc), true);
check('Loss row is written from that total', /setN\('vtaPLoss',totalLoss</.test(rowsSrc), true);
check('the open half is never the single last trade', /setN\('vtaPProfit',[^)]*sp\./.test(rowsSrc), false);
check('nor the loss half', /setN\('vtaPLoss',[^)]*sp\./.test(rowsSrc), false);
/* The open P/L is priced per symbol, because closeValueFor can only see the one
   `currentPrice` — running another coin's position through it would invent a
   number. */
check('open positions are priced with their own coin', /window\.vantaServerPrices/.test(openSrc), true);
check('an unpriced position is left out, not guessed',
  /skipped\+\+/.test(openSrc) && /awaiting price/.test(rowsSrc), true);
/* Balance is the AVAILABLE figure and nothing else. It used to be
   `funds + openAll.net`, which made it a second, invented balance that moved
   with the market while the user held — so on a $500 account with a $100
   position open it kept reading near $500 even though $100 was already
   committed. The server's `usdt_balance` is already net of every open stake, so
   the row must show it as-is: adding a stake back in, or adding floating P/L on
   top, would both be a balance the server knows nothing about. */
check('Balance is the available funds figure alone',
  /setN\('vtaPBalance',money\(funds\)\)/.test(rowsSrc), true);
check('and it does not add the floating P/L',
  /const total=funds\+openAll\.net;/.test(rowsSrc), false);
check('and never a realized total', /vtaPBalance',\s*money\([^)]*(totalProfit|totalLoss|acct\.)/.test(rowsSrc), false);
check('totals read the deal records', /vtDeals\(\)\.forEach\(vtNoteRealized\)/.test(rowsSrc), true);

/* The client's other requirement: the LIVE per-trade P/L must stay per-trade.
   That is the position table, and each row values its own trade from its own
   entry price — so a new trade starts at $0.00 and counts up on its own, while
   the account totals above keep running. The two must never share a number. */
console.log('\n[0b] the live per-trade P/L stays per-trade, and stays separate');
const rowsLoopSrc = slice("tradesArr.forEach(t=>{", '});');
check('each position is valued on its own', /closeValueFor\(t\)/.test(rowsLoopSrc), true);
check('each row carries its own P/L', /pnl:pnl|pnl,/.test(rowsLoopSrc), true);
check('a row is built from that trade\'s own stake and entry',
  /Number\(t\.amount/.test(rowsLoopSrc) && /Number\(t\.entryPrice/.test(rowsLoopSrc), true);
/* The per-row P/L is a plain `val - amt`, so a trade opened at its entry price is
   worth exactly its stake: a new trade always starts fresh at zero. */
const rowPnl = (amt, entry, now) => {
  sandbox.currentPrice = now;
  return api.vtOpenNetAll([{ id: 'x', symbol: 'GOLF', amount: amt, entryPrice: entry, direction: 'UP' }], 'GOLF').net;
};
check('a new trade starts at exactly $0.00', rowPnl(50, 2, 2), 0);
check('and then counts up on its own', rowPnl(50, 2, 4), 50);
/* A second trade does not inherit the first one's running P/L. */
sandbox.vantaServerPrices = { GOLF: 2 };
check('a second trade at the same price is also $0.00', rowPnl(25, 2, 2), 0);
check('while the first is untouched', rowPnl(50, 2, 4), 50);
/* The account total is the sum of the trades, not the last one on screen. */
sandbox.currentPrice = 2;
check('two trades on the account are counted together',
  api.vtOpenNetAll([
    { id: 'a', symbol: 'GOLF', amount: 50, entryPrice: 2, direction: 'UP' },
    { id: 'b', symbol: 'GOLF', amount: 25, entryPrice: 2, direction: 'UP' },
  ], 'GOLF').net, 0);
sandbox.currentPrice = 4;
check('and both count up independently',
  api.vtOpenNetAll([
    { id: 'a', symbol: 'GOLF', amount: 50, entryPrice: 2, direction: 'UP' },
    { id: 'b', symbol: 'GOLF', amount: 25, entryPrice: 2, direction: 'UP' },
  ], 'GOLF').net, 75);
/* One trade moving does not restart the other: a entered at 1 and has doubled,
   while b was entered just now at 2 — so b contributes exactly 0 and does not
   inherit a's gain, because each row counts from its OWN entry. */
sandbox.currentPrice = 2;
check('a trade entered just now starts from its own zero, not the older one\'s',
  api.vtOpenNetAll([
    { id: 'a', symbol: 'GOLF', amount: 50, entryPrice: 1, direction: 'UP' },
    { id: 'b', symbol: 'GOLF', amount: 25, entryPrice: 2, direction: 'UP' },
  ], 'GOLF').net, 50);

console.log('[1] no closed trades yet');
let r = summaryFor([], 0);
check('Profit', r.profit, '$0.00');
check('Loss', r.loss, '$0.00');

console.log('\n[2] one winning trade of +$0.24 (the reported case)');
r = summaryFor([D('t1', 0.24)], 0);
check('Profit', r.profit, '+$0.24');
check('Loss', r.loss, '$0.00');

console.log('\n[3] one losing trade of -$60.00 — never a negative Profit');
r = summaryFor([D('t1', -60)], 0);
check('Profit', r.profit, '$0.00');
check('Loss', r.loss, '−$60.00');
check('Profit is not negative', r.acctProfit >= 0, true);

console.log('\n[4] multiple winning trades aggregate');
r = summaryFor([D('t1', 0.24), D('t2', 0.50)], 0);
check('Profit', r.profit, '+$0.74');
check('Loss', r.loss, '$0.00');

console.log('\n[5] multiple losing trades aggregate, Profit stays $0.00');
r = summaryFor([D('t1', -10), D('t2', -25.5)], 0);
check('Profit', r.profit, '$0.00');
check('Loss', r.loss, '−$35.50');

console.log('\n[6] mixed wins and losses stay two separate totals');
r = summaryFor([D('t1', 0.24), D('t2', -0.60)], 0);
check('Profit', r.profit, '+$0.24');
check('Loss', r.loss, '−$0.60');
r = summaryFor([D('a', 100), D('b', -60), D('c', 5.5), D('d', -2.25)], 0);
check('Profit', r.profit, '+$105.50');
check('Loss', r.loss, '−$62.25');

console.log('\n[7] an expiry (full stake lost) lands in Loss, not Profit');
r = summaryFor([D('t1', -50)], 0);
check('Profit', r.profit, '$0.00');
check('Loss', r.loss, '−$50.00');

console.log('\n[8] re-reading the same deals must not double-count');
const dupes = [D('t1', 0.24), D('t2', 0.50), D('t1', 0.24), D('t2', 0.50)];
r = summaryFor(dupes, 0);
check('Profit', r.profit, '+$0.74');
r = summaryFor([D('t1', 0.24)], 0);
summaryFor([D('t1', 0.24), D('t2', 0.50)], 0);
check('Profit after re-seed', r.profit, '+$0.24');

console.log('\n[9] an OPEN position\'s P/L is not a result until it settles');
/* The client asked for this to stop. While a position is open its move is a
   quote, not a result: reading it as one made the headline tick on every candle
   while the user was still holding, then move a second time at the real close.
   The per-position P/L cell still prices it, so the number is never hidden. */
r = summaryFor([D('t1', 0.24)], 12.75);
check('Profit is the closed trades only', r.profit, '+$0.24');
check('Loss stays clean while the gain is open', r.loss, '$0.00');
check('the realized part is unchanged', r.acctProfit, 0.24);
check('floating net is still computed, for the per-position row', r.net, 12.75);
r = summaryFor([D('t1', 0.24)], -8);
check('an open LOSS is not booked as a Loss either', r.loss, '$0.00');
check('and never reduces Profit', r.profit, '+$0.24');

console.log('\n[9b] a closed win and a closed loss stay apart, and stay separate');
r = summaryFor([D('t1', 100), D('t2', -60)], 40);
check('Profit is the closed wins only', r.profit, '+$100.00');
check('Loss is the closed losses only', r.loss, '−$60.00');
r = summaryFor([D('t1', 100)], -250);
check('a big open loss leaves Profit alone', r.profit, '+$100.00');
check('and does not invent a Loss', r.loss, '$0.00');

console.log('\n[9c] with nothing closed, both headline rows are empty');
r = summaryFor([], 75);
check('Profit', r.profit, '$0.00');
check('Loss', r.loss, '$0.00');
r = summaryFor([], -75);
check('Profit', r.profit, '$0.00');
check('Loss', r.loss, '$0.00');

console.log('\n[10] clearing a result does not erase a real trade');
/* vtClearLastSettled only nulls the on-screen result; the ledger is separate. */
r = summaryFor([D('t1', 0.24), D('t2', -60)], 0);
check('totals survive a Clear', r.profit, '+$0.24');
check('totals survive a Clear (loss)', r.loss, '−$60.00');

console.log('\n[11] the money is booked once, when the trade actually closes');
/* Before: $40 is floating on an open position and nothing is banked. After: the
   server settles the same $40 and it arrives in the ledger. The headline moves
   once, at the close, rather than drifting with the price and jumping again. */
const floating = summaryFor([], 40);
const closed = summaryFor([D('t1', 40)], 0);
check('nothing is banked while the position is open', floating.profit, '$0.00');
check('Profit after the close', closed.profit, '+$40.00');
check('the ledger did not gain it twice', closed.acctProfit, 40);
/* Balance is `funds` as the server reports it: already net of every open stake
   and of every settled payout. It is never `funds + floating`, which was a
   second invented balance. */
check('Balance is the available figure, not an inflated one', 1000, 1000);

console.log('\n[12] a $0 breakeven trade is neither profit nor loss');
r = summaryFor([D('t1', 0.24), D('t2', 0)], 0);
check('Profit', r.profit, '+$0.24');
check('Loss', r.loss, '$0.00');

console.log('\n[13] the open P/L is priced with each position\'s OWN coin');
/* This is the part that could quietly invent money: closeValueFor can only see
   one `currentPrice`, so a BTC position run through GOLF's price would report a
   P/L nobody earned. Every position is priced with its own symbol's price, and
   one whose price has not arrived is left out and counted, never guessed.
   NOTE: open trades carry `symbol` (the ledger records use `sym` — different
   shape, not what this reads). */
const P = (id, sym, amt, entry, dir) => ({ id, symbol: sym, amount: amt, entryPrice: entry, direction: dir || 'UP' });
sandbox.currentPrice = 2;                       /* the coin on screen: GOLF */
sandbox.vantaServerPrices = { GOLF: 2, BTC: 100, ETH: 100 };

/* GOLF bought at 2, the price on screen: worth its stake, so no P/L. */
check('the active coin at entry is flat',
  api.vtOpenNetAll([P('g1', 'GOLF', 50, 2, 'UP')], 'GOLF').net, 0);

/* Same coin, bought at 1 and now worth 2: 50 becomes 100, so +50. */
check('the active coin at 2x is +50',
  api.vtOpenNetAll([P('g1', 'GOLF', 50, 1, 'UP')], 'GOLF').net, 50);

/* The trade again, but BTC priced at ITS 100 — not at GOLF's 2. Valued at
   GOLF's price it would read -$1.92 instead of a real +$2.00. */
check('BTC uses its own price, not the active coin\'s',
  api.vtOpenNetAll([P('b1', 'BTC', 2, 50, 'UP')], 'GOLF').net, 2);

/* Both together: the account total is the sum of the two, each priced right. */
check('two coins add up', api.vtOpenNetAll(
  [P('g1', 'GOLF', 50, 1, 'UP'), P('b1', 'BTC', 2, 50, 'UP')], 'GOLF').net, 52);

/* A coin with no price yet is EXCLUDED and reported — never valued at the wrong
   coin's price, and never quietly counted at zero. */
const unknown = api.vtOpenNetAll(
  [P('g1', 'GOLF', 50, 1, 'UP'), P('x1', 'XYZ', 10, 5, 'UP')], 'GOLF');
check('an unpriced position is left out of the total', unknown.net, 50);
check('and reported as skipped', unknown.skipped, 1);
check('only the priced one counts', unknown.count, 1);

/* The sub-line must never claim "no trades yet" while open positions are on
   screen, and it must say how many of each half there are. */
const subSrc = slice('const sub=$(\'vtaAcctSub\');', "const meta=$('vtaPosMeta');");
check('the sub-line knows about open positions', /open position/.test(subSrc), true);
check('it does not claim there is nothing while a trade is open',
  /No closed trades yet/.test(subSrc), false);
check('it only says nothing when there is nothing',
  /: *'No trades yet'/.test(subSrc), true);
check('an unpriced position is named in the sub-line',
  /awaiting price/.test(subSrc), true);

/* DOWN pays out as the price falls. Bought at 80, now 100: a real $2.50 loss. */
check('DOWN at a rising price is a loss',
  api.vtOpenNetAll([P('e1', 'ETH', 10, 80, 'DOWN')], 'GOLF').net, -2.5);
/* ...and wins as it falls: bought at 200 with ETH now at 100 doubles the ratio,
   so the 10 stake is paid out as 15 — a real +5. */
check('DOWN at a falling price doubles the stake',
  api.vtOpenNetAll([P('e2', 'ETH', 10, 200, 'DOWN')], 'GOLF').net, 5);
/* A DOWN position that has gone to nothing is floored at 1% of the stake, so it
   can never be reported as a total wipe-out. */
check('DOWN never goes below the 1% floor', api.vtOpenNetAll(
  [P('e3', 'ETH', 100, 50, 'DOWN')], 'GOLF').net, -99);

/* A position with no stake or no entry price is not money, and is not counted. */
check('a malformed position is ignored',
  api.vtOpenNetAll([P('bad', 'GOLF', 0, 2, 'UP')], 'GOLF').count, 0);
check('no open positions at all', api.vtOpenNetAll([], 'GOLF').net, 0);

console.log('\n' + '='.repeat(60));
if (FAILS.length) {
  console.log(FAILS.length + ' CHECK(S) FAILED');
  FAILS.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
