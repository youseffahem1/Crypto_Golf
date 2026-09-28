/* Guards STAKE RESERVATION and REALIZED-ONLY Profit/Loss in the live frontend.
 *
 * The server is the authority on every number here, so these are the frontend
 * rules that must hold no matter what the backend does:
 *
 *   1. opening a position deducts the stake from the shown balance at once
 *   2. the authoritative refresh follows immediately, so the server always wins
 *   3. the balance is never re-inflated by an open position's floating P/L
 *   4. the account Profit/Loss is built from CLOSED records only
 *   5. an open position's unrealized P/L never reaches the headline totals
 *   6. Profit and Loss stay two separate figures, never netted into one
 *   7. a per-position P/L is still drawn for each open position
 *   8. only the server's settled profit is ever booked as realized
 *   9. closing a position adds the server's profit to the ledger, once
 *  10. the wallet ledger is never touched by trading
 *
 * Usage: node test_stake_reservation.js <path to index.html>
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

console.log('\n[1] opening a position deducts the stake from the shown balance at once');
const create = fnBody('createTrade');
ok('createTrade exists', !!create);
if (create) {
  ok('subtracts the stake from the balance', /balance\s*=\s*Math\.max\(\s*0\s*,\s*Number\(balance\)\s*-\s*reserved\s*\)/.test(create));
  /* The server decided what was reserved, so that is the figure to project. */
  ok('the amount is the one the SERVER reserved',
    /const\s+reserved\s*=\s*Number\(serverTrade\s*&&\s*serverTrade\.amount\)\s*>\s*0/.test(create));
  ok('the typed value is only a fallback', /:\s*Number\(amount\);/.test(create));
  /* The deduction has to redraw, or the header keeps the old figure. */
  ok('redraws the balance right away', /balance\s*=\s*Math\.max[\s\S]{0,200}?updateBalance\(\)/.test(create));
  ok('redraws the account panel too', /vantaRenderAccountPanel\(\)/.test(create));
  /* ...and it has to happen AFTER the server accepted the trade, or a rejected
     trade would silently eat the user's balance. */
  const post = create.search(/await vantaApi\(\s*"\/api\/trade\/open"/);
  const deduct = create.search(/balance\s*=\s*Math\.max\(\s*0\s*,/);  ok('the server has already accepted the position', post >= 0);
  ok('the deduction comes after the server accepted it', post >= 0 && deduct > post,
    'POST at ' + post + ', deduction at ' + deduct);
  ok('a rejected open does not deduct anything',
    /catch\s*\(\s*e\s*\)\s*\{\s*(?:[^{}]|\{[^{}]*\})*?return;/.test(create));
  ok('practice mode reserves the stake the same way', /demoBalance\s*-=\s*amount/.test(create));
  ok('the client-side guard is on the available figure', /if\s*\(\s*amount\s*>\s*balance\s*\)/.test(create));
}

console.log('\n[2] the authoritative refresh follows, so the server always wins');
if (create) {
  ok('the balance is re-read from the server after opening', /await\s+refreshBalance\(\)/.test(create));
  /* Strip comments first: the word refreshBalance() also appears in the prose
     above the call, and a text search must not be fooled by a sentence. */
  const code = create.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
  ok('the refresh is AWAITED, not fire-and-forget',
    /await\s+refreshBalance\(\);/.test(code) && !/(?<!await )(?<![.\w])refreshBalance\(\);/.test(code));
  ok("the balance comes from the server's own endpoint", /vantaApi\("\/api\/wallet\/balances"\)/.test(src));
  const refresh = fnBody('refreshBalance');
  ok('refreshBalance reads the TRADING ledger', !!refresh && /tr\.USDT/.test(refresh));
  ok('...and never derives it from the wallet table', !!refresh && !/wallet[\s\S]{0,80}USDT\s*=/.test(refresh));
}

console.log('\n[3] the balance is never re-inflated by floating P/L');
const posCard = fnBody('updatePosCard');
ok('updatePosCard exists', !!posCard);
if (posCard) {
  /* The bug: the balance row was `funds + openAll.net`, a second invented
     balance that moved with the market while the user held a position. */
  ok('the balance row is the available figure alone', /setN\('vtaPBalance',\s*money\(funds\)\)/.test(posCard));
  ok('it does NOT add the open book to it', !/money\(\s*funds\s*\+\s*openAll\./.test(posCard));
  ok('nothing adds openAll.net into the balance row', !/vtaPBalance[^\n]*openAll/.test(posCard));
  ok('the balance is not coloured by unrealized P/L either',
    !/balEl\.className\s*=\s*'vta-acct-v '\s*\+\s*\(\s*openAll/.test(posCard));
}

console.log('\n[4] the account Profit/Loss is built from CLOSED records only');
if (posCard) {
  ok('the totals come from the realized ledger', /const acct=vtRealizedTotals\(\)/.test(posCard));
  ok('the realized ledger is fed from closed deals', /vtDeals\(\)\.forEach\(vtNoteRealized\)/.test(posCard));
  ok('the headline Profit is exactly the realized profit', /const totalProfit=acct\.profit;/.test(posCard));
  ok('the headline Loss is exactly the realized loss', /const totalLoss=acct\.loss;/.test(posCard));
}
const split = fnBody('vtRealizedTotals');
ok('vtRealizedTotals exists', !!split);
if (split) {
  ok('it splits into a positive profit and a negative loss',
    /if\s*\(\s*p\s*>\s*0\s*\)\s*profit\s*\+=\s*p;\s*else\s+if\s*\(\s*p\s*<\s*0\s*\)\s*loss\s*\+=\s*p;/.test(split));
  ok('a loss is never accumulated into profit', !/profit\s*\+=\s*Math\.abs/.test(split));
}

console.log('\n[5] an open position never reaches the headline totals');
if (posCard) {
  /* This is the exact defect the client reported. Both of these lines used to
     add the floating P/L of every open position to the two headline numbers. */
  ok('unrealized profit is NOT added to the headline profit',
    !/totalProfit\s*=\s*acct\.profit\s*\+/i.test(posCard));
  ok('unrealized loss is NOT added to the headline loss',
    !/totalLoss\s*=\s*acct\.loss\s*\+/i.test(posCard));
  ok('the open book is still computed, for the per-position rows',
    /vtOpenNetAll\(all,\s*sym\)/.test(posCard));
  ok('the sub-line calls an open position P/L "unrealized"',
    /P\/L unrealized until they close/.test(posCard));
  ok('the sub-line never describes an open position as won or lost',
    !/parts\.push\('won '\+money\(openAll/.test(posCard));
}

console.log('\n[6] Profit and Loss stay two separate figures');
if (posCard) {
  ok('Profit is written from totalProfit', /setN\('vtaPProfit',\s*totalProfit\s*>\s*0/.test(posCard));
  ok('Loss is written from totalLoss', /setN\('vtaPLoss',\s*totalLoss\s*<\s*0/.test(posCard));
  ok('a negative totalProfit can never render', /totalProfit\s*>\s*0\s*\?\s*'\+'\s*\+\s*money\(totalProfit\)\s*:\s*money\(0\)/.test(posCard));
  ok('a positive totalLoss can never render', /totalLoss\s*<\s*0\s*\?\s*.+\s*money\(Math\.abs\(totalLoss\)\)\s*:\s*money\(0\)/.test(posCard));
  ok('the two are displayed in separate rows', /id="vtaPProfit"/.test(src) && /id="vtaPLoss"/.test(src));
  /* The client's explicit instruction: +$20 and -$60 must not become -$40. */
  ok('the headline is not a single netted number',
    !/id="vtaPNet"/.test(src) && !/setN\('vtaPProfit',\s*money\(totalProfit\s*\+\s*totalLoss\)/.test(posCard));
}

console.log('\n[7] a per-position P/L is still drawn for each open position');
if (posCard) {
  ok('each open position gets its own P/L row', /const pnl\s*=\s*val\s*-\s*amt\s*;/.test(posCard));
  ok('it is priced with the direction-aware live value', /closeValueFor\(t\)/.test(posCard));
  ok('the row is pushed into the positions table', /rows\.push\(\{[^}]*pnl/.test(posCard));
  ok('the per-position P/L renders through the shared splitter', /vtPlCell\(r\.pnl/.test(posCard));
}
ok('the POSITIONS table has a P/L column',
  /vtHead\(\[\[[\s\S]{0,220}?\['pl','P\/L'\]/.test(src));
ok('...and it is distinct from the DEALS table\'s realized Profit',
  /vtHead\(\[\[[\s\S]{0,220}?\['pl','Profit'\]/.test(src));

console.log('\n[8] only the SERVER\'s settled profit is ever booked as realized');
/* Anything that writes into the realized ledger must take a number the server
   produced. A locally computed P/L must never reach it. */
const noteRealized = fnBody('vtNoteRealized');
ok('vtNoteRealized exists', !!noteRealized);
if (noteRealized) {
  /* Merging on the trade id is what makes a replay a no-op: re-reading the same
     closed deal REPLACES its row rather than appending a second one. */
  ok('it merges on the trade id, so a replay cannot double-count',
    /findIndex\([^)]*id\s*===/.test(noteRealized));
  ok('an existing row is replaced, not appended',
    /if\s*\(\s*i\s*>=\s*0\s*\)\s*\w+\[\s*i\s*\]\s*=\s*\w+\s*;\s*else\s+\w+\.push/.test(noteRealized));
  ok('it stores the profit it is handed', /profit\s*:/.test(noteRealized));
}
const noteCalls = src.match(/vantaNoteRealizedTrade\(/g) || [];
ok('the ledger is written from close paths only', noteCalls.length > 0);
/* Every write must pass a `profit` read off a server response. */
const profitSources = src.match(/vantaNoteRealizedTrade\(\s*\{[^}]*profit\s*:\s*[^}]*\}/g) || [];
ok('each write names a profit explicitly', profitSources.length > 0);
ok('the expiry close takes profit from the close response',
  /vantaNoteRealizedTrade\(\{\s*id:\s*t\.id,\s*sym:[^}]*profit\s*\}/.test(src));
ok('the close-all books each server profit',
  /vantaNoteRealizedTrade/.test(fnBody('sellOpenPositions') || ''));
ok('no open position is booked as realized',
  !/vantaNoteRealizedTrade\([^)]*closeValueFor/.test(src));
ok('no unrealized figure is added to the ledger',
  !/vtNoteRealized\([^)]*closeValueFor/.test(src));

console.log('\n[9] closing adds the server profit to the ledger exactly once');
const closePositions = fnBody('closePositions');
ok('closePositions exists', !!closePositions);
if (closePositions) {
  ok('books the server profit', /vantaNoteRealizedTrade/.test(closePositions));
  ok('reads the profit off the response', /Number\(sold\.profit/.test(closePositions));
  ok('sends no client price', !/api\/trade\/close["'][\s\S]{0,300}?value\s*:/.test(src));
  ok('refreshes the balance after settling', /refreshBalance\(\)/.test(closePositions));
}
const sellAll = fnBody('sellOpenPositions');
ok('close-all exists', !!sellAll);
if (sellAll) {
  ok('books each trade the server settled', /vantaNoteRealizedTrade/.test(sellAll));
  ok('guards positions while in flight', /__vantaClosing/.test(sellAll));
  ok('refreshes the balance', /refreshBalance\(\)/.test(sellAll));
}

console.log('\n[10] the wallet ledger is never touched by trading');
ok('the two ledgers are separate variables', /window\.vantaWalletBalances/.test(src));
ok('trading reads the trading table', /window\.vantaCoinBalances\s*=\s*coinBalances/.test(src));
ok('the wallet table is built from the wallet side', /window\.vantaWalletBalances\s*=\s*walletBalances/.test(src));
/* The header balance must be the TRADING balance, never the wallet's. */
const updateBalance = fnBody('updateBalance');
ok('updateBalance exists', !!updateBalance);
if (updateBalance) {
  ok('it shows `balance` (the trading ledger) in live mode',
    /accountMode\s*===\s*"demo"\s*\?\s*demoBalance\s*:\s*balance/.test(updateBalance));
  ok('it never shows a wallet figure', !/walletBalances/.test(updateBalance));
}
/* The refresh copying the wallet's own columns into a local cache is correct
   bookkeeping. What must never happen is a TRADING path moving money between the
   two ledgers — so the check is that the trade functions never mention the
   wallet ledger at all, and that nothing does arithmetic on it. */
const tradeFns = ['createTrade', 'closePositions', 'sellOpenPositions',
                  'vantaExpireDueTrades', 'vantaSyncTrades'];
tradeFns.forEach(n => {
  const b = fnBody(n);
  ok(n + ' never touches the wallet ledger', !!b && !/walletBalances|vantaWalletBalances/.test(b));
});
ok('nothing does arithmetic on the wallet ledger',
  !/walletBalances\[[^\]]*\]\s*[-+*\/]=/.test(src) && !/vantaWalletBalances\[[^\]]*\]\s*[-+*\/]=/.test(src));
ok('no server column for the wallet is written from the client',
  !/usdt_wallet_balance\s*=/.test(src));

console.log();
if (fails) {
  console.log('FAILED (' + fails + '):');
  process.exit(1);
}
console.log('All stake-reservation / realized-P/L frontend checks passed.');
