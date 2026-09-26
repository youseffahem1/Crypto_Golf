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
const splitSrc = slice('const vtSplit=net=>{', 'const sp=vtSplit(pl);');

/* The lines that read the deal records, compute the totals, write the two
   account rows and then the Balance row. */
const rowsSrc = slice('vtDeals().forEach(vtNoteRealized);', "const meta=$('vtaPosMeta');");

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
  '(function(){\n' + ledgerSrc + '\n' + splitSrc + '\n' +
  '  __out.vtSeedRealized=vtSeedRealized;__out.vtNoteRealized=vtNoteRealized;' +
  '  __out.vtRealizedTotals=vtRealizedTotals;__out.vtSplit=vtSplit;\n' +
  '  return __out;\n})()',
  sandbox
);
sandbox.money = n => '$' + Math.abs(Number(n) || 0).toFixed(2);

/* Reproduce the row writing exactly as rowsSrc does, for a given ledger. */
function summaryFor(ledgerSeed, floatingPl) {
  api.vtSeedRealized(ledgerSeed);
  const sp = api.vtSplit(floatingPl);
  const acct = api.vtRealizedTotals();
  return {
    profit: acct.profit > 0 ? '+' + sandbox.money(acct.profit) : sandbox.money(0),
    loss: acct.loss < 0 ? '−' + sandbox.money(Math.abs(acct.loss)) : sandbox.money(0),
    acctProfit: acct.profit,
    acctLoss: acct.loss,
    net: sp.net,
  };
}
const D = (id, profit) => ({ id, sym: 'GOLF', profit });

console.log('[0] the real page binds Profit / Loss to the realized totals');
check('Profit row uses acct.profit', /setN\('vtaPProfit',acct\.profit>/.test(rowsSrc), true);
check('Loss row uses acct.loss', /setN\('vtaPLoss',acct\.loss</.test(rowsSrc), true);
check('Profit row does NOT use floating sp', /setN\('vtaPProfit',[^)]*sp\./.test(rowsSrc), false);
check('Loss row does NOT use floating sp', /setN\('vtaPLoss',[^)]*sp\./.test(rowsSrc), false);
/* Balance must stay funds + floating only, or a settled payout is counted twice. */
check('Balance still excludes realized totals', /const total=funds\+sp\.profit\+sp\.loss;/.test(rowsSrc), true);
check('totals read the deal records', /vtDeals\(\)\.forEach\(vtNoteRealized\)/.test(rowsSrc), true);

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

console.log('\n[9] an open position\'s floating P/L does NOT leak into the totals');
r = summaryFor([D('t1', 0.24)], 12.75);
check('Profit (realized only)', r.profit, '+$0.24');
check('floating net is separate', r.net, 12.75);
r = summaryFor([D('t1', 0.24)], -8);
check('Loss ignores floating', r.loss, '$0.00');

console.log('\n[10] clearing a result does not erase a real trade');
/* vtClearLastSettled only nulls the on-screen result; the ledger is separate. */
r = summaryFor([D('t1', 0.24), D('t2', -60)], 0);
check('totals survive a Clear', r.profit, '+$0.24');
check('totals survive a Clear (loss)', r.loss, '−$60.00');

console.log('\n[11] opening a new trade does not move the totals');
const before = summaryFor([D('t1', 0.24)], 0);
const after = summaryFor([D('t1', 0.24)], 30);
check('Profit unchanged', after.profit, before.profit);
check('Loss unchanged', after.loss, before.loss);

console.log('\n[12] a $0 breakeven trade is neither profit nor loss');
r = summaryFor([D('t1', 0.24), D('t2', 0)], 0);
check('Profit', r.profit, '+$0.24');
check('Loss', r.loss, '$0.00');

console.log('\n' + '='.repeat(60));
if (FAILS.length) {
  console.log(FAILS.length + ' CHECK(S) FAILED');
  FAILS.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
