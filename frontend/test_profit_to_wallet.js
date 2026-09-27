/* Verifies the trading screen's PROFIT / LOSS card and its "Move to Wallet"
   action against the REAL source of frontend/index.html — the render helpers
   are sliced straight out of the file, so this test cannot drift from the
   implementation. The transfer itself is proven server-side by
   backend/smoke_profit_to_wallet.py; what is checked here is the classification
   the page performs, the colour rule, the disabled state, and the request the
   button actually makes.
   Run: node test_profit_to_wallet.js <path-to-index.html>                */
const fs = require('fs');
const vm = require('vm');

const file = process.argv[2];
const html = fs.readFileSync(file, 'utf8');
const lines = html.split(/\r?\n/);

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

/* The whole dashboard-cards block header through to just before
   refreshInfoCards: mainEl, byId, usd, setText, setNeg, activeSymbol,
   liveSplit, setMoveAvailability and writeResult, exactly as the page
   defines them. */
const renderSrc = slice('if (window.__vantaDashboardCards) return;', 'function refreshInfoCards(){');

/* The whole "Move to Wallet" feature, exactly as the page defines it: the
   two-step dialog markup helpers, the coin list loader, the PIN submit and the
   wiring. The marker includes the ": realized trading profit" part because a
   DIFFERENT feature above it — the whole-holding USDT cash-out, which keeps its
   own endpoint and is intentionally left alone — starts with the same words. */
const moveSrc = slice('"Move to Wallet": realized trading profit',
  '/* ---- Dashboard balance card quick actions (delegated) ---- */');

/* The practice-mode split + move, exactly as the page defines them. */
const practiceSrc = slice('function practiceMoved(){', 'function realBal(sym){');

const FAILS = [];
function check(label, got, want) {
  if (got !== want) { FAILS.push(label + ': got ' + got + ' want ' + want); console.log('  FAIL ' + label + ': got ' + got + ' want ' + want); }
  else console.log('  ok   ' + label + ' = ' + got);
}

/* ---- a minimal element registry standing in for the page's DOM ---------- */
function el() { return { textContent: '', className: '', title: '', disabled: false, dataset: {} }; }
const els = {
  icProfit: el(), icLoss: el(), icProfitSub: el(), icMoveBtn: el(),
  icInvested: el(), icInvestedTotal: el(), icInvestedMsg: el(),
};
const sandbox = {
  Math, Number, String, Array, Object, isFinite, Date, console,
  document: { getElementById: id => els[id] || null, querySelector: () => null },
  accountMode: 'live',
  window: null,
};
sandbox.window = sandbox;
sandbox.vantaActiveCoin = 'GOLF';
vm.createContext(sandbox);
const api = vm.runInContext(
  'var __out={};\n(function(){\n' + renderSrc + '\n' +
  '  __out.writeResult=writeResult;__out.setMoveAvailability=setMoveAvailability;' +
  '  __out.liveSplit=liveSplit;__out.activeSymbol=activeSymbol;return __out;\n})()',
  sandbox
);

const render = (sp, sym) => { api.writeResult(sp, sym || 'GOLF'); return els; };
const btn = els.icMoveBtn;

console.log('[0] the page binds PROFIT and LOSS to separate totals');
check('the card has a LOSS element', /id="icLoss"/.test(html), true);
check('LOSS is rendered by writeResult', /icLoss/.test(renderSrc), true);
check('PROFIT is never given the red `neg` class', /setNeg\('icProfit'/.test(html), false);
check('PROFIT keeps the existing green styling', /\.ic-profit \.ic-value\{color:#1ee68d/.test(html), true);
check('only LOSS gets a red class', /lossEl\.className = 'ic-pl-v'/.test(renderSrc), true);

console.log('\n[1] CASE 1 — a profit: PROFIT +$20.00, LOSS $0.00, move enabled');
render({ profit: 20, loss: 0, available: 20 });
check('PROFIT', els.icProfit.textContent, '$20.00');
check('PROFIT is not red', /(^|\s)neg(\s|$)/.test(els.icProfit.className), false);
check('LOSS', els.icLoss.textContent, '$0.00');
check('LOSS is not red', /down/.test(els.icLoss.className), false);
check('Move to Wallet enabled', btn.disabled, false);
check('button explains the two-step action', /confirm with your wallet PIN/.test(btn.title), true);

console.log('\n[2] the reported bug — a loss can never render as a negative PROFIT');
render({ profit: 0, loss: -681.01, available: 0 });
check('PROFIT', els.icProfit.textContent, '$0.00');
check('LOSS', els.icLoss.textContent, '\u2212$681.01');
check('LOSS is red', /down/.test(els.icLoss.className), true);
check('Move to Wallet disabled', btn.disabled, true);

console.log('\n[3] CASE 2 — loss only: nothing is transferable');
render({ profit: 0, loss: -60, available: 0 });
check('PROFIT', els.icProfit.textContent, '$0.00');
check('LOSS', els.icLoss.textContent, '\u2212$60.00');
check('Move to Wallet disabled', btn.disabled, true);
check('the sub-line says there is nothing to move', /No profit to move/.test(els.icProfitSub.textContent), true);

console.log('\n[4] CASE 3 — profit and loss from different trades are never netted');
render({ profit: 20, loss: -60, available: 20 });
check('PROFIT stays $20.00', els.icProfit.textContent, '$20.00');
check('LOSS is the full -$60.00', els.icLoss.textContent, '\u2212$60.00');
check('not netted to -$40.00', els.icProfit.textContent, '$20.00');
check('only the profit is transferable', btn.disabled, false);
check('button offers exactly the profit', /\$20\.00/.test(btn.title), true);

console.log('\n[5] CASE 4 — after a move, available is $0 so nothing can move again');
render({ profit: 20, loss: 0, available: 0 });
check('the profit total is history, not a balance', els.icProfit.textContent, '$20.00');
check('Move to Wallet disabled', btn.disabled, true);
check('the sub-line says nothing is available', /No realized GOLF profit to move yet/.test(els.icProfitSub.textContent), true);

console.log('\n[6] only a POSITIVE realized amount is ever treated as transferable');
[[0, false], [-1, false], [NaN, false], [0.004, true], [20, true]].forEach(([v, want]) => {
  render({ profit: 0, loss: 0, available: v });
  check('available=' + v + ' -> enabled=' + want, btn.disabled, !want);
});

console.log('\n[7] a negative total can never leak into either field');
render({ profit: -5, loss: 5, available: -5 });
check('PROFIT clamped to zero', els.icProfit.textContent, '$0.00');
check('LOSS clamped to zero', els.icLoss.textContent, '$0.00');
check('Move to Wallet disabled', btn.disabled, true);

console.log('\n[8] the card reports the coin actually being traded');
sandbox.vantaActiveCoin = 'nova';
check('active symbol', api.activeSymbol(), 'NOVA');
sandbox.vantaActiveCoin = null;
check('defaults to GOLF', api.activeSymbol(), 'GOLF');
sandbox.vantaActiveCoin = 'GOLF';
render({ profit: 30, loss: 0, available: 30 });
check('button names the traded coin', /GOLF profit/.test(btn.title), true);

console.log('\n[9] the live figures come from the server, per coin, already split');
const p = {
  coins: [
    { symbol: 'GOLF', realized_profit: 681.01, realized_loss: -60, available_profit: 681.01 },
    { symbol: 'NOVA', realized_profit: 0, realized_loss: -120, available_profit: 0 },
  ],
  realized_profit: 681.01, realized_loss: -180, available_profit: 681.01,
};
const golf = api.liveSplit(p, 'GOLF');
check('GOLF profit', golf.profit, 681.01);
check('GOLF loss', golf.loss, -60);
check('GOLF available', golf.available, 681.01);
const nova = api.liveSplit(p, 'NOVA');
check('NOVA profit', nova.profit, 0);
check('NOVA loss', nova.loss, -120);
check('NOVA available (a loss is not transferable)', nova.available, 0);
const btc = api.liveSplit(p, 'BTC');
check('a non-platform coin has no result of its own', btc.profit, 0);
check('a non-platform coin has nothing to move', btc.available, 0);
check('a response with no coin list falls back to the totals',
  api.liveSplit({ realized_profit: 5, realized_loss: -1, available_profit: 5 }, 'GOLF').profit, 5);

console.log('\n[10] a refresh cannot re-enable the button mid-transfer');
render({ profit: 20, loss: 0, available: 20 });
check('enabled', btn.disabled, false);
btn.dataset.vantaBusy = '1';
api.setMoveAvailability(20, 'GOLF');
check('still disabled while a move is in flight', btn.disabled, false);
btn.dataset.vantaBusy = '';
api.setMoveAvailability(0, 'GOLF');
check('re-disabled once there is no profit', btn.disabled, true);

console.log('\n[11] the button opens Select Coin, then the SHARED account PIN, and only then moves');
/* The old assertions here matched the source text of a handler that moved money
   the instant it was clicked. That is exactly what the client asked to change, so
   this section no longer pattern-matches: it runs the real dialog code out of the
   file, against a stub DOM, and inspects the request it actually makes. */
check('the dialog exists in the page', /id="vantaProfitMove"/.test(html), true);
check('it has a coin step', /id="vpmCoinStep"/.test(html), true);
check('it has NO PIN field of its own', /id="vpmPin"/.test(html), false);
check('it has no PIN error line of its own', /id="vpmError"/.test(html), false);
check('the PIN prompt it uses is the shared one', /id="vantaPinModal"/.test(html), true);
check('it posts to the profit endpoint', /\/api\/platform\/move-profit/.test(moveSrc), true);
check('it sends the traded symbol', /symbol:sym/.test(moveSrc), true);
check('it sends the CHOSEN destination', /dest_symbol:dest/.test(moveSrc), true);
check('it sends the PIN for the server to verify', /pin:pin/.test(moveSrc), true);
check('no amount is sent by the client', /\bamount:/.test(moveSrc), false);
check('it no longer liquidates the whole holding', /api\/platform\/liquidate/.test(moveSrc), false);
check('it re-reads the active coin', /vantaActiveCoin/.test(moveSrc), true);
check('a double click is refused while busy', /vantaBusy==='1'\) return/.test(moveSrc), true);
check('a second submit cannot interleave', /if\(vpmBusy\) return/.test(moveSrc), true);
check('it refreshes the trading UI', /vantaRefreshInfoCards/.test(moveSrc), true);
check('it refreshes the wallet UI', /vantaRefreshWalletSplit/.test(moveSrc), true);
check('it refreshes the profit source list', /icSrcInvalidate/.test(moveSrc), true);
check('it offers only tradeable coins', /tradeable!==false/.test(moveSrc), true);
/* ONE PIN for the account: the move goes through the same gate Convert uses, so a
   PIN created on either surface is the PIN the other one asks for. */
check('it goes through the shared PIN gate', /window\.vantaRequirePin\(/.test(moveSrc), true);
check('it does not prompt for a PIN itself', /vpmPin\b|vpmError\b/.test(moveSrc), false);
check('it does not set a PIN itself', /api\/wallet\/pin/.test(moveSrc), false);
check('first use offers "Create PIN & move"', /setupButton:'Create PIN & move'/.test(moveSrc), true);
check('later uses offer "Move to wallet"', /loginButton:'Move to wallet'/.test(moveSrc), true);
check('a failure is said out loud, never swallowed', /toast\(why\|\|'Move failed/.test(moveSrc), true);

console.log('\n[12] practice mode applies the same rules');
check('practice splits profit and loss', /window\.vantaPracticeRealizedSplit=function/.test(practiceSrc), true);
check('practice only counts positive results as profit', /if\(p>0\)\{ profit\+=p;/.test(practiceSrc), true);
check('practice only counts negative results as loss', /else if\(p<0\) loss\+=p;/.test(practiceSrc), true);
check('practice never nets them', !/available=profit\+loss/.test(practiceSrc), true);
check('a moved profit stops being available', /__profitMoved/.test(practiceSrc), true);
check('a non-positive move is refused', /if\(!\(usd>0\)\) return \{usd:0/.test(practiceSrc), true);
check('practice never moves more than it holds', /Math\.min\(usd, have\)/.test(practiceSrc), true);
check('with no choice, the default is the traded coin', /var d=String\(destSymbol\|\|s\)\.toUpperCase\(\)/.test(practiceSrc), true);
check('a chosen coin is credited at ITS own price', /getPrice\(d\)/.test(practiceSrc), true);
check('the credited coin is the chosen one', /m\.__wallet\[d\]/.test(practiceSrc), true);
check('the card reads the practice split', /vantaPracticeRealizedSplit\(sym\)/.test(html), true);

console.log('\n[13] the trading and wallet ledgers stay separate');
check('the page still posts ordinary transfers', /api\/wallet\/transfer/.test(html), true);
check('the old whole-holding cash-out helper is untouched', /vantaPracticeLiquidate=function/.test(html), true);
check('nothing syncs the two ledgers automatically', !/vantaSyncWalletUI\(\);\s*\}\s*catch\(e\)\{\}\s*\}\s*repaint/.test(html), true);

console.log('\n' + '='.repeat(60));
if (FAILS.length) {
  console.log(FAILS.length + ' CHECK(S) FAILED');
  FAILS.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
